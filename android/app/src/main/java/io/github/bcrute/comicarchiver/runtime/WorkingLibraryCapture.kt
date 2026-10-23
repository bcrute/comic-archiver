package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.os.CancellationSignal
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.system.Os
import android.system.OsConstants
import android.util.AtomicFile
import androidx.annotation.Keep
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest

internal enum class LibraryCaptureFailure(val words: String) {
    ACCESS("Folder access was lost. Choose the folder again; the previous library is retained."),
    INCOMPLETE("This folder is incomplete or unsupported. Choose a complete project or Library folder."),
    CHANGED("The source library changed. Pause it before trying the copy again."),
    SPACE("There is not enough free space for a verified working copy. Free space and retry or cancel."),
    CANCELLED("Library copying was cancelled. The previous library is retained."),
    PRIVATE_COPY("The private library copy could not be read. Retry or cancel; the previous library is retained."),
}

internal class LibraryCaptureException(val failure: LibraryCaptureFailure) : IllegalStateException(failure.words)

/** Query cancellation and closing a blocked stream belong to the IO cancellation path. */
@Keep
internal class LibraryCaptureCancellation {
    val signal = CancellationSignal()
    @Volatile private var stopped = false
    private val gate = Any()
    private var stream: Closeable? = null
    fun isCancelled(): Boolean = stopped
    fun check() { if (stopped) throw LibraryCaptureException(LibraryCaptureFailure.CANCELLED) }
    fun cancel() {
        stopped = true
        try { signal.cancel() } finally {
            val active = synchronized(gate) { stream }
            try { active?.close() } catch (_: Exception) { }
        }
    }
    fun <T> reading(input: InputStream, action: (InputStream) -> T): T = input.use {
        synchronized(gate) { check(); stream = input }
        try { check(); action(input) }
        finally { synchronized(gate) { if (stream === input) stream = null } }
    }
}

internal data class WorkingLibraryDocument(val id: String, val name: String, val mime: String,
    val bytes: Long?, val modified: Long?, val flags: Int = 0) {
    val directory get() = mime == Document.MIME_TYPE_DIR
    val partial get() = flags and (Document.FLAG_PARTIAL or Document.FLAG_VIRTUAL_DOCUMENT) != 0
    override fun toString() = "WorkingLibraryDocument(private)"
}

/** No write, delete, credential, download or provider mutation API. */
internal interface WorkingLibraryDocuments {
    fun retain(tree: Uri)
    fun readable(tree: Uri): Boolean
    fun root(tree: Uri, cancellation: LibraryCaptureCancellation): WorkingLibraryDocument
    fun children(tree: Uri, parent: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): List<WorkingLibraryDocument>
    fun current(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): Boolean
    fun open(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): InputStream
}

internal class ResolverWorkingLibraryDocuments(context: Context) : WorkingLibraryDocuments {
    private val resolver = context.applicationContext.contentResolver
    private val columns = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
        Document.COLUMN_MIME_TYPE, Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS)
    private fun uri(tree: Uri, id: String) = DocumentsContract.buildDocumentUriUsingTree(tree, id)
    override fun retain(tree: Uri) {
        WorkingLibraryCapture.source(tree)
        resolver.takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        if (!readable(tree)) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
    }
    override fun readable(tree: Uri): Boolean = try {
        val source = WorkingLibraryCapture.source(tree)
        resolver.persistedUriPermissions.any {
            try { it.isReadPermission && WorkingLibraryCapture.source(it.uri).key == source.key }
            catch (_: Exception) { false }
        }
    } catch (_: Exception) { false }
    private fun read(cursor: Cursor): WorkingLibraryDocument {
        fun string(name: String) = cursor.getString(cursor.getColumnIndexOrThrow(name))
        fun number(name: String): Long? {
            val column = cursor.getColumnIndexOrThrow(name)
            return if (cursor.isNull(column)) null else cursor.getLong(column)
        }
        return WorkingLibraryDocument(requireNotNull(string(Document.COLUMN_DOCUMENT_ID)),
            requireNotNull(string(Document.COLUMN_DISPLAY_NAME)), requireNotNull(string(Document.COLUMN_MIME_TYPE)),
            number(Document.COLUMN_SIZE)?.takeIf { it >= 0 }, number(Document.COLUMN_LAST_MODIFIED)?.takeIf { it > 0 },
            (number(Document.COLUMN_FLAGS) ?: 0).toInt())
    }
    private fun lookup(tree: Uri, id: String, cancellation: LibraryCaptureCancellation): WorkingLibraryDocument {
        cancellation.check()
        if (!readable(tree)) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
        return resolver.query(uri(tree, id), columns, null, null, null, cancellation.signal)?.use { cursor ->
            check(cursor.moveToFirst())
            read(cursor).also { check(it.id == id && !cursor.moveToNext()) }
        } ?: throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
    }
    override fun root(tree: Uri, cancellation: LibraryCaptureCancellation) =
        lookup(tree, DocumentsContract.getTreeDocumentId(tree), cancellation)
    override fun children(tree: Uri, parent: WorkingLibraryDocument,
        cancellation: LibraryCaptureCancellation): List<WorkingLibraryDocument> {
        cancellation.check()
        if (!readable(tree)) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
        val location = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parent.id)
        return resolver.query(location, columns, null, null, null, cancellation.signal)?.use { cursor ->
            val result = ArrayList<WorkingLibraryDocument>()
            var textBytes = 0L
            while (cursor.moveToNext()) {
                cancellation.check()
                if (result.size >= WorkingLibraryCapture.MAX_FILES) throw LibraryCaptureException(LibraryCaptureFailure.INCOMPLETE)
                val document = read(cursor)
                textBytes += document.id.toByteArray(Charsets.UTF_8).size.toLong() +
                    document.name.toByteArray(Charsets.UTF_8).size + document.mime.toByteArray(Charsets.UTF_8).size
                if (textBytes > WorkingLibraryCapture.MAX_PLAN_TEXT) throw LibraryCaptureException(LibraryCaptureFailure.INCOMPLETE)
                result.add(document)
            }
            if (cursor.extras.getBoolean(DocumentsContract.EXTRA_LOADING) ||
                cursor.extras.containsKey(DocumentsContract.EXTRA_ERROR)) throw LibraryCaptureException(LibraryCaptureFailure.INCOMPLETE)
            result
        } ?: throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
    }
    override fun current(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation) =
        lookup(tree, document.id, cancellation) == document
    override fun open(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): InputStream {
        cancellation.check()
        if (!readable(tree)) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
        val descriptor = resolver.openAssetFileDescriptor(uri(tree, document.id), "r", cancellation.signal)
            ?: throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
        try { return descriptor.createInputStream() }
        catch (error: Exception) { descriptor.close(); throw error }
    }
}

internal enum class WorkingCapturePhase { CHECKING_COPY, COPYING, CHECKING_SOURCE, CAPTURED }
internal data class WorkingCaptureProgress(val phase: WorkingCapturePhase, val filesDone: Int,
    val filesTotal: Int, val bytesRead: Long, val knownBytes: Long, val unknownSizes: Int)
internal data class WorkingCaptureFile(val path: String, val document: WorkingLibraryDocument, val maximum: Long) {
    override fun toString() = "WorkingCaptureFile(private)"
}

/** Private authority/paths never belong in a public state projection or logs. */
internal class WorkingCapturePlan internal constructor(val tree: Uri, val siteKey: String,
    val root: WorkingLibraryDocument, val folders: Map<String, WorkingLibraryDocument>,
    val files: List<WorkingCaptureFile>) {
    val knownBytes = files.fold(0L) { total, file -> Math.addExact(total, file.document.bytes ?: 0L) }
    val unknownSizes = files.count { it.document.bytes == null }
    val label = root.name.filter { it.code >= 32 && it.code != 127 }.take(120).ifBlank { "Picked library" }
    override fun toString() = "WorkingCapturePlan(private)"
}

internal class CapturedWorkingLibrary(val plan: WorkingCapturePlan, val workspace: File, val bytes: Long) {
    override fun toString() = "CapturedWorkingLibrary(private)"
}

/** Complete read-only SAF capture, before shared-core verification and explicit root selection. */
internal class WorkingLibraryCapture(private val documents: WorkingLibraryDocuments,
    private val workspaceFolders: Map<String, String>,
    private val freeBytes: (File) -> Long = { it.usableSpace },
    private val reserve: Long = 256L * 1024 * 1024) {

    init {
        require(workspaceFolders.isNotEmpty() && workspaceFolders.values.count { it.isEmpty() } == 1)
        require(workspaceFolders.keys.all { it.matches(Regex("[a-z][a-z0-9_-]{0,40}")) })
        require(workspaceFolders.values.filter { it.isNotEmpty() }.all { name(it) && !it.startsWith('.') })
        require(workspaceFolders.values.distinct().size == workspaceFolders.size && reserve >= 0)
    }

    fun scan(pickedTree: Uri, siteKey: String, cancellation: LibraryCaptureCancellation = LibraryCaptureCancellation()): WorkingCapturePlan =
        guarded(cancellation) {
            val tree = Uri.parse(source(pickedTree).tree)
            if (siteKey !in workspaceFolders) fail(LibraryCaptureFailure.INCOMPLETE)
            if (!documents.readable(tree)) fail(LibraryCaptureFailure.ACCESS)
            val root = documents.root(tree, cancellation)
            valid(root, true)
            val folders = sortedMapOf<String, WorkingLibraryDocument>()
            val files = sortedMapOf<String, WorkingCaptureFile>()
            val claimed = mutableSetOf<String>()
            var textBytes = 0L
            val listed = mutableMapOf<String, List<WorkingLibraryDocument>>()
            fun children(parent: WorkingLibraryDocument): List<WorkingLibraryDocument> = listed.getOrPut(parent.id) {
                cancellation.check()
                val values = documents.children(tree, parent, cancellation)
                if (values.size > MAX_FILES || values.map { it.id }.distinct().size != values.size ||
                    values.map { it.name }.distinct().size != values.size) fail(LibraryCaptureFailure.INCOMPLETE)
                values.forEach { valid(it, it.directory) }
                if (values.sumOf { documentSize(it) } > MAX_PLAN_TEXT) fail(LibraryCaptureFailure.INCOMPLETE)
                values.sortedBy { it.name }
            }
            fun claim(path: String, document: WorkingLibraryDocument) {
                relative(path)
                if (!claimed.add(document.id) || folders.size + files.size >= MAX_FILES) fail(LibraryCaptureFailure.INCOMPLETE)
                textBytes += documentSize(document) + path.toByteArray(Charsets.UTF_8).size
                if (textBytes > MAX_PLAN_TEXT) fail(LibraryCaptureFailure.INCOMPLETE)
            }
            fun folder(path: String, document: WorkingLibraryDocument) {
                valid(document, true); claim(path, document)
                if (folders.put(path, document) != null || files.containsKey(path)) fail(LibraryCaptureFailure.INCOMPLETE)
            }
            fun file(path: String, document: WorkingLibraryDocument, maximum: Long) {
                valid(document, false); claim(path, document)
                if (document.bytes != null && document.bytes > maximum) fail(LibraryCaptureFailure.INCOMPLETE)
                if (files.put(path, WorkingCaptureFile(path, document, maximum)) != null || folders.containsKey(path))
                    fail(LibraryCaptureFailure.INCOMPLETE)
            }
            fun optional(parent: WorkingLibraryDocument, key: String) = children(parent).singleOrNull { it.name == key }
            fun privateState(prefix: String, state: WorkingLibraryDocument) {
                valid(state, true)
                folder("$prefix/.archive-state", state)
                for (item in children(state)) {
                    val path = "$prefix/.archive-state/${item.name}"
                    when {
                        item.name in DATABASES -> file(path, item, MAX_DATABASE)
                        item.name == "provenance" -> {
                            folder(path, item)
                            for (record in children(item)) {
                                if (!record.name.endsWith(".json") || !UUID.matches(record.name.removeSuffix(".json")))
                                    fail(if (record.name.endsWith(".tmp")) LibraryCaptureFailure.CHANGED else LibraryCaptureFailure.INCOMPLETE)
                                file("$path/${record.name}", record, MAX_JSON)
                            }
                        }
                        item.name == "cloud" -> {
                            folder(path, item)
                            optional(item, "targets.json")?.let { file("$path/targets.json", it, MAX_CLOUD_JSON) }
                            for (category in listOf("outbox", "history")) {
                                val group = optional(item, category) ?: continue
                                folder("$path/$category", group)
                                val provider = optional(group, "b2") ?: continue
                                val providerPath = "$path/$category/b2"
                                folder(providerPath, provider)
                                for (entry in children(provider)) {
                                    if (category == "outbox") {
                                        if (!entry.name.endsWith(".json") || !UUID.matches(entry.name.removeSuffix(".json")))
                                            fail(LibraryCaptureFailure.INCOMPLETE)
                                        file("$providerPath/${entry.name}", entry, MAX_CLOUD_JSON)
                                    } else {
                                        if (!UUID.matches(entry.name)) fail(LibraryCaptureFailure.INCOMPLETE)
                                        folder("$providerPath/${entry.name}", entry)
                                        for (record in children(entry)) {
                                            if (!record.name.endsWith(".json")) fail(LibraryCaptureFailure.INCOMPLETE)
                                            file("$providerPath/${entry.name}/${record.name}", record, MAX_CLOUD_JSON)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            fun library(prefix: String, library: WorkingLibraryDocument) {
                folder(prefix, library)
                for (series in children(library)) {
                    if (series.name == ".archive-state") { privateState(prefix, series); continue }
                    if (series.name.startsWith('.')) continue
                    valid(series, true)
                    val seriesPath = "$prefix/${series.name}"
                    folder(seriesPath, series)
                    for (entry in children(series)) {
                        val maximum = when {
                            entry.name.endsWith(".json") -> MAX_JSON
                            entry.name in COVERS -> 10L * 1024 * 1024
                            entry.name.endsWith(".cbz") || entry.name.endsWith(".epub") || entry.name.endsWith(".tar.zst") -> MAX_ARCHIVE
                            entry.name.endsWith(".tmp") || entry.name.endsWith(".part") -> fail(LibraryCaptureFailure.CHANGED)
                            else -> fail(LibraryCaptureFailure.INCOMPLETE)
                        }
                        file("$seriesPath/${entry.name}", entry, maximum)
                    }
                }
            }
            val top = children(root)
            val defaultLibrary = top.singleOrNull { it.name == "Library" && it.directory }
            val providerLibraries = workspaceFolders.filterValues { it.isNotEmpty() }.mapNotNull { (_, mapping) ->
                val container = top.singleOrNull { it.name == mapping && it.directory } ?: return@mapNotNull null
                val library = optional(container, "Library")?.takeIf { it.directory } ?: return@mapNotNull null
                Triple(mapping, container, library)
            }
            if (defaultLibrary != null || providerLibraries.isNotEmpty()) {
                defaultLibrary?.let { library("Library", it) }
                for ((mapping, container, child) in providerLibraries) {
                    folder(mapping, container); library("$mapping/Library", child)
                }
            } else {
                val mapping = workspaceFolders.getValue(siteKey)
                library(if (mapping.isEmpty()) "Library" else "$mapping/Library", root)
            }
            WorkingCapturePlan(tree, siteKey, root, folders.toMap(), files.values.toList())
        }

    /** The root is reserved by WorkingLibraryLocations; a completed file is reusable only by its journal hash. */
    fun capture(plan: WorkingCapturePlan, root: File, cancellation: LibraryCaptureCancellation = LibraryCaptureCancellation(),
        progress: (WorkingCaptureProgress) -> Unit = {}): CapturedWorkingLibrary = guarded(cancellation) {
        safe(root)
        if (planJson(scan(plan.tree, plan.siteKey, cancellation)).toString() != planJson(plan).toString())
            fail(LibraryCaptureFailure.CHANGED)
        privateDirectory(root)
        val stateFile = root.resolve(STATE)
        val atomic = AtomicFile(stateFile)
        val state = if (stateFile.exists() || root.resolve("$STATE.bak").exists()) read(atomic) else {
            if (root.listFiles()?.isNotEmpty() != false) fail(LibraryCaptureFailure.PRIVATE_COPY)
            JSONObject().put("v", 1).put("plan", planJson(plan)).put("stable", false)
                .also { write(atomic, it) }
        }
        validateState(state, plan)
        state.put("stable", false); write(atomic, state)
        val workspace = root.resolve("workspace").also { privateDirectory(it) }
        plan.folders.keys.forEach { privateDirectory(target(workspace, it), workspace) }
        val completed = completed(root, plan)
        val databases = plan.files.filter { it.path.substringAfterLast('/') in DATABASES }
        var copied = 0L
        var knownCopied = 0L
        var count = 0
        progress(WorkingCaptureProgress(WorkingCapturePhase.CHECKING_COPY, 0, plan.files.size, 0, plan.knownBytes, plan.unknownSizes))
        for (file in plan.files) {
            cancellation.check()
            val path = target(workspace, file.path)
            val saved = completed.optJSONObject(file.path)
            if (saved != null && path.isFile && sameFact(hashPrivate(path, cancellation) { size ->
                    progress(WorkingCaptureProgress(WorkingCapturePhase.CHECKING_COPY, count, plan.files.size,
                        Math.addExact(copied, size), plan.knownBytes, plan.unknownSizes))
                }, saved)) {
                copied = Math.addExact(copied, saved.getLong("size")); count++
                if (file.document.bytes != null) knownCopied = Math.addExact(knownCopied, saved.getLong("size"))
            } else if (saved != null) fail(LibraryCaptureFailure.PRIVATE_COPY)
        }
        val remaining = Math.addExact(maxOf(0, Math.subtractExact(Math.multiplyExact(plan.knownBytes, 2), knownCopied)),
            Math.subtractExact(copied, knownCopied))
        room(root, Math.addExact(remaining, scratchBytes(databases, completed)))
        progress(WorkingCaptureProgress(WorkingCapturePhase.COPYING, count, plan.files.size, copied, plan.knownBytes, plan.unknownSizes))
        for (file in plan.files) {
            cancellation.check()
            if (completed.has(file.path)) continue
            if (!documents.current(plan.tree, file.document, cancellation)) fail(LibraryCaptureFailure.CHANGED)
            val path = target(workspace, file.path)
            val temporary = path.resolveSiblingCapture()
            safe(path); safe(temporary)
            if (path.exists() && !path.delete() || temporary.exists() && !temporary.delete()) fail(LibraryCaptureFailure.PRIVATE_COPY)
            var written = 0L
            val hash = MessageDigest.getInstance("SHA-256")
            val previousScratch = scratchBytes(databases, completed)
            try {
                FileOutputStream(temporary).use { output ->
                    Os.fchmod(output.fd, 384)
                    cancellation.reading(documents.open(plan.tree, file.document, cancellation)) { input ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            cancellation.check()
                            val size = input.read(buffer)
                            if (size < 0) break
                            if (size == 0) continue
                            written = Math.addExact(written, size.toLong())
                            if (written > file.maximum || file.document.bytes != null && written > file.document.bytes)
                                fail(LibraryCaptureFailure.CHANGED)
                            val scratch = Math.addExact(previousScratch,
                                if (file.document.bytes == null && file.path.substringAfterLast('/') in DATABASES) written else 0)
                            room(root, Math.addExact(Math.addExact(Math.addExact(copied, written), size.toLong()), scratch))
                            output.write(buffer, 0, size); hash.update(buffer, 0, size)
                            progress(WorkingCaptureProgress(WorkingCapturePhase.COPYING, count, plan.files.size,
                                Math.addExact(copied, written), plan.knownBytes, plan.unknownSizes))
                        }
                    }
                    output.fd.sync()
                }
                if (file.document.bytes != null && written != file.document.bytes ||
                    !documents.current(plan.tree, file.document, cancellation)) fail(LibraryCaptureFailure.CHANGED)
                if (!temporary.renameTo(path)) fail(LibraryCaptureFailure.PRIVATE_COPY)
                syncDirectory(path.parentFile ?: fail(LibraryCaptureFailure.PRIVATE_COPY))
                val saved = fact(written, hex(hash.digest()))
                val journal = root.resolve("completed").also { privateDirectory(it) }
                write(AtomicFile(journal.resolve(receiptName(file.path))), JSONObject(saved.toString()).put("path", file.path))
                completed.put(file.path, saved)
                copied = Math.addExact(copied, written); count++
            } finally { temporary.delete() }
        }
        if (planJson(scan(plan.tree, plan.siteKey, cancellation)).toString() != planJson(plan).toString())
            fail(LibraryCaptureFailure.CHANGED)
        var checked = 0L
        for ((index, file) in plan.files.withIndex()) {
            cancellation.check()
            val saved = completed.getJSONObject(file.path)
            val observed = hashSource(plan.tree, file, cancellation) { size ->
                progress(WorkingCaptureProgress(WorkingCapturePhase.CHECKING_SOURCE, index, plan.files.size,
                    Math.addExact(checked, size), copied, 0))
            }
            if (!sameFact(observed, saved)) fail(LibraryCaptureFailure.CHANGED)
            checked = Math.addExact(checked, observed.getLong("size"))
        }
        if (planJson(scan(plan.tree, plan.siteKey, cancellation)).toString() != planJson(plan).toString())
            fail(LibraryCaptureFailure.CHANGED)
        verifyWorkspace(workspace, plan)
        room(root, Math.addExact(copied, scratchBytes(databases, completed)))
        state.put("stable", true); write(atomic, state)
        progress(WorkingCaptureProgress(WorkingCapturePhase.CAPTURED, plan.files.size, plan.files.size, copied, copied, 0))
        cancellation.check()
        CapturedWorkingLibrary(plan, workspace, copied)
    }

    /** A completed capture can finish core preparation while its original source is offline. */
    fun recover(root: File, expectedSource: WorkingLibrarySource,
        cancellation: LibraryCaptureCancellation = LibraryCaptureCancellation()): CapturedWorkingLibrary? = guarded(cancellation) {
        safe(root)
        val state = read(AtomicFile(root.resolve(STATE)))
        val plan = parsePlan(state.getJSONObject("plan"))
        if (source(plan.tree) != expectedSource) fail(LibraryCaptureFailure.PRIVATE_COPY)
        validateState(state, plan)
        if (!state.getBoolean("stable")) return@guarded null
        val completed = completed(root, plan)
        val databases = plan.files.filter { it.path.substringAfterLast('/') in DATABASES }
        if (completed.length() != plan.files.size) fail(LibraryCaptureFailure.PRIVATE_COPY)
        val workspace = root.resolve("workspace")
        safe(workspace)
        var bytes = 0L
        for (file in plan.files) {
            val observed = hashPrivate(target(workspace, file.path), cancellation)
            if (!sameFact(observed, completed.getJSONObject(file.path))) fail(LibraryCaptureFailure.PRIVATE_COPY)
            bytes = Math.addExact(bytes, completed.getJSONObject(file.path).getLong("size"))
        }
        verifyWorkspace(workspace, plan)
        room(root, Math.addExact(bytes, scratchBytes(databases, completed)))
        cancellation.check()
        CapturedWorkingLibrary(plan, workspace, bytes)
    }

    private fun room(root: File, required: Long) {
        if (freeBytes(root) < Math.addExact(required, reserve)) fail(LibraryCaptureFailure.SPACE)
    }
    private fun scratchBytes(databases: List<WorkingCaptureFile>, completed: JSONObject): Long = databases.fold(0L) { total, file -> Math.addExact(total,
        completed.optJSONObject(file.path)?.getLong("size") ?: file.document.bytes ?: 0L) }

    private fun hashSource(tree: Uri, file: WorkingCaptureFile, cancellation: LibraryCaptureCancellation,
        progress: (Long) -> Unit): JSONObject {
        if (!documents.current(tree, file.document, cancellation)) fail(LibraryCaptureFailure.CHANGED)
        val hash = MessageDigest.getInstance("SHA-256")
        var total = 0L
        cancellation.reading(documents.open(tree, file.document, cancellation)) { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                cancellation.check()
                val size = input.read(buffer)
                if (size < 0) break
                if (size == 0) continue
                total = Math.addExact(total, size.toLong())
                if (total > file.maximum || file.document.bytes != null && total > file.document.bytes)
                    fail(LibraryCaptureFailure.CHANGED)
                hash.update(buffer, 0, size); progress(total)
            }
        }
        if (file.document.bytes != null && total != file.document.bytes ||
            !documents.current(tree, file.document, cancellation)) fail(LibraryCaptureFailure.CHANGED)
        return fact(total, hex(hash.digest()))
    }
    private fun hashPrivate(file: File, cancellation: LibraryCaptureCancellation, progress: (Long) -> Unit = {}): JSONObject {
        safe(file)
        val hash = MessageDigest.getInstance("SHA-256")
        var total = 0L
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                cancellation.check()
                val count = input.read(buffer)
                if (count < 0) break
                hash.update(buffer, 0, count); total = Math.addExact(total, count.toLong())
                progress(total)
            }
        }
        return fact(total, hex(hash.digest()))
    }
    private fun documentJson(document: WorkingLibraryDocument) = JSONObject().put("id", document.id).put("name", document.name)
        .put("mime", document.mime).put("bytes", document.bytes ?: JSONObject.NULL)
        .put("modified", document.modified ?: JSONObject.NULL).put("flags", document.flags)
    private fun planJson(plan: WorkingCapturePlan) = JSONObject().put("tree", plan.tree.toString()).put("site", plan.siteKey)
        .put("root", documentJson(plan.root)).put("folders", JSONArray().apply {
            plan.folders.toSortedMap().forEach { (path, document) -> put(JSONObject().put("path", path).put("document", documentJson(document))) }
        }).put("files", JSONArray().apply {
            plan.files.sortedBy { it.path }.forEach { put(JSONObject().put("path", it.path)
                .put("document", documentJson(it.document)).put("maximum", it.maximum)) }
        })
    private fun parsePlan(value: JSONObject): WorkingCapturePlan {
        if (fields(value) != setOf("tree", "site", "root", "folders", "files")) fail(LibraryCaptureFailure.PRIVATE_COPY)
        val tree = Uri.parse(value.string("tree")); source(tree)
        val site = value.string("site")
        if (site !in workspaceFolders) fail(LibraryCaptureFailure.PRIVATE_COPY)
        fun document(item: JSONObject): WorkingLibraryDocument {
            if (fields(item) != setOf("id", "name", "mime", "bytes", "modified", "flags")) fail(LibraryCaptureFailure.PRIVATE_COPY)
            fun number(key: String) = if (item.isNull(key)) null else item.opt(key).let {
                if (it !is Number || it.toLong() < 0 || it.toDouble() != it.toLong().toDouble()) fail(LibraryCaptureFailure.PRIVATE_COPY)
                it.toLong()
            }
            val flag = number("flags") ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (flag > Int.MAX_VALUE) fail(LibraryCaptureFailure.PRIVATE_COPY)
            return WorkingLibraryDocument(item.string("id"), item.string("name"), item.string("mime"),
                number("bytes"), number("modified"), flag.toInt()).also { valid(it, it.directory) }
        }
        val root = document(value.getJSONObject("root")); valid(root, true)
        val folders = value.getJSONArray("folders"); val files = value.getJSONArray("files")
        if (folders.length().toLong() + files.length() > MAX_FILES || folders.length() == 0) fail(LibraryCaptureFailure.PRIVATE_COPY)
        val ids = mutableSetOf<String>(); val paths = mutableSetOf<String>()
        fun path(item: JSONObject): String {
            val path = item.string("path"); relative(path)
            if (!paths.add(path)) fail(LibraryCaptureFailure.PRIVATE_COPY)
            return path
        }
        fun bound(item: JSONObject): WorkingLibraryDocument {
            val document = document(item.getJSONObject("document"))
            if (!ids.add(document.id)) fail(LibraryCaptureFailure.PRIVATE_COPY)
            return document
        }
        val parsedFolders = sortedMapOf<String, WorkingLibraryDocument>()
        for (index in 0 until folders.length()) {
            val item = folders.getJSONObject(index)
            if (fields(item) != setOf("path", "document")) fail(LibraryCaptureFailure.PRIVATE_COPY)
            val document = bound(item); valid(document, true)
            parsedFolders[path(item)] = document
        }
        val parsedFiles = ArrayList<WorkingCaptureFile>()
        for (index in 0 until files.length()) {
            val item = files.getJSONObject(index)
            if (fields(item) != setOf("path", "document", "maximum")) fail(LibraryCaptureFailure.PRIVATE_COPY)
            val document = bound(item); valid(document, false)
            val maximum = item.opt("maximum") as? Number ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (maximum.toLong() !in 1..MAX_ARCHIVE || maximum.toDouble() != maximum.toLong().toDouble()) fail(LibraryCaptureFailure.PRIVATE_COPY)
            parsedFiles.add(WorkingCaptureFile(path(item), document, maximum.toLong()))
        }
        return WorkingCapturePlan(tree, site, root, parsedFolders, parsedFiles)
    }
    private fun validateState(state: JSONObject, plan: WorkingCapturePlan) {
        if (fields(state) != setOf("v", "plan", "stable") || state.opt("v") != 1 ||
            state.opt("stable") !is Boolean || state.getJSONObject("plan").toString() != planJson(plan).toString())
            fail(LibraryCaptureFailure.PRIVATE_COPY)
    }
    private fun completed(root: File, plan: WorkingCapturePlan): JSONObject {
        val directory = root.resolve("completed").also { safe(it) }
        val allowed = plan.files.associate { receiptName(it.path) to it.path }
        val completed = JSONObject()
        if (!directory.exists()) return completed
        val entries = directory.listFiles() ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
        if (entries.size.toLong() > MAX_FILES.toLong() * 3) fail(LibraryCaptureFailure.PRIVATE_COPY)
        val names = mutableSetOf<String>()
        for (entry in entries) {
            safe(entry)
            val name = entry.name.removeSuffix(".bak").removeSuffix(".new")
            if (name !in allowed || !entry.isFile || entry.name !in setOf(name, "$name.bak", "$name.new"))
                fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (entry.name == name || entry.name.endsWith(".bak")) names.add(name)
        }
        for (name in names) {
            val record = read(AtomicFile(directory.resolve(name)), 4096)
            val path = allowed.getValue(name)
            if (fields(record) != setOf("path", "size", "sha256") || record.opt("path") != path)
                fail(LibraryCaptureFailure.PRIVATE_COPY)
            completed.put(path, JSONObject(record.toString()).apply { remove("path") })
        }
        val paths = plan.files.associateBy { it.path }
        for (path in fields(completed)) {
            val file = paths[path] ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
            val item = completed.getJSONObject(path)
            val size = item.opt("size") as? Number ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (fields(item) != setOf("size", "sha256") || !HASH.matches(item.string("sha256")) ||
                size.toLong() !in 0..file.maximum || size.toDouble() != size.toLong().toDouble() ||
                file.document.bytes != null && file.document.bytes != size.toLong()) fail(LibraryCaptureFailure.PRIVATE_COPY)
        }
        return completed
    }
    private fun verifyWorkspace(workspace: File, plan: WorkingCapturePlan) {
        val expectedFiles = plan.files.map { it.path }.toSet()
        val expectedFolders = plan.folders.keys.flatMap { path ->
            val pieces = path.split('/'); (1..pieces.size).map { pieces.take(it).joinToString("/") }
        }.toSet()
        val found = mutableSetOf<String>()
        fun walk(folder: File) {
            safe(folder)
            for (file in folder.listFiles() ?: fail(LibraryCaptureFailure.PRIVATE_COPY)) {
                safe(file)
                val relative = file.relativeTo(workspace).invariantSeparatorsPath
                if (file.isDirectory) {
                    if (relative !in expectedFolders) fail(LibraryCaptureFailure.PRIVATE_COPY)
                    walk(file)
                } else {
                    if (!file.isFile || relative !in expectedFiles || !found.add(relative)) fail(LibraryCaptureFailure.PRIVATE_COPY)
                }
            }
        }
        walk(workspace)
        if (found != expectedFiles) fail(LibraryCaptureFailure.PRIVATE_COPY)
    }
    private fun read(file: AtomicFile, maximum: Int = MAX_STATE): JSONObject {
        safe(file.baseFile); safe(File(file.baseFile.path + ".bak")); safe(File(file.baseFile.path + ".new"))
        return file.openRead().use { input ->
            val output = ByteArrayOutputStream(); val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer); if (count < 0) break
                if (output.size().toLong() + count > maximum) fail(LibraryCaptureFailure.PRIVATE_COPY)
                output.write(buffer, 0, count)
            }
            JSONObject(output.toString(Charsets.UTF_8.name()))
        }
    }
    private fun write(file: AtomicFile, value: JSONObject) {
        safe(file.baseFile); safe(File(file.baseFile.path + ".bak")); safe(File(file.baseFile.path + ".new"))
        val data = value.toString().toByteArray(Charsets.UTF_8)
        if (data.size > MAX_STATE) fail(LibraryCaptureFailure.PRIVATE_COPY)
        val stream = file.startWrite()
        try { Os.fchmod(stream.fd, 384); stream.write(data); file.finishWrite(stream) }
        catch (error: Exception) { file.failWrite(stream); throw error }
        syncDirectory(file.baseFile.parentFile ?: fail(LibraryCaptureFailure.PRIVATE_COPY))
    }
    private fun target(root: File, path: String): File {
        relative(path)
        return File(root, path).also { safe(it); if (!it.path.startsWith(root.path + File.separator)) fail(LibraryCaptureFailure.PRIVATE_COPY) }
    }
    private fun privateDirectory(path: File, base: File = path) {
        safe(path)
        if (!path.isDirectory && !path.mkdirs()) fail(LibraryCaptureFailure.PRIVATE_COPY)
        var current = path
        while (true) {
            if (current != base && !current.path.startsWith(base.path + File.separator)) fail(LibraryCaptureFailure.PRIVATE_COPY)
            Os.chmod(current.path, 448)
            if (current == base) break
            current = current.parentFile ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
        }
    }
    private fun syncDirectory(path: File) {
        safe(path)
        val descriptor = Os.open(path.path, OsConstants.O_RDONLY or OsConstants.O_DIRECTORY or OsConstants.O_NOFOLLOW, 0)
        try { Os.fsync(descriptor) } finally { Os.close(descriptor) }
    }
    private fun File.resolveSiblingCapture() = File(parentFile, ".capture-part-" +
        hex(MessageDigest.getInstance("SHA-256").digest(name.toByteArray(Charsets.UTF_8))))

    companion object {
        const val MAX_FILES = 100_000
        const val MAX_PLAN_TEXT = 16L * 1024 * 1024
        private const val STATE = "capture-v1.json"
        private const val MAX_STATE = 64 * 1024 * 1024
        private const val MAX_JSON = 16L * 1024 * 1024
        private const val MAX_CLOUD_JSON = 1024L * 1024
        private const val MAX_DATABASE = 128L * 1024 * 1024
        private const val MAX_ARCHIVE = 1024L * 1024 * 1024 * 1024
        private val DATABASES = setOf("inventory.sqlite3", "inventory.sqlite3-wal", "inventory.sqlite3-shm", "inventory.sqlite3-journal")
        private val COVERS = setOf("cover.jpg", "cover.png", "cover.gif", "cover.webp")
        private val UUID = Regex("[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
        private val HASH = Regex("[a-f0-9]{64}")
        fun source(tree: Uri): WorkingLibrarySource = try {
            if (tree.scheme != "content" || tree.authority?.matches(Regex("[A-Za-z0-9._-]+")) != true ||
                tree.toString().length > 16384 || !DocumentsContract.isTreeUri(tree)) fail(LibraryCaptureFailure.ACCESS)
            val id = DocumentsContract.getTreeDocumentId(tree)
            if (id.isBlank() || id.any { it.code < 32 }) fail(LibraryCaptureFailure.ACCESS)
            val canonical = DocumentsContract.buildTreeDocumentUri(tree.authority!!, id)
            WorkingLibrarySource(hex(MessageDigest.getInstance("SHA-256").digest(
                ("working-library/v1\u0000" + tree.authority + "\u0000" + id).toByteArray(Charsets.UTF_8))), canonical.toString())
        } catch (error: LibraryCaptureException) { throw error }
          catch (_: Exception) { fail(LibraryCaptureFailure.ACCESS) }
        private fun name(value: String) = value.isNotBlank() && value !in setOf(".", "..") &&
            value.toByteArray(Charsets.UTF_8).size <= 255 && value.none { it == '/' || it == '\\' || it.code < 32 || it.code == 127 }
        private fun relative(value: String) {
            if (value.split('/').size !in 1..8 || value.split('/').any { !name(it) }) fail(LibraryCaptureFailure.PRIVATE_COPY)
        }
        private fun valid(document: WorkingLibraryDocument, directory: Boolean) {
            if (document.id.isEmpty() || document.id.length > 16384 || document.id.any { it.code < 32 } ||
                document.mime.isBlank() || document.mime.length > 255 || document.mime.any { it.code < 32 } ||
                !name(document.name) || document.directory != directory || document.partial ||
                document.bytes != null && document.bytes < 0 || document.modified != null && document.modified < 0)
                fail(LibraryCaptureFailure.INCOMPLETE)
        }
        private fun documentSize(document: WorkingLibraryDocument) = document.id.toByteArray(Charsets.UTF_8).size.toLong() +
            document.name.toByteArray(Charsets.UTF_8).size + document.mime.toByteArray(Charsets.UTF_8).size
        private fun safe(path: File) { if (path.canonicalFile != path.absoluteFile) fail(LibraryCaptureFailure.PRIVATE_COPY) }
        private fun fact(size: Long, sha: String) = JSONObject().put("size", size).put("sha256", sha)
        private fun sameFact(a: JSONObject, b: JSONObject) = a.getLong("size") == b.getLong("size") && a.getString("sha256") == b.getString("sha256")
        private fun receiptName(path: String) = hex(MessageDigest.getInstance("SHA-256").digest(path.toByteArray(Charsets.UTF_8))) + ".json"
        private fun fields(value: JSONObject) = value.keys().asSequence().toSet()
        private fun JSONObject.string(name: String) = opt(name) as? String ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
        private fun hex(value: ByteArray) = buildString(value.size * 2) {
            val digits = "0123456789abcdef"
            for (byte in value) { val number = byte.toInt() and 255; append(digits[number ushr 4]); append(digits[number and 15]) }
        }
        private fun fail(value: LibraryCaptureFailure): Nothing = throw LibraryCaptureException(value)
        private fun <T> guarded(cancellation: LibraryCaptureCancellation, action: () -> T): T = try {
            cancellation.check(); action()
        } catch (error: LibraryCaptureException) { throw error }
          catch (_: ArithmeticException) { cancellation.check(); fail(LibraryCaptureFailure.SPACE) }
          catch (_: SecurityException) { cancellation.check(); fail(LibraryCaptureFailure.ACCESS) }
          catch (_: Exception) { cancellation.check(); fail(LibraryCaptureFailure.PRIVATE_COPY) }
    }
}
