package io.github.bcrute.comicarchiver.export

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/**
 * In-memory fake of the SAF seam for JVM tests: documents are URI-keyed byte buffers,
 * providers rename on collision (as real SAF does), and grant revocation, refusals and
 * write failures are injectable. No Android framework dependency.
 */
class FakeDocuments : DocumentsAdapter {

    class Doc(
        val uri: String,
        var name: String,
        val mime: String,
        val isDir: Boolean,
        var parentUri: String?,
    ) {
        var bytes: ByteArray? = if (isDir) null else ByteArray(0)
        val children = ArrayList<String>()
    }

    val docs = LinkedHashMap<String, Doc>()
    val deleted = mutableListOf<String>()
    val createdNames = mutableListOf<String>()

    var grantRevoked = false
    var refuseCreate = false
    var writeFailure: IOException? = null

    /** Hook invoked on every source-read call, so tests can flip the stop flag mid-copy. */
    var onSourceRead: (() -> Unit)? = null

    private var nextId = 1
    private val rootUri = "content://fake/tree/root"

    fun grantLost() = DocumentsException(DocumentsCode.GRANT_LOST, "grant revoked (fake)")

    private fun checkGrant() {
        if (grantRevoked) throw grantLost()
    }

    private fun newUri(): String = "content://fake/tree/doc-${nextId++}"

    private fun doc(uri: String): Doc =
        docs[uri] ?: throw DocumentsException(DocumentsCode.NOT_FOUND, "no doc $uri")

    override fun rootFolder(target: SafExportTarget): DocumentsFolder {
        checkGrant()
        if (rootUri !in docs) {
            docs[rootUri] = Doc(rootUri, "Export", "vnd.android.document/directory", true, null)
        }
        return DocumentsFolder(rootUri)
    }

    override fun ensureSubfolder(parent: DocumentsFolder, displayName: String): DocumentsFolder {
        checkGrant()
        val parentDoc = doc(parent.uriString)
        if (!parentDoc.isDir) throw DocumentsException(DocumentsCode.TARGET_REFUSED, "not a dir")
        parentDoc.children.map { docs[it]!! }.firstOrNull {
            it.isDir && it.name == displayName
        }?.let { return DocumentsFolder(it.uri) }
        return DocumentsFolder(putDoc(parent.uriString, displayName, "vnd.android.document/directory"))
    }

    override fun createFile(
        parent: DocumentsFolder,
        displayName: String,
        mimeType: String,
    ): DocumentsFile {
        checkGrant()
        if (refuseCreate) {
            throw DocumentsException(DocumentsCode.TARGET_REFUSED, "create refused (fake)")
        }
        // Real providers never overwrite: a same-named sibling forces a unique name.
        val parentDoc = doc(parent.uriString)
        val siblings = parentDoc.children.map { docs[it]!! }
        var finalName = displayName
        if (siblings.any { !it.isDir && it.name == finalName }) {
            val dot = displayName.lastIndexOf('.')
            var n = 1
            while (siblings.any { !it.isDir && it.name == finalName }) {
                finalName = if (dot > 0) {
                    displayName.substring(0, dot) + " ($n)" + displayName.substring(dot)
                } else {
                    "$displayName ($n)"
                }
                n++
            }
        }
        createdNames.add(finalName)
        return DocumentsFile(putDoc(parent.uriString, finalName, mimeType))
    }

    override fun displayNameFor(file: DocumentsFile): String {
        checkGrant()
        return doc(file.uriString).name
    }

    override fun sizeBytes(file: DocumentsFile): Long {
        checkGrant()
        return doc(file.uriString).bytes?.size?.toLong() ?: -1L
    }

    override fun openOutput(file: DocumentsFile): OutputStream {
        checkGrant()
        doc(file.uriString) // NOT_FOUND if absent
        return object : ByteArrayOutputStream() {
            override fun write(b: ByteArray, off: Int, len: Int) {
                writeFailure?.let { throw it }
                super.write(b, off, len)
            }

            override fun close() {
                docs[file.uriString]?.bytes = toByteArray()
                super.close()
            }
        }
    }

    override fun openInput(file: DocumentsFile): InputStream {
        checkGrant()
        val bytes = doc(file.uriString).bytes ?: ByteArray(0)
        return ByteArrayInputStream(bytes)
    }

    override fun delete(file: DocumentsFile): Boolean {
        if (grantRevoked) return false
        val d = docs[file.uriString] ?: return false
        docs.remove(file.uriString)
        deleted.add(file.uriString)
        d.parentUri?.let { docs[it]?.children?.remove(file.uriString) }
        return true
    }

    private fun putDoc(parentUri: String, name: String, mime: String): String {
        val uri = newUri()
        docs[uri] = Doc(uri, name, mime, mime == "vnd.android.document/directory", parentUri)
        docs[parentUri]?.children?.add(uri)
        return uri
    }
}

/** In-memory original store: episodeId -> bytes, with optional read-time failure. */
class FakeSource : ExportContentSource {
    val originals = LinkedHashMap<String, ByteArray>()
    var refuse: MutableSet<String> = HashSet()
    var onRead: (() -> Unit)? = null

    override fun openStream(entry: ExportEntry): InputStream {
        if (entry.episodeId in refuse) throw SourceRefusedException("refused (fake)")
        val bytes = originals[entry.episodeId] ?: throw SourceRefusedException("missing (fake)")
        return object : ByteArrayInputStream(bytes) {
            override fun read(b: ByteArray, off: Int, len: Int): Int {
                onRead?.invoke()
                return super.read(b, off, len)
            }
        }
    }
}

/** In-memory journal store for tests that do not exercise the file format. */
class MemJournalStore : JournalStore {
    var journal: ExportJournal = ExportJournal.empty()
        private set

    override fun load(): ExportJournal = journal

    override fun save(journal: ExportJournal) {
        this.journal = ExportJournal.of(journal.all())
    }
}
