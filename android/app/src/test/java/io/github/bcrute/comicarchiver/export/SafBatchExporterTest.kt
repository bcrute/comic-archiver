package io.github.bcrute.comicarchiver.export

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.security.MessageDigest

/**
 * Focused JVM tests for the SAF batch exporter core, over the fake seam. These prove the
 * state machine, verification and recovery behaviour only; they do not prove the real
 * ContentResolver adapter (androidTest, fleet) or compilation of the app.
 */
class SafBatchExporterTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val documents = FakeDocuments()
    private val source = FakeSource()
    private val store by lazy { FileJournalStore(java.io.File(tmp.root, "journal.txt")) }
    private val target = SafExportTarget("content://fake/tree")

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") {
            "%02x".format(it)
        }

    private fun entry(
        episodeId: String,
        bytes: ByteArray,
        seriesId: String = "Series One",
        kind: ExportKind = ExportKind.CBZ,
        filename: String = "$episodeId.${kind.extension}",
        declaredBytes: Long = bytes.size.toLong(),
        declaredSha: String = sha256(bytes),
    ) = ExportEntry(seriesId, episodeId, kind, filename, declaredBytes, declaredSha,
        relpath = "private/$seriesId/$episodeId.${kind.extension}")

    private fun exporter(store: JournalStore = this.store) =
        SafBatchExporter(source, documents, store)

    @Test
    fun `copies into a series folder and verifies size and digest`() {
        val bytes = ByteArray(150_000) { (it % 251).toByte() } // spans several chunks
        source.originals["ep1"] = bytes
        val summary = exporter().exportBatch(target, listOf(entry("ep1", bytes)))

        assertTrue(summary.complete)
        assertEquals(1, summary.verified)
        assertEquals(0, summary.failed)
        val folder = documents.docs.values.first { it.isDir && it.name == "Series One" }
        val file = documents.docs[folder.children.single()]!!
        assertEquals("ep1.cbz", file.name)
        assertTrue(bytes.contentEquals(file.bytes))
    }

    @Test
    fun `digest mismatch is failed and never reported complete`() {
        val bytes = ByteArray(10) { 1 }
        source.originals["ep1"] = bytes
        val summary = exporter().exportBatch(
            target,
            listOf(entry("ep1", bytes, declaredSha = sha256(ByteArray(10) { 2 }))),
        )

        assertFalse(summary.complete)
        assertEquals(1, summary.failed)
        val journal = store.load()
        assertEquals(JournalState.FAILED, journal.get("ep1")?.state)
        assertEquals(ExportFailure.VERIFICATION_MISMATCH, journal.get("ep1")?.failure)
        // The bad copy is cleaned up, the original is untouched.
        assertEquals(1, documents.deleted.size)
        assertTrue(source.originals["ep1"]!!.contentEquals(bytes))
    }

    @Test
    fun `size mismatch alone also fails verification`() {
        val bytes = ByteArray(10) { 3 }
        source.originals["ep1"] = bytes
        val summary = exporter().exportBatch(
            target,
            listOf(entry("ep1", bytes, declaredBytes = 11)),
        )

        assertFalse(summary.complete)
        assertEquals(ExportFailure.VERIFICATION_MISMATCH, store.load().get("ep1")?.failure)
    }

    @Test
    fun `interruption mid-copy resumes the same target document and completes`() {
        val bytes = ByteArray(200_000) { (it % 199).toByte() }
        source.originals["ep1"] = bytes
        val store = FileJournalStore(tmp.newFile("journal.txt"))
        val first = SafBatchExporter(source, documents, store)
        source.onRead = { first.requestStop() }

        val interrupted = first.exportBatch(target, listOf(entry("ep1", bytes)))
        assertFalse(interrupted.complete)
        assertTrue(interrupted.interrupted)
        val midJournal = store.load()
        assertEquals(JournalState.FAILED, midJournal.get("ep1")?.state)
        assertEquals(ExportFailure.INTERRUPTED, midJournal.get("ep1")?.failure)
        val midTarget = midJournal.get("ep1")?.targetUri
        assertTrue(midTarget != null)

        // Second run: same journal, same fake tree, no stop flag.
        source.onRead = null
        val second = SafBatchExporter(source, documents, store).exportBatch(
            target, listOf(entry("ep1", bytes)),
        )

        assertTrue(second.complete)
        assertEquals(1, second.verified)
        val done = store.load().get("ep1")!!
        assertEquals(JournalState.VERIFIED, done.state)
        // Resumed onto the SAME document: no duplicate copies in the tree.
        assertEquals(midTarget, done.targetUri)
        val folder = documents.docs.values.first { it.isDir && it.name == "Series One" }
        assertEquals(1, folder.children.size)
        assertEquals(2, done.attempt)
    }

    @Test
    fun `revoked grant at batch start is a recoverable state, not success`() {
        source.originals["ep1"] = ByteArray(5)
        documents.grantRevoked = true
        val summary = exporter().exportBatch(target, listOf(entry("ep1", ByteArray(5))))

        assertFalse(summary.complete)
        assertTrue(summary.grantLost)
        assertEquals(ExportFailure.GRANT_LOST, store.load().get("ep1")?.failure)
    }

    @Test
    fun `grant revoked mid-batch keeps earlier verified work and resumes after re-pick`() {
        val b1 = ByteArray(10) { 1 }
        val b2 = ByteArray(10) { 2 }
        source.originals["ep1"] = b1
        source.originals["ep2"] = b2
        val store = FileJournalStore(tmp.newFile("journal.txt"))
        val entries = listOf(entry("ep1", b1), entry("ep2", b2))

        val first = SafBatchExporter(source, documents, store)
        val run1 = first.exportBatch(target, entries)
        assertTrue(run1.complete)

        // Now a second series runs while the grant is revoked MID-BATCH: the first read
        // of ep3's original flips the grant, so the verification read loses it.
        val b3 = ByteArray(10) { 3 }
        source.originals["ep3"] = b3
        val e3 = entry("ep3", b3, seriesId = "Series Two")
        source.onRead = { documents.grantRevoked = true }
        val run2 = SafBatchExporter(source, documents, store).exportBatch(
            target, listOf(entry("ep1", b1), e3),
        )
        source.onRead = null
        assertFalse(run2.complete)
        assertTrue(run2.grantLost)
        assertEquals(JournalState.VERIFIED, store.load().get("ep1")?.state)
        // ep3 keeps its COPYING record with the target it had acquired.
        assertEquals(JournalState.COPYING, store.load().get("ep3")?.state)

        // Re-picked run completes the remaining entry.
        documents.grantRevoked = false
        val run3 = SafBatchExporter(source, documents, store).exportBatch(target, listOf(entry("ep1", b1), e3))
        assertTrue(run3.complete)
        assertEquals(1, run3.alreadyVerified)
        assertEquals(1, run3.verified)
        assertEquals(JournalState.VERIFIED, store.load().get("ep3")?.state)
    }

    @Test
    fun `name collision records the provider's actual name`() {
        val bytes = ByteArray(5) { 7 }
        source.originals["ep1"] = bytes
        // Pre-existing same-named file in the target series folder, as another app run
        // or a prior export left behind.
        val root = documents.rootFolder(target)
        val folder = documents.ensureSubfolder(root, "Series One")
        documents.createFile(folder, "ep1.cbz", ExportKind.CBZ.mimeType)

        val summary = exporter().exportBatch(target, listOf(entry("ep1", bytes)))

        assertTrue(summary.complete)
        val done = store.load().get("ep1")!!
        assertEquals(JournalState.VERIFIED, done.state)
        assertEquals("ep1.cbz", done.requestedName)
        assertEquals("ep1 (1).cbz", done.actualName)
        assertTrue(documents.createdNames.contains("ep1 (1).cbz"))
    }

    @Test
    fun `already verified entries are skipped, not re-copied`() {
        val bytes = ByteArray(5) { 9 }
        source.originals["ep1"] = bytes
        val store = FileJournalStore(tmp.newFile("journal.txt"))
        SafBatchExporter(source, documents, store).exportBatch(target, listOf(entry("ep1", bytes)))
        val copiedBytes = documents.docs.values.filter { !it.isDir && it.name == "ep1.cbz" }
            .mapNotNull { it.bytes }.single()

        val second = SafBatchExporter(source, documents, store).exportBatch(
            target, listOf(entry("ep1", bytes)),
        )

        assertEquals(0, second.verified)
        assertEquals(1, second.alreadyVerified)
        assertTrue(second.complete)
        // No second document was created and the copy is unchanged.
        assertEquals(1, documents.docs.values.count { !it.isDir && it.name == "ep1.cbz" })
        assertTrue(copiedBytes.contentEquals(source.originals["ep1"]))
    }

    @Test
    fun `refused original is a failed entry, never complete, original world intact`() {
        val bytes = ByteArray(5) { 4 }
        source.originals["ep1"] = bytes
        source.refuse.add("ep1")
        val summary = exporter().exportBatch(target, listOf(entry("ep1", bytes)))

        assertFalse(summary.complete)
        assertEquals(1, summary.failed)
        assertEquals(ExportFailure.SOURCE_REFUSED, store.load().get("ep1")?.failure)
    }

    @Test
    fun `write failure during copy surfaces as storage_low`() {
        val bytes = ByteArray(5) { 5 }
        source.originals["ep1"] = bytes
        documents.writeFailure = java.io.IOException("no space (fake)")
        val summary = exporter().exportBatch(target, listOf(entry("ep1", bytes)))

        assertFalse(summary.complete)
        assertEquals(ExportFailure.STORAGE_LOW, store.load().get("ep1")?.failure)
    }

    @Test
    fun `provider refusing file creation is a recoverable failure`() {
        val bytes = ByteArray(5) { 6 }
        source.originals["ep1"] = bytes
        documents.refuseCreate = true
        val summary = exporter().exportBatch(target, listOf(entry("ep1", bytes)))

        assertFalse(summary.complete)
        assertEquals(ExportFailure.TARGET_REFUSED, store.load().get("ep1")?.failure)
        assertNull(store.load().get("ep1")?.targetUri)
    }

    @Test
    fun `deleted target document is recreated rather than fatally failing`() {
        val bytes = ByteArray(5) { 8 }
        source.originals["ep1"] = bytes
        val store = FileJournalStore(tmp.newFile("journal.txt"))
        SafBatchExporter(source, documents, store).exportBatch(target, listOf(entry("ep1", bytes)))
        // Simulate the user deleting the exported copy but the journal surviving.
        val done = store.load().get("ep1")!!
        documents.delete(DocumentsFile(done.targetUri!!))

        val rerun = SafBatchExporter(source, documents, store).exportBatch(
            target, listOf(entry("ep1", bytes)),
        )

        assertTrue(rerun.complete)
        assertEquals(1, rerun.verified)
        assertNotEquals(done.targetUri, store.load().get("ep1")?.targetUri)
    }

    @Test
    fun `stale target under a revoked previous tree does not poison a re-picked run`() {
        val bytes = ByteArray(10) { 1 }
        source.originals["ep1"] = bytes
        val entries = listOf(entry("ep1", bytes))
        val store = FileJournalStore(tmp.newFile("journal.txt"))

        // Run 1 interrupted mid-copy: the journal records a target document T1.
        val first = SafBatchExporter(source, documents, store)
        source.onRead = { first.requestStop() }
        assertFalse(first.exportBatch(target, entries).complete)
        source.onRead = null
        val t1 = store.load().get("ep1")!!.targetUri!!

        // The user revokes THAT tree's grant and re-picks a folder (fresh grant, same
        // fake tree): probing the recorded T1 now reports GRANT_LOST, but the run must
        // recover by creating a fresh document instead of aborting forever.
        documents.staleUris.add(t1)
        val rerun = SafBatchExporter(source, documents, store).exportBatch(target, entries)

        assertTrue(rerun.complete)
        assertFalse(rerun.grantLost)
        assertEquals(1, rerun.verified)
        val done = store.load().get("ep1")!!
        assertEquals(JournalState.VERIFIED, done.state)
        assertNotEquals(t1, done.targetUri)
        // T1 still exists as an orphan sibling; the provider's rename kept both intact.
        assertEquals("ep1 (1).cbz", done.actualName)
    }

    @Test
    fun `a re-saved episode with new bytes is exported again, not skipped`() {
        val bytes = ByteArray(10) { 1 }
        source.originals["ep1"] = bytes
        val store = FileJournalStore(tmp.newFile("journal.txt"))
        SafBatchExporter(source, documents, store).exportBatch(target, listOf(entry("ep1", bytes)))

        // The episode is re-saved: same id, new content and digest in the manifest.
        val resaved = ByteArray(10) { 2 }
        source.originals["ep1"] = resaved
        val rerun = SafBatchExporter(source, documents, store).exportBatch(
            target, listOf(entry("ep1", resaved)),
        )

        assertTrue(rerun.complete)
        assertEquals(1, rerun.verified)
        assertEquals(0, rerun.alreadyVerified)
        val done = store.load().get("ep1")!!
        assertEquals(sha256(resaved), done.sha256)
        // Same episode, same file: the copy is rewritten in place, not duplicated.
        assertEquals("ep1.cbz", done.actualName)
        val copied = documents.docs.values.first { !it.isDir && it.name == "ep1.cbz" }
        assertTrue(resaved.contentEquals(copied.bytes))
    }

    @Test
    fun `entry whose filename disagrees with its kind is manifest-invalid`() {
        val bytes = ByteArray(5) { 2 }
        source.originals["ep1"] = bytes
        val bad = entry("ep1", bytes, filename = "notes.txt")
        val summary = exporter().exportBatch(target, listOf(bad))

        assertFalse(summary.complete)
        assertEquals(ExportFailure.MANIFEST_INVALID, store.load().get("ep1")?.failure)
    }

    @Test
    fun `novels export as EPUB through the same path`() {
        val epubBytes = ByteArray(1000) { (it % 97).toByte() }
        source.originals["n1"] = epubBytes
        val novel = entry("n1", epubBytes, kind = ExportKind.EPUB, seriesId = "Novel One")
        val summary = exporter().exportBatch(target, listOf(novel))

        assertTrue(summary.complete)
        val done = store.load().get("n1")!!
        assertEquals(ExportKind.EPUB, done.kind)
        assertEquals("n1.epub", done.actualName)
    }

    @Test
    fun `multiple series land in separate folders`() {
        val a = ByteArray(5) { 1 }
        val b = ByteArray(5) { 2 }
        source.originals["epA"] = a
        source.originals["epB"] = b
        val summary = exporter().exportBatch(
            target,
            listOf(entry("epA", a, seriesId = "S1"), entry("epB", b, seriesId = "S2")),
        )

        assertTrue(summary.complete)
        assertEquals(2, summary.verified)
        assertEquals(
            setOf("S1", "S2"),
            documents.docs.values.filter { it.isDir && it.name.startsWith("S") }
                .map { it.name }.toSet(),
        )
    }
}
