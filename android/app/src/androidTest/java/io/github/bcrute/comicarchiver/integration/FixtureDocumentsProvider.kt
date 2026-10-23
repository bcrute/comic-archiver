package io.github.bcrute.comicarchiver.integration

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Binder
import android.os.Bundle
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract.Document
import android.provider.DocumentsContract.Root
import android.provider.DocumentsProvider
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileNotFoundException
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** Files belong solely to the disposable emulator's test APK, never to the target library. */
private object FixtureDocuments {
    const val TITLE = "Archive integration fixture"
    lateinit var root: File
    @Volatile var armed = false
    @Volatile var gate: Gate? = null
    class Gate(val document: String) {
        val release = CountDownLatch(1)
        val closed = CountDownLatch(1)
        @Volatile var blocked = false
        @Volatile var bytes = 0L
    }

    @Synchronized fun initialize(context: Context) {
        FixtureSafety.requireEmulator()
        if (!::root.isInitialized) root = context.filesDir.resolve("export-fixture").canonicalFile.apply { mkdirs() }
    }

    fun file(id: String): File {
        if (id == "root") return root
        if (!id.startsWith("root/")) throw FileNotFoundException("Unknown fixture document")
        val result = root.resolve(id.removePrefix("root/")).canonicalFile
        if (!result.path.startsWith(root.path + File.separator)) throw FileNotFoundException("Outside fixture tree")
        return result
    }
    fun id(file: File) = if (file == root) "root" else "root/" + file.relativeTo(root).invariantSeparatorsPath
    fun digest(file: File): String {
        val hash = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(65536)
            while (true) { val count = input.read(buffer); if (count < 0) break; hash.update(buffer, 0, count) }
        }
        return hash.digest().joinToString("") { "%02x".format(it) }
    }

    @Synchronized fun arm() {
        check(gate == null && !armed)
        armed = true
    }
    @Synchronized fun begin(id: String): Gate? {
        if (!armed || !id.endsWith(".cbz")) return null
        armed = false
        return Gate(id).also { gate = it }
    }
    fun abandon() {
        gate?.let { old ->
            old.release.countDown()
            check(old.closed.await(15, TimeUnit.SECONDS)) { "The interrupted provider writer did not close." }
        }
        gate = null
    }
    fun status(): Bundle {
        val current = gate
        val files = root.walkTopDown().filter { it.isFile }.toList()
        return Bundle().apply {
            putBoolean("blocked", current?.blocked == true)
            putLong("partial_bytes", current?.bytes ?: 0)
            putString("document_id", current?.document ?: "")
            putInt("file_count", files.size)
            putString("files", JSONArray(files.map { file -> JSONObject()
                .put("id", id(file)).put("bytes", file.length()).put("sha256", digest(file)) }).toString())
        }
    }
}

class FixtureDocumentsProvider : DocumentsProvider() {
    override fun onCreate(): Boolean { FixtureDocuments.initialize(requireNotNull(context)); return true }
    private val documentColumns = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
        Document.COLUMN_MIME_TYPE, Document.COLUMN_FLAGS, Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED)

    override fun queryRoots(projection: Array<out String>?): Cursor {
        val columns = projection ?: arrayOf(Root.COLUMN_ROOT_ID, Root.COLUMN_TITLE, Root.COLUMN_DOCUMENT_ID,
            Root.COLUMN_FLAGS, Root.COLUMN_MIME_TYPES, Root.COLUMN_AVAILABLE_BYTES)
        val values = mapOf<String, Any>(Root.COLUMN_ROOT_ID to "fixture", Root.COLUMN_TITLE to FixtureDocuments.TITLE,
            Root.COLUMN_DOCUMENT_ID to "root", Root.COLUMN_FLAGS to
                (Root.FLAG_SUPPORTS_CREATE or Root.FLAG_SUPPORTS_IS_CHILD or Root.FLAG_LOCAL_ONLY),
            Root.COLUMN_MIME_TYPES to "*/*", Root.COLUMN_AVAILABLE_BYTES to 128L * 1024 * 1024)
        return MatrixCursor(columns).apply { newRow().apply {
            for (column in columns) add(column, values[column])
        } }
    }

    private fun add(cursor: MatrixCursor, file: File) {
        if (!file.exists()) throw FileNotFoundException("Fixture document missing")
        val values = mapOf<String, Any>(Document.COLUMN_DOCUMENT_ID to FixtureDocuments.id(file),
            Document.COLUMN_DISPLAY_NAME to if (file == FixtureDocuments.root) FixtureDocuments.TITLE else file.name,
            Document.COLUMN_MIME_TYPE to if (file.isDirectory) Document.MIME_TYPE_DIR
                else if (file.extension == "epub") "application/epub+zip" else "application/vnd.comicbook+zip",
            Document.COLUMN_FLAGS to if (file.isDirectory) Document.FLAG_DIR_SUPPORTS_CREATE
                else Document.FLAG_SUPPORTS_WRITE or Document.FLAG_SUPPORTS_DELETE,
            Document.COLUMN_SIZE to file.length(), Document.COLUMN_LAST_MODIFIED to file.lastModified())
        cursor.newRow().apply { for (column in cursor.columnNames) add(column, values[column]) }
    }

    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor =
        MatrixCursor(projection ?: documentColumns).also { add(it, FixtureDocuments.file(documentId)) }
    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor =
        MatrixCursor(projection ?: documentColumns).also { cursor ->
            FixtureDocuments.file(parentDocumentId).listFiles()?.sortedBy { it.name }?.forEach { add(cursor, it) }
        }
    override fun isChildDocument(parentDocumentId: String, documentId: String): Boolean =
        FixtureDocuments.file(documentId).path.startsWith(FixtureDocuments.file(parentDocumentId).path + File.separator)

    @Synchronized override fun createDocument(parentDocumentId: String, mimeType: String, displayName: String): String {
        require(displayName.isNotBlank() && displayName.length <= 240 && displayName !in setOf(".", "..") &&
            displayName.none { it == '/' || it == '\\' || it.code < 32 })
        val parent = FixtureDocuments.file(parentDocumentId)
        check(parent.isDirectory)
        var file = parent.resolve(displayName)
        var index = 1
        while (file.exists()) {
            check(index <= 1000)
            file = parent.resolve("${displayName.substringBeforeLast('.', displayName)} ($index)" +
                if (displayName.contains('.')) ".${displayName.substringAfterLast('.')}" else "")
            index++
        }
        check(if (mimeType == Document.MIME_TYPE_DIR) file.mkdir() else file.createNewFile())
        return FixtureDocuments.id(file)
    }

    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?): ParcelFileDescriptor {
        val file = FixtureDocuments.file(documentId)
        if (mode.contains('w')) {
            val gate = FixtureDocuments.begin(documentId)
            if (gate != null) {
                val pipe = ParcelFileDescriptor.createPipe()
                thread(name = "fixture-paused-export") {
                    try {
                        FileOutputStream(file, false).use { output ->
                            ParcelFileDescriptor.AutoCloseInputStream(pipe[0]).use { input ->
                                val chunk = ByteArray(8192)
                                val count = input.read(chunk)
                                check(count > 0)
                                output.write(chunk, 0, count); output.flush()
                                gate.bytes = count.toLong(); gate.blocked = true
                                // This process survives the target's forced stop. The host must
                                // stop the target, then explicitly abandon buffered old bytes.
                                check(gate.release.await(180, TimeUnit.SECONDS))
                            }
                        }
                    } finally { gate.blocked = false; gate.closed.countDown() }
                }
                return pipe[1]
            }
        } else {
            FixtureDocuments.gate?.takeIf { it.document == documentId }?.let {
                check(it.closed.await(180, TimeUnit.SECONDS))
            }
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.parseMode(mode))
    }
    override fun deleteDocument(documentId: String) {
        require(documentId != "root")
        check(FixtureDocuments.file(documentId).delete())
    }
}

/** Control is test-only, emulator-only, and limited to the target UID or fleet adb shell. */
class FixtureControlProvider : ContentProvider() {
    override fun onCreate(): Boolean { FixtureDocuments.initialize(requireNotNull(context)); return true }
    override fun call(method: String, arg: String?, extras: Bundle?): Bundle {
        FixtureSafety.requireEmulator()
        val targetUid = requireNotNull(context).packageManager
            .getApplicationInfo("io.github.bcrute.comicarchiver", 0).uid
        check(Binder.getCallingUid() in setOf(targetUid, 2000)) { "Caller is not this fixture's target or fleet shell." }
        when (method) {
            "arm" -> FixtureDocuments.arm()
            "abandon" -> FixtureDocuments.abandon()
            "status" -> Unit
            else -> throw IllegalArgumentException("Unknown fixture control")
        }
        return FixtureDocuments.status()
    }
    override fun query(uri: Uri, projection: Array<out String>?, selection: String?,
                       selectionArgs: Array<out String>?, sortOrder: String?): Cursor? = null
    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = throw UnsupportedOperationException()
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = throw UnsupportedOperationException()
    override fun update(uri: Uri, values: ContentValues?, selection: String?,
                        selectionArgs: Array<out String>?): Int = throw UnsupportedOperationException()
}
