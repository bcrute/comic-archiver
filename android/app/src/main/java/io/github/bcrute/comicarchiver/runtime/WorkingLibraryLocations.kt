package io.github.bcrute.comicarchiver.runtime

import android.net.Uri
import android.provider.DocumentsContract
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.util.AtomicFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/** Private picker authority. Neither the URI nor the source identity belongs in UI/log projections. */
internal data class WorkingLibrarySource(val key: String, val tree: String) {
    override fun toString() = "WorkingLibrarySource(private)"
}

internal data class WorkingLibraryLocation(val id: String, val label: String,
    val source: WorkingLibrarySource? = null)

internal data class PendingWorkingLibrary(val id: String, val label: String,
    val source: WorkingLibrarySource, val digest: String? = null)

internal sealed interface WorkingLibraryStart {
    data class Existing(val library: WorkingLibraryLocation) : WorkingLibraryStart
    data class Copy(val pending: PendingWorkingLibrary) : WorkingLibraryStart
}

internal enum class WorkingLibraryFailure(val words: String) {
    UNAVAILABLE("The working library could not be opened. The previous library is retained."),
    CORRUPT("The saved library choices could not be read. Existing files are retained."),
    BUSY("Finish or cancel the library copy before choosing another folder."),
    STALE("This library choice has changed. Choose the folder again."),
    NOT_READY("The library copy has not finished verification. The previous library is retained."),
}

internal class WorkingLibraryException(val failure: WorkingLibraryFailure) : IllegalStateException(failure.words)

/**
 * Immutable app-private working roots. The legacy workspace and its old URI grants never move.
 *
 * This is selection/storage bookkeeping, not a source copier or an archive verifier. The owning
 * import coordinator must stop its copy before cancel(), and call verified() only after the
 * common-core snapshot verifier succeeds. complete() makes a verified copy selectable; only
 * the controller, after worker/browser/export/cloud drain and workspace preparation, selects it.
 * No method starts a worker, changes a provider's cookies or writes to the picked documents.
 */
internal class WorkingLibraryLocations(filesDir: File) {
    private val owner = filesDir.canonicalFile
    private val namespace = File(owner, "working-libraries")
    private val staging = File(namespace, ".staging")
    private val file = File(owner, "library-locations.json")
    private val atomic = AtomicFile(file)
    private val gate = gates.getOrPut(owner.path) { Any() }

    private data class Snapshot(val active: String = DEFAULT,
        val libraries: List<WorkingLibraryLocation> = listOf(WorkingLibraryLocation(DEFAULT, "This device")),
        val pending: PendingWorkingLibrary? = null)

    fun libraries(): List<WorkingLibraryLocation> = locked { read().libraries.toList() }

    fun pending(): PendingWorkingLibrary? = locked { read().pending }

    fun current(): WorkingLibraryLocation = locked {
        val state = read()
        val chosen = state.libraries.single { it.id == state.active }
        existingRoot(chosen)
        chosen
    }

    fun root(id: String): File = locked {
        val location = read().libraries.singleOrNull { it.id == id } ?: fail(WorkingLibraryFailure.STALE)
        existingRoot(location)
    }

    /** A repeated source choice reopens its existing private root; it never silently recopies it. */
    fun begin(label: String, source: WorkingLibrarySource): WorkingLibraryStart = locked {
        validLabel(label); validSource(source)
        val state = read()
        if (state.pending != null) fail(WorkingLibraryFailure.BUSY)
        state.libraries.singleOrNull { it.source?.key == source.key }?.let {
            return@locked WorkingLibraryStart.Existing(it)
        }
        if (state.libraries.size >= MAX_LIBRARIES) fail(WorkingLibraryFailure.UNAVAILABLE)
        val pending = PendingWorkingLibrary(UUID.randomUUID().toString(), label, source)
        val stage = stageRoot(pending.id)
        val payload = File(stage, "payload")
        checkNewRoot(pending.id)
        mkdirs(payload)
        try { write(state.copy(pending = pending)) }
        catch (error: Exception) {
            removeTree(stage)
            throw error
        }
        WorkingLibraryStart.Copy(pending)
    }

    /** Native streams go only into this reserved private directory, never into an active root. */
    fun copyRoot(ticket: String): File = locked {
        val pending = ticket(read(), ticket)
        if (pending.digest != null) fail(WorkingLibraryFailure.NOT_READY)
        val payload = File(stageRoot(ticket), "payload")
        if (!payload.isDirectory) fail(WorkingLibraryFailure.UNAVAILABLE)
        safe(payload)
        payload
    }

    /** Captured documents/journals stay separate from the verifier's empty output payload. */
    fun captureRoot(ticket: String): File = locked {
        val pending = ticket(read(), ticket)
        if (pending.digest != null) fail(WorkingLibraryFailure.NOT_READY)
        val root = File(stageRoot(ticket), "capture")
        safe(root)
        mkdirs(root)
        root
    }

    /** The digest identifies the verified import snapshot, not every future change to the library. */
    fun verified(ticket: String, digest: String) = locked {
        if (!HASH.matches(digest)) fail(WorkingLibraryFailure.NOT_READY)
        val state = read()
        val pending = ticket(state, ticket)
        if (pending.digest != null && pending.digest != digest) fail(WorkingLibraryFailure.STALE)
        val payload = File(stageRoot(ticket), "payload")
        safe(payload)
        if (!payload.isDirectory) fail(WorkingLibraryFailure.UNAVAILABLE)
        val marker = JSONObject().put("v", 1).put("id", ticket)
            .put("source", pending.source.key).put("digest", digest)
        writeAtomic(File(payload, MARKER), marker.toString().toByteArray(Charsets.UTF_8))
        write(state.copy(pending = pending.copy(digest = digest)))
    }

    /**
     * Rename is private/local. A crash after rename but before the registry write leaves a ready
     * pending choice; retry completes the same root without another source read or activation.
     */
    fun complete(ticket: String): WorkingLibraryLocation = locked {
        val state = read()
        val pending = ticket(state, ticket)
        if (pending.digest == null) fail(WorkingLibraryFailure.NOT_READY)
        val stage = stageRoot(ticket)
        val payload = File(stage, "payload")
        val target = locationRoot(ticket)
        if (target.exists()) {
            if (payload.exists()) fail(WorkingLibraryFailure.STALE)
            marker(target, pending.id, pending.source.key, pending.digest)
        } else {
            marker(payload, pending.id, pending.source.key, pending.digest)
            if (!payload.renameTo(target)) fail(WorkingLibraryFailure.UNAVAILABLE)
        }
        val location = WorkingLibraryLocation(pending.id, pending.label, pending.source)
        write(state.copy(libraries = state.libraries + location, pending = null))
        // Captured input is disposable after the verified payload commits. Cleanup cannot undo that commit.
        try { removeTree(stage) } catch (_: Exception) { }
        location
    }

    /** Old libraries and source documents are retained. This removes only this uncommitted copy. */
    fun cancel(ticket: String) = locked {
        val state = read()
        val pending = ticket(state, ticket)
        val target = locationRoot(ticket)
        if (target.exists()) {
            val digest = pending.digest ?: fail(WorkingLibraryFailure.STALE)
            marker(target, ticket, pending.source.key, digest)
            removeTree(target)
        }
        removeTree(stageRoot(ticket))
        write(state.copy(pending = null))
    }

    /** The caller owns busy checks, full workspace preparation and rollback around this commit. */
    fun select(id: String): WorkingLibraryLocation = locked {
        val state = read()
        if (state.pending != null) fail(WorkingLibraryFailure.BUSY)
        val location = state.libraries.singleOrNull { it.id == id } ?: fail(WorkingLibraryFailure.STALE)
        existingRoot(location)
        write(state.copy(active = id))
        location
    }

    private fun ticket(state: Snapshot, id: String): PendingWorkingLibrary {
        if (!ID.matches(id)) fail(WorkingLibraryFailure.STALE)
        return state.pending?.takeIf { it.id == id } ?: fail(WorkingLibraryFailure.STALE)
    }

    private fun existingRoot(location: WorkingLibraryLocation): File {
        val root = locationRoot(location.id)
        safe(root)
        if (location.id != DEFAULT) {
            if (!root.isDirectory) fail(WorkingLibraryFailure.UNAVAILABLE)
            marker(root, location.id, requireNotNull(location.source).key)
        }
        return root
    }

    private fun locationRoot(id: String): File {
        if (id == DEFAULT) return File(owner, "workspace")
        if (!ID.matches(id)) fail(WorkingLibraryFailure.STALE)
        safe(namespace)
        return File(namespace, id)
    }

    private fun stageRoot(id: String): File {
        if (!ID.matches(id)) fail(WorkingLibraryFailure.STALE)
        safe(staging)
        return File(staging, id).also(::safe)
    }

    private fun checkNewRoot(id: String) {
        if (locationRoot(id).exists() || stageRoot(id).exists()) fail(WorkingLibraryFailure.STALE)
    }

    private fun marker(root: File, id: String, source: String, digest: String? = null) {
        safe(root)
        val value = json(File(root, MARKER), 4096)
        if (value.fields() != setOf("v", "id", "source", "digest") || value.opt("v") != 1 ||
            value.string("id") != id || value.string("source") != source ||
            !HASH.matches(value.string("digest")) || digest != null && value.string("digest") != digest)
            fail(WorkingLibraryFailure.CORRUPT)
    }

    private fun read(): Snapshot {
        safe(file); safe(File(file.path + ".bak")); safe(File(file.path + ".new"))
        if (!file.exists() && !File(file.path + ".bak").exists()) return Snapshot()
        val value = try { atomic.openRead().use { JSONObject(readBounded(it, MAX_REGISTRY).toString(Charsets.UTF_8)) } }
            catch (_: Exception) { fail(WorkingLibraryFailure.CORRUPT) }
        try {
            if (value.fields() != setOf("v", "active", "libraries", "pending") || value.opt("v") != 1)
                fail(WorkingLibraryFailure.CORRUPT)
            val array = value.getJSONArray("libraries")
            if (array.length() !in 1..MAX_LIBRARIES) fail(WorkingLibraryFailure.CORRUPT)
            val entries = (0 until array.length()).map { index ->
                val entry = array.getJSONObject(index)
                if (entry.fields() != setOf("id", "label", "source")) fail(WorkingLibraryFailure.CORRUPT)
                val id = entry.string("id"); val label = entry.string("label")
                val source = if (entry.isNull("source")) null else source(entry.getJSONObject("source"))
                validLabel(label)
                if (id == DEFAULT) {
                    if (source != null) fail(WorkingLibraryFailure.CORRUPT)
                } else if (!ID.matches(id) || source == null) fail(WorkingLibraryFailure.CORRUPT)
                WorkingLibraryLocation(id, label, source)
            }
            if (entries.count { it.id == DEFAULT } != 1 || entries.map { it.id }.distinct().size != entries.size ||
                entries.mapNotNull { it.source?.key }.distinct().size != entries.count { it.source != null })
                fail(WorkingLibraryFailure.CORRUPT)
            val active = value.string("active")
            if (entries.none { it.id == active }) fail(WorkingLibraryFailure.CORRUPT)
            val pending = if (value.isNull("pending")) null else {
                val entry = value.getJSONObject("pending")
                if (entry.fields() != setOf("id", "label", "source", "digest")) fail(WorkingLibraryFailure.CORRUPT)
                val id = entry.string("id"); val label = entry.string("label")
                val source = source(entry.getJSONObject("source"))
                val digest = if (entry.isNull("digest")) null else entry.string("digest")
                validLabel(label)
                if (!ID.matches(id) || entries.any { it.id == id || it.source?.key == source.key } ||
                    digest != null && !HASH.matches(digest)) fail(WorkingLibraryFailure.CORRUPT)
                PendingWorkingLibrary(id, label, source, digest)
            }
            return Snapshot(active, entries, pending)
        } catch (error: WorkingLibraryException) { throw error }
          catch (_: Exception) { fail(WorkingLibraryFailure.CORRUPT) }
    }

    private fun source(value: JSONObject): WorkingLibrarySource {
        if (value.fields() != setOf("key", "tree")) fail(WorkingLibraryFailure.CORRUPT)
        return WorkingLibrarySource(value.string("key"), value.string("tree")).also(::validSource)
    }

    private fun write(state: Snapshot) {
        fun source(value: WorkingLibrarySource) = JSONObject().put("key", value.key).put("tree", value.tree)
        val entries = JSONArray()
        state.libraries.forEach { entries.put(JSONObject().put("id", it.id).put("label", it.label)
            .put("source", it.source?.let(::source) ?: JSONObject.NULL)) }
        val pending = state.pending?.let { JSONObject().put("id", it.id).put("label", it.label)
            .put("source", source(it.source)).put("digest", it.digest ?: JSONObject.NULL) } ?: JSONObject.NULL
        val value = JSONObject().put("v", 1).put("active", state.active).put("libraries", entries).put("pending", pending)
        writeAtomic(file, value.toString().toByteArray(Charsets.UTF_8))
    }

    private fun writeAtomic(target: File, data: ByteArray) {
        if (data.size > MAX_REGISTRY) fail(WorkingLibraryFailure.UNAVAILABLE)
        safe(target); safe(File(target.path + ".bak")); safe(File(target.path + ".new"))
        val atomic = AtomicFile(target)
        val stream = atomic.startWrite()
        try { Os.fchmod(stream.fd, PRIVATE_FILE); stream.write(data); atomic.finishWrite(stream) }
        catch (_: Exception) { atomic.failWrite(stream); fail(WorkingLibraryFailure.UNAVAILABLE) }
    }

    private fun json(path: File, maximum: Int): JSONObject {
        safe(path)
        return try { path.inputStream().use { JSONObject(readBounded(it, maximum).toString(Charsets.UTF_8)) } }
            catch (_: Exception) { fail(WorkingLibraryFailure.CORRUPT) }
    }

    private fun safe(path: File) {
        val absolute = path.absoluteFile
        if (path.canonicalFile != absolute ||
            absolute != owner && !absolute.path.startsWith(owner.path + File.separator))
            fail(WorkingLibraryFailure.CORRUPT)
    }

    private fun mkdirs(path: File) {
        safe(path)
        if (!path.isDirectory && !path.mkdirs()) fail(WorkingLibraryFailure.UNAVAILABLE)
        var current = path
        while (current != owner) {
            Os.chmod(current.path, PRIVATE_DIRECTORY)
            current = current.parentFile ?: fail(WorkingLibraryFailure.CORRUPT)
        }
    }

    private fun removeTree(path: File) {
        // lstat treats even a dangling link as a leaf. Never recurse into a picked-source link.
        val info = try { Os.lstat(path.path) } catch (error: ErrnoException) {
            if (error.errno == OsConstants.ENOENT) return
            fail(WorkingLibraryFailure.UNAVAILABLE)
        }
        if (OsConstants.S_ISDIR(info.st_mode)) {
            safe(path)
            val children = path.listFiles() ?: fail(WorkingLibraryFailure.UNAVAILABLE)
            children.forEach(::removeTree)
        }
        if (!path.delete()) fail(WorkingLibraryFailure.UNAVAILABLE)
    }

    private fun <T> locked(action: () -> T): T = synchronized(gate) {
        try {
            safe(owner)
            val lock = File(owner, "library-locations.lock").also(::safe)
            FileOutputStream(lock, true).channel.use { channel ->
                Os.chmod(lock.path, PRIVATE_FILE)
                channel.lock().use { action() }
            }
        } catch (error: WorkingLibraryException) { throw error }
          catch (_: Exception) { fail(WorkingLibraryFailure.UNAVAILABLE) }
    }

    companion object {
        const val DEFAULT = "default"
        private const val MARKER = ".working-library.json"
        private const val MAX_LIBRARIES = 1024
        private const val MAX_REGISTRY = 4 * 1024 * 1024
        private const val PRIVATE_FILE = 384 // 0600
        private const val PRIVATE_DIRECTORY = 448 // 0700
        private val ID = Regex("[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")
        private val HASH = Regex("[a-f0-9]{64}")
        private val gates = ConcurrentHashMap<String, Any>()

        private fun fail(value: WorkingLibraryFailure): Nothing = throw WorkingLibraryException(value)
        private fun validLabel(value: String) {
            if (value.isBlank() || value.length > 120 || value.any { it.code < 32 || it.code == 127 })
                fail(WorkingLibraryFailure.CORRUPT)
        }
        private fun validSource(value: WorkingLibrarySource) {
            val uri = try { Uri.parse(value.tree) } catch (_: Exception) { fail(WorkingLibraryFailure.CORRUPT) }
            if (!HASH.matches(value.key) || value.tree.length > 16384 || value.tree.any { it.code < 32 } ||
                uri.scheme != "content" || uri.authority?.matches(Regex("[A-Za-z0-9._-]+")) != true ||
                !DocumentsContract.isTreeUri(uri)) fail(WorkingLibraryFailure.CORRUPT)
        }
        private fun JSONObject.fields() = keys().asSequence().toSet()
        private fun JSONObject.string(name: String) = opt(name) as? String ?: fail(WorkingLibraryFailure.CORRUPT)
        private fun readBounded(input: java.io.InputStream, maximum: Int): ByteArray {
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) return output.toByteArray()
                if (output.size().toLong() + count > maximum) fail(WorkingLibraryFailure.CORRUPT)
                output.write(buffer, 0, count)
            }
        }
    }
}
