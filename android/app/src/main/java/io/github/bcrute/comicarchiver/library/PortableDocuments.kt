package io.github.bcrute.comicarchiver.library

import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.security.MessageDigest

internal data class PortableDocument(val id: String, val name: String, val mime: String,
    val bytes: Long?, val modified: Long?, val flags: Int = 0) {
    val directory get() = mime == Document.MIME_TYPE_DIR
    val partial get() = flags and (Document.FLAG_PARTIAL or Document.FLAG_VIRTUAL_DOCUMENT) != 0
}
internal data class PortableMetadata(val value: JSONObject?, val bytes: Int)

internal fun projectPortableMetadata(value: JSONObject) = JSONObject().apply {
    for (key in listOf("title", "format", "episode_number", "imported_at", "cover_file")) {
        val item = value.opt(key)
        if (item is String || item is Number || item is Boolean) put(key, item)
    }
}

/** Grant and document operations only: no writable stream, delete, import or download API. */
internal interface PortableDocuments {
    fun retain(tree: Uri)
    fun readable(tree: Uri): Boolean
    fun root(tree: Uri): PortableDocument
    fun children(tree: Uri, parent: PortableDocument): List<PortableDocument>
    fun metadata(tree: Uri, document: PortableDocument, maximum: Int): PortableMetadata
    fun current(tree: Uri, document: PortableDocument): Boolean
    fun open(tree: Uri, document: PortableDocument): ArchiveOpenResult
}

/** Only read permission is requested/persisted; export retains its separate writable picker. */
class ReadOnlyLibraryTree : ActivityResultContracts.OpenDocumentTree() {
    override fun createIntent(context: Context, input: Uri?): Intent = super.createIntent(context, input).apply {
        flags = (flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION.inv()) or
            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
    }
}

internal class ResolverPortableDocuments(private val context: Context,
    private val launch: (Intent) -> Unit = { context.startActivity(it) }) : PortableDocuments {
    private val resolver = context.contentResolver
    private val columns = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
        Document.COLUMN_MIME_TYPE, Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS)
    private fun uri(tree: Uri, id: String) = DocumentsContract.buildDocumentUriUsingTree(tree, id)
    private fun valid(tree: Uri) = tree.scheme == "content" &&
        tree.authority?.matches(Regex("[A-Za-z0-9._-]+")) == true && DocumentsContract.isTreeUri(tree)
    override fun retain(tree: Uri) {
        require(valid(tree))
        resolver.takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        check(readable(tree))
    }
    override fun readable(tree: Uri): Boolean = valid(tree) && resolver.persistedUriPermissions.any {
        it.uri == tree && it.isReadPermission
    }
    private fun read(cursor: Cursor): PortableDocument {
        fun string(column: String) = cursor.getString(cursor.getColumnIndexOrThrow(column))
        fun number(column: String): Long? {
            val index = cursor.getColumnIndexOrThrow(column)
            return if (cursor.isNull(index)) null else cursor.getLong(index)
        }
        return PortableDocument(requireNotNull(string(Document.COLUMN_DOCUMENT_ID)),
            requireNotNull(string(Document.COLUMN_DISPLAY_NAME)), requireNotNull(string(Document.COLUMN_MIME_TYPE)),
            number(Document.COLUMN_SIZE)?.takeIf { it >= 0 },
            number(Document.COLUMN_LAST_MODIFIED)?.takeIf { it in 1000L..32_503_680_000_000 }?.div(1000),
            (number(Document.COLUMN_FLAGS) ?: 0).toInt())
    }
    private fun lookup(tree: Uri, id: String): PortableDocument {
        check(readable(tree))
        return resolver.query(uri(tree, id), columns, null, null, null)?.use { cursor ->
            check(cursor.moveToFirst())
            read(cursor).also { check(it.id == id && !cursor.moveToNext()) }
        } ?: throw IOException("Document unavailable")
    }
    override fun root(tree: Uri) = lookup(tree, DocumentsContract.getTreeDocumentId(tree)).also { check(it.directory) }
    override fun children(tree: Uri, parent: PortableDocument): List<PortableDocument> {
        check(readable(tree) && parent.directory)
        val location = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parent.id)
        return resolver.query(location, columns, null, null, null)?.use { cursor ->
            val result = ArrayList<PortableDocument>()
            while (cursor.moveToNext()) { check(result.size < PortableFolderScanner.MAX_FILES); result.add(read(cursor)) }
            val extras = cursor.extras
            check(!extras.getBoolean(DocumentsContract.EXTRA_LOADING) &&
                !extras.containsKey(DocumentsContract.EXTRA_ERROR)) { "Provider has not completed its listing" }
            check(result.map { it.id }.distinct().size == result.size)
            check(result.map { it.name }.distinct().size == result.size)
            result
        } ?: throw IOException("Folder unavailable")
    }
    override fun metadata(tree: Uri, document: PortableDocument, maximum: Int): PortableMetadata {
        check(readable(tree) && maximum >= 0)
        if (document.bytes != null && document.bytes > maximum) throw IOException("Metadata exceeds limit")
        val bytes = resolver.openInputStream(uri(tree, document.id))?.use { input ->
            val output = ByteArrayOutputStream(); val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer); if (count < 0) break
                if (output.size().toLong() + count > maximum) throw IOException("Metadata exceeds limit")
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        } ?: throw IOException("Metadata unavailable")
        val projected = try {
            projectPortableMetadata(JSONObject(bytes.toString(Charsets.UTF_8)))
        } catch (_: JSONException) { null }
        return PortableMetadata(projected, bytes.size)
    }
    override fun current(tree: Uri, document: PortableDocument): Boolean = try {
        val observed = lookup(tree, document.id)
        !observed.directory && !observed.partial && observed.name == document.name &&
            observed.bytes != 0L && observed.bytes == document.bytes && observed.modified == document.modified
    } catch (_: Exception) { false }
    override fun open(tree: Uri, document: PortableDocument): ArchiveOpenResult {
        if (!readable(tree)) return ArchiveOpenResult.UNAVAILABLE
        val kind = document.name.substringAfterLast('.', "")
        return requestArchiveOpen(context, uri(tree, document.id), kind, launch)
    }
}

internal data class PortableSnapshot(val manifest: JSONObject, val label: String,
    val documents: Map<String, PortableDocument>)

/** A selected Library/project folder, or an exported series folder. No recursive private scan. */
internal class PortableFolderScanner(private val documents: PortableDocuments) {
    companion object { const val MAX_FILES = 50_000; const val MAX_FOLDERS = 2000 }
    fun scan(tree: Uri, checkActive: () -> Unit = {}): PortableSnapshot {
        check(documents.readable(tree))
        val picked = documents.root(tree)
        check(picked.directory && !picked.partial) { "Selected folder is incomplete" }
        fun safe(name: String) = name.isNotBlank() && name.length <= 500 && !name.startsWith('.') &&
            name.none { it == '/' || it == '\\' || it.code < 32 }
        fun archive(name: String) = listOf(".cbz", ".epub", ".tar.zst").firstOrNull { name.endsWith(it) }
        var count = 0; var budget = 64L * 1024 * 1024
        val identities = HashSet<String>()
        identities.add(picked.id)
        val selected = linkedMapOf<String, PortableDocument>()
        fun children(parent: PortableDocument): List<PortableDocument> {
            checkActive()
            return documents.children(tree, parent).also { rows ->
                count += rows.size; check(count <= MAX_FILES)
                check(rows.all { identities.add(it.id) }) { "Repeated document identity" }
                check(rows.map { it.name }.distinct().size == rows.size)
            }.filter { safe(it.name) && !it.partial }
        }
        var root = picked; var entries = children(root)
        if (entries.none { !it.directory && archive(it.name) != null }) {
            entries.singleOrNull { it.directory && it.name == "Library" }?.let {
                root = it; entries = children(it)
            }
        }
        val folders = JSONArray()
        fun folder(directory: PortableDocument, files: List<PortableDocument>) {
            checkActive()
            val archives = files.filter { !it.directory && archive(it.name) != null && it.bytes != 0L }
            if (archives.isEmpty()) return
            val metadataNames = archives.map { it.name.removeSuffix(requireNotNull(archive(it.name))) + ".json" }.toSet() + "series.json"
            val facts = JSONArray()
            for (file in files.filter { !it.directory && (it in archives || it.name in metadataNames ||
                    it.name in setOf("cover.jpg", "cover.png", "cover.gif", "cover.webp")) }) {
                checkActive()
                val key = MessageDigest.getInstance("SHA-256").digest((tree.toString() + "\u0000" + file.id)
                    .toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
                check(selected.put(key, file) == null)
                val fact = JSONObject().put("key", key).put("name", file.name)
                    .put("bytes", file.bytes ?: JSONObject.NULL).put("modified", file.modified ?: JSONObject.NULL)
                if (file.name in metadataNames) {
                    val maximum = minOf(if (file.name == "series.json") 256 * 1024 else 16 * 1024 * 1024, budget.toInt())
                    val meta = documents.metadata(tree, file, maximum)
                    check(meta.bytes in 0..maximum)
                    budget -= meta.bytes; check(budget >= 0)
                    if (meta.value != null) fact.put("metadata", meta.value)
                }
                facts.put(fact)
            }
            check(folders.length() < MAX_FOLDERS)
            folders.put(JSONObject().put("name", directory.name.takeIf(::safe) ?: "Selected folder").put("files", facts))
        }
        folder(root, entries)
        for (directory in entries.filter { it.directory }) folder(directory, children(directory))
        return PortableSnapshot(JSONObject().put("v", 1).put("folders", folders),
            picked.name.takeIf(::safe) ?: "Selected folder", selected)
    }
}
