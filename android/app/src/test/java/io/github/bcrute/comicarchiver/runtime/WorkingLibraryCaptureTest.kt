package io.github.bcrute.comicarchiver.runtime

import android.app.Application
import android.net.Uri
import android.provider.DocumentsContract.Document
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream
import java.nio.file.Files

/** Capture fixtures deliberately are not archives. Core verification and system SAF journeys are independent gates. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], shadows = [WorkingLibraryFileSystemShadow::class])
class WorkingLibraryCaptureTest {
    @get:Rule val temporary = TemporaryFolder()
    private val tree = Uri.parse("content://fixture.documents/tree/root")
    private val mappings = linkedMapOf("tapas" to "", "webtoon" to "Webtoon", "tappytoon" to "Tappytoon")
    private val provenance = "11111111-1111-4111-8111-111111111111"
    private val targetId = "22222222-2222-4222-8222-222222222222"

    private class Documents : WorkingLibraryDocuments {
        val rootId = "root"
        val records = mutableMapOf(rootId to WorkingLibraryDocument(rootId, "Picked folder", Document.MIME_TYPE_DIR, null, null))
        val children = mutableMapOf(rootId to mutableListOf<String>())
        val bytes = mutableMapOf<String, ByteArray>()
        val opens = mutableMapOf<String, Int>()
        val closes = mutableMapOf<String, Int>()
        var available = true
        var incomplete = false
        var onOpen: (String, Int) -> Unit = { _, _ -> }
        private var sequence = 0
        fun folder(parent: String, name: String): String {
            val id = "document-${++sequence}"
            records[id] = WorkingLibraryDocument(id, name, Document.MIME_TYPE_DIR, null, null)
            children[id] = mutableListOf(); children.getValue(parent).add(id)
            return id
        }
        fun file(parent: String, name: String, data: ByteArray = "capture fixture".toByteArray(), unknown: Boolean = false): String {
            val id = "document-${++sequence}"
            records[id] = WorkingLibraryDocument(id, name, "application/octet-stream", if (unknown) null else data.size.toLong(),
                if (unknown) null else 1_700_000_000_001L)
            bytes[id] = data.copyOf(); children.getValue(parent).add(id)
            return id
        }
        private fun access(cancellation: LibraryCaptureCancellation) {
            cancellation.check()
            if (!available) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS)
        }
        override fun retain(tree: Uri) { if (!available) throw LibraryCaptureException(LibraryCaptureFailure.ACCESS) }
        override fun readable(tree: Uri) = available
        override fun root(tree: Uri, cancellation: LibraryCaptureCancellation): WorkingLibraryDocument {
            access(cancellation); return records.getValue(rootId)
        }
        override fun children(tree: Uri, parent: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): List<WorkingLibraryDocument> {
            access(cancellation)
            if (incomplete) throw LibraryCaptureException(LibraryCaptureFailure.INCOMPLETE)
            return children.getValue(parent.id).map { records.getValue(it) }
        }
        override fun current(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): Boolean {
            access(cancellation); return records[document.id] == document
        }
        override fun open(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): InputStream {
            access(cancellation)
            val count = (opens[document.id] ?: 0) + 1; opens[document.id] = count
            onOpen(document.id, count)
            return object : ByteArrayInputStream(bytes.getValue(document.id)) {
                private var closed = false
                override fun close() {
                    if (!closed) { closes[document.id] = (closes[document.id] ?: 0) + 1; closed = true }
                    super.close()
                }
            }
        }
    }

    private data class Fixture(val documents: Documents, val library: String, val series: String,
        val metadata: String, val archive: String)
    private fun fixture(unknown: Boolean = false, large: Boolean = false): Fixture {
        val documents = Documents()
        val library = documents.folder(documents.rootId, "Library")
        val series = documents.folder(library, "Series")
        val metadata = documents.file(series, "a.json")
        val archive = documents.file(series, "b.cbz", if (large) ByteArray(256 * 1024) { (it % 127).toByte() }
            else "capture bytes, not a verified CBZ".toByteArray(), unknown)
        return Fixture(documents, library, series, metadata, archive)
    }
    private fun capturer(documents: Documents, free: (File) -> Long = { Long.MAX_VALUE / 4 }, reserve: Long = 128) =
        WorkingLibraryCapture(documents, mappings, free, reserve)
    private fun root() = temporary.newFolder().canonicalFile.resolve("capture")
    private fun failure(expected: LibraryCaptureFailure, action: () -> Unit) {
        try { action(); fail("Expected a fixed library capture refusal") }
        catch (error: LibraryCaptureException) {
            assertEquals(expected, error.failure)
            assertEquals(expected.words, error.message)
            assertFalse(error.message.orEmpty().contains("PRIVATE"))
        }
    }
    private fun stable(root: File) = JSONObject(root.resolve("capture-v1.json").readText()).getBoolean("stable")

    @Test fun portableReadingRecordsRemainExactWithoutWriterLocksOrUncommittedRecords() {
        val f = fixture()
        val state = f.documents.folder(f.library, ".archive-state")
        val reading = f.documents.folder(state, "reading")
        val name = "a".repeat(64) + ".json"
        val bytes = """{"v":99,"future":"retained"}""".toByteArray()
        val record = f.documents.file(reading, name, bytes)
        val lock = f.documents.file(reading, "writer.lock", "PRIVATE lock".toByteArray())
        val partial = f.documents.file(reading, "$name.tmp", "PARTIAL".toByteArray())
        val capture = capturer(f.documents)
        val plan = capture.scan(tree, "tapas")
        val result = capture.capture(plan, root())
        assertArrayEquals(bytes, result.workspace.resolve("Library/.archive-state/reading/$name").readBytes())
        assertEquals(2, f.documents.opens[record])
        assertFalse(f.documents.opens.containsKey(lock))
        assertFalse(f.documents.opens.containsKey(partial))
        assertFalse(result.workspace.resolve("Library/.archive-state/reading/writer.lock").exists())
        assertFalse(result.workspace.resolve("Library/.archive-state/reading/$name.tmp").exists())
    }

    @Test fun oversizedAndForeignReadingRecordsCannotEnterTheCapturedLibrary() {
        for (oversized in listOf(true, false)) {
            val f = fixture()
            val reading = f.documents.folder(f.documents.folder(f.library, ".archive-state"), "reading")
            f.documents.file(reading, if (oversized) "b".repeat(64) + ".json" else "PRIVATE-secret.data",
                if (oversized) ByteArray(64 * 1024 + 1) else "private".toByteArray())
            failure(LibraryCaptureFailure.INCOMPLETE) { capturer(f.documents).scan(tree, "tapas") }
            assertTrue(f.documents.opens.isEmpty())
        }
    }

    @Test fun onlyWhitelistedDataAndPrivateProvenanceAreCapturedWithTwoSourceReads() {
        val f = fixture()
        val state = f.documents.folder(f.library, ".archive-state")
        f.documents.file(state, "inventory.sqlite3"); f.documents.file(state, "inventory.sqlite3-wal")
        f.documents.file(state, "inventory.sqlite3-shm")
        val provenanceFolder = f.documents.folder(state, "provenance")
        f.documents.file(provenanceFolder, "$provenance.json")
        val cloud = f.documents.folder(state, "cloud")
        f.documents.file(cloud, "targets.json")
        val history = f.documents.folder(f.documents.folder(f.documents.folder(cloud, "history"), "b2"), provenance)
        f.documents.file(history, "$targetId-${"a".repeat(64)}.json")
        val sourceSession = f.documents.folder(f.documents.rootId, ".comic-session")
        val cookie = f.documents.file(sourceSession, "cookies.json", "PRIVATE_COOKIE".toByteArray())
        val credential = f.documents.file(f.documents.folder(cloud, "b2"), "credentials.json", "PRIVATE_KEY".toByteArray())
        val queue = f.documents.file(state, "tasks.json", "PRIVATE_QUEUE".toByteArray())
        val shared = f.documents.file(state, "shared.json", "PRIVATE_LINK".toByteArray())
        val cache = f.documents.file(f.documents.folder(f.library, ".archive-cache"), "index.sqlite3")
        val before = f.documents.bytes.mapValues { it.value.toList() }
        val capture = capturer(f.documents)
        val plan = capture.scan(tree, "tapas")
        val progress = mutableListOf<WorkingCaptureProgress>()
        val root = root()
        val result = capture.capture(plan, root, progress = { progress.add(it) })
        for (file in plan.files) {
            assertArrayEquals(f.documents.bytes.getValue(file.document.id), result.workspace.resolve(file.path).readBytes())
            assertEquals(2, f.documents.opens[file.document.id])
            assertEquals(2, f.documents.closes[file.document.id])
        }
        for (id in listOf(cookie, credential, queue, shared, cache)) assertFalse(f.documents.opens.containsKey(id))
        assertEquals(before, f.documents.bytes.mapValues { it.value.toList() })
        assertEquals(WorkingCapturePhase.CAPTURED, progress.last().phase)
        assertTrue(progress.map { it.phase }.containsAll(WorkingCapturePhase.entries))
        assertEquals(result.bytes, progress.last().bytesRead)
        assertTrue(stable(root))
        assertFalse(result.workspace.resolve(".working-library.json").exists())
        assertFalse(result.workspace.resolve("library-locations.json").exists())
    }

    @Test fun projectProviderPathsUseTheCoreMappingsAndAChosenLibraryUsesItsDeclaredSite() {
        val f = fixture()
        val webtoon = f.documents.folder(f.documents.rootId, "Webtoon")
        val library = f.documents.folder(webtoon, "Library")
        f.documents.file(f.documents.folder(library, "Other series"), "book.epub")
        val capture = capturer(f.documents)
        val plan = capture.scan(tree, "tapas")
        assertTrue(plan.files.any { it.path == "Webtoon/Library/Other series/book.epub" })
        val result = capture.capture(plan, root())
        assertTrue(result.workspace.resolve("Library/Series/b.cbz").isFile)
        assertTrue(result.workspace.resolve("Webtoon/Library/Other series/book.epub").isFile)
        val raw = Documents()
        raw.file(raw.folder(raw.rootId, "Series"), "a.cbz")
        val rawCapture = capturer(raw)
        val rawPlan = rawCapture.scan(tree, "tappytoon")
        assertEquals(listOf("Tappytoon/Library/Series/a.cbz"), rawPlan.files.map { it.path })
        assertTrue(rawCapture.capture(rawPlan, root()).workspace.resolve("Tappytoon/Library/Series/a.cbz").isFile)
    }

    @Test fun anExplicitEmptyLocationHasACapturedEmptyLibraryAndNoWorkerOrSignInState() {
        val documents = Documents(); val capture = capturer(documents)
        val plan = capture.scan(tree, "tapas")
        assertTrue(plan.files.isEmpty()); assertEquals(setOf("Library"), plan.folders.keys)
        val result = capture.capture(plan, root())
        assertEquals(0L, result.bytes)
        assertTrue(result.workspace.resolve("Library").isDirectory)
        assertFalse(result.workspace.resolve(".comic-session").exists())
    }

    @Test fun aSameSizeSourceMutationWithUnknownTimestampsIsCaughtByTheSecondRead() {
        val f = fixture(unknown = true)
        f.documents.onOpen = { id, count -> if (id == f.archive && count == 2) {
            f.documents.bytes[id] = f.documents.bytes.getValue(id).map { (it.toInt() xor 1).toByte() }.toByteArray()
        } }
        val capture = capturer(f.documents); val root = root()
        failure(LibraryCaptureFailure.CHANGED) { capture.capture(capture.scan(tree, "tapas"), root) }
        assertFalse(stable(root))
        assertNull(capture.recover(root, WorkingLibraryCapture.source(tree)))
    }

    @Test fun filesAddedDuringTheSecondPassCannotBecomeAnIncompleteSuccess() {
        val f = fixture()
        f.documents.onOpen = { id, count -> if (id == f.archive && count == 2) {
            f.documents.file(f.series, "new.cbz")
        } }
        val capture = capturer(f.documents); val root = root()
        failure(LibraryCaptureFailure.CHANGED) { capture.capture(capture.scan(tree, "tapas"), root) }
        assertFalse(stable(root))
    }

    @Test fun aStalePreviewOrLostGrantRefusesBeforeReadingSourceFiles() {
        val f = fixture(); val capture = capturer(f.documents)
        val plan = capture.scan(tree, "tapas")
        val old = f.documents.records.getValue(f.archive)
        f.documents.records[f.archive] = old.copy(modified = old.modified!! + 1)
        failure(LibraryCaptureFailure.CHANGED) { capture.capture(plan, root()) }
        assertTrue(f.documents.opens.isEmpty())
        f.documents.available = false
        failure(LibraryCaptureFailure.ACCESS) { capture.capture(plan, root()) }
    }

    @Test fun partialVirtualDuplicateAndIncompleteProviderListingsAreRefused() {
        for (kind in listOf("partial", "virtual", "duplicate-id", "duplicate-name", "loading", "traversal", "long-id", "bad-mime")) {
            val f = fixture(); val document = f.documents.records.getValue(f.archive)
            when (kind) {
                "partial" -> f.documents.records[f.archive] = document.copy(flags = Document.FLAG_PARTIAL)
                "virtual" -> f.documents.records[f.archive] = document.copy(flags = Document.FLAG_VIRTUAL_DOCUMENT)
                "duplicate-id" -> f.documents.children.getValue(f.series).add(f.archive)
                "duplicate-name" -> f.documents.file(f.series, document.name)
                "loading" -> f.documents.incomplete = true
                "traversal" -> f.documents.records[f.archive] = document.copy(name = "../PRIVATE.cbz")
                "long-id" -> f.documents.records[f.archive] = document.copy(id = "x".repeat(16385))
                "bad-mime" -> f.documents.records[f.archive] = document.copy(mime = "PRIVATE\nTYPE")
            }
            failure(LibraryCaptureFailure.INCOMPLETE) { capturer(f.documents).scan(tree, "tapas") }
            assertTrue(f.documents.opens.isEmpty())
        }
    }

    @Test fun cancelClosesTheStreamAndColdRetryReusesOnlyJournaledCompletedFiles() {
        val f = fixture(large = true); val capture = capturer(f.documents)
        val plan = capture.scan(tree, "tapas"); val root = root()
        val cancelled = LibraryCaptureCancellation()
        failure(LibraryCaptureFailure.CANCELLED) {
            capture.capture(plan, root, cancelled) { p ->
                if (p.phase == WorkingCapturePhase.COPYING && p.filesDone == 1 && p.bytesRead > plan.files.first().document.bytes!!)
                    cancelled.cancel()
            }
        }
        assertFalse(stable(root)); assertEquals(1, f.documents.closes[f.archive])
        assertArrayEquals(f.documents.bytes.getValue(f.metadata), root.resolve("workspace/Library/Series/a.json").readBytes())
        assertFalse(root.walkTopDown().any { it.name.startsWith(".capture-part-") })
        val result = capturer(f.documents).capture(plan, root)
        assertTrue(stable(root)); assertArrayEquals(f.documents.bytes.getValue(f.archive), result.workspace.resolve("Library/Series/b.cbz").readBytes())
        assertEquals(2, f.documents.opens[f.metadata]) // First copy, then retry's source verification, without recopy.
        assertEquals(3, f.documents.opens[f.archive]) // Interrupted copy, retry, then source verification.
    }

    @Test fun aCompletedPrivateCaptureCanRecoverWithoutSourceAccessAndStillVerifiesEveryByte() {
        val f = fixture(); val capture = capturer(f.documents); val root = root()
        val result = capture.capture(capture.scan(tree, "tapas"), root)
        val opens = f.documents.opens.toMap(); f.documents.available = false
        val recovered = capturer(f.documents).recover(root, WorkingLibraryCapture.source(tree))!!
        assertEquals(result.bytes, recovered.bytes); assertEquals(opens, f.documents.opens)
        recovered.workspace.resolve("Library/Series/b.cbz").writeText("PRIVATE corruption")
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.recover(root, WorkingLibraryCapture.source(tree)) }
    }

    @Test fun recoveryRejectsAnExtraFileWrongSourceOrAnEscapingJournalPath() {
        val f = fixture(); val capture = capturer(f.documents); val root = root()
        val result = capture.capture(capture.scan(tree, "tapas"), root)
        val extra = result.workspace.resolve("PRIVATE.json"); extra.writeText("retained")
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.recover(root, WorkingLibraryCapture.source(tree)) }
        extra.delete()
        failure(LibraryCaptureFailure.PRIVATE_COPY) {
            capture.recover(root, WorkingLibraryCapture.source(Uri.parse("content://fixture.documents/tree/other")))
        }
        val state = root.resolve("capture-v1.json")
        val value = JSONObject(state.readText())
        value.getJSONObject("plan").getJSONArray("files").getJSONObject(0).put("path", "../PRIVATE")
        state.writeText(value.toString())
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.recover(root, WorkingLibraryCapture.source(tree)) }
    }

    @Test fun lowSpaceLeavesThePreviousWorkspaceUntouchedAndCanRetryWithMoreSpace() {
        val f = fixture(); var free = 0L
        val capture = capturer(f.documents, { free }); val plan = capture.scan(tree, "tapas")
        val root = root()
        failure(LibraryCaptureFailure.SPACE) { capture.capture(plan, root) }
        assertTrue(f.documents.opens.isEmpty()); assertFalse(stable(root))
        free = 1024L * 1024
        val result = capture.capture(plan, root)
        assertTrue(result.workspace.resolve("Library/Series/b.cbz").isFile)
        assertTrue(stable(root))
    }

    @Test fun unknownSizesAreHonestAndRunningOutOfSpaceMidCopyDoesNotCommitTheCapture() {
        val f = fixture(unknown = true, large = true); var free = 2L * 1024 * 1024
        val capture = capturer(f.documents, { free }); val plan = capture.scan(tree, "tapas")
        assertEquals(1, plan.unknownSizes)
        val root = root()
        failure(LibraryCaptureFailure.SPACE) {
            capture.capture(plan, root) { p ->
                if (p.phase == WorkingCapturePhase.COPYING && p.bytesRead > 64 * 1024) free = 0
            }
        }
        assertFalse(stable(root))
        free = 2L * 1024 * 1024
        assertNotNull(capture.capture(plan, root))
        assertTrue(stable(root))
    }

    @Test fun failedJournalCommitRetainsUnselectedBytesAndRetryRecopiesOnlyTheUnrecordedFile() {
        val f = fixture(); val capture = capturer(f.documents); val plan = capture.scan(tree, "tapas"); val root = root()
        var injected = false
        failure(LibraryCaptureFailure.PRIVATE_COPY) {
            capture.capture(plan, root) { p ->
                if (!injected && p.phase == WorkingCapturePhase.COPYING && p.bytesRead > 0) {
                    injected = true; root.resolve("completed").writeText("PRIVATE write failure")
                }
            }
        }
        assertFalse(stable(root))
        assertTrue(root.resolve("workspace/Library/Series/a.json").isFile)
        assertTrue(root.resolve("completed").delete())
        assertNotNull(capture.capture(plan, root)); assertTrue(stable(root))
        assertEquals(3, f.documents.opens[f.metadata])
    }

    @Test fun inputSymlinksAndUnrelatedDestinationFilesAreNeverFollowedOrRemoved() {
        val f = fixture(); val capture = capturer(f.documents); val plan = capture.scan(tree, "tapas")
        val outside = temporary.newFolder().canonicalFile
        val sentinel = outside.resolve("PRIVATE"); sentinel.writeText("retained")
        val link = temporary.root.canonicalFile.resolve("linked")
        Files.createSymbolicLink(link.toPath(), outside.toPath())
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.capture(plan, link) }
        assertEquals("retained", sentinel.readText())
        val root = root(); root.mkdirs(); root.resolve("keep").writeText("retained")
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.capture(plan, root) }
        assertEquals("retained", root.resolve("keep").readText())
    }

    @Test fun anInterruptedPrivateFileCannotBeTrustedAfterItsBytesChange() {
        val f = fixture(large = true); val capture = capturer(f.documents); val plan = capture.scan(tree, "tapas")
        val root = root(); val cancelled = LibraryCaptureCancellation()
        failure(LibraryCaptureFailure.CANCELLED) {
            capture.capture(plan, root, cancelled) { p ->
                if (p.phase == WorkingCapturePhase.COPYING && p.filesDone == 1 && p.bytesRead > 64 * 1024) cancelled.cancel()
            }
        }
        root.resolve("workspace/Library/Series/a.json").writeText("changed")
        failure(LibraryCaptureFailure.PRIVATE_COPY) { capture.capture(plan, root) }
        assertFalse(stable(root))
    }

    @Test fun sourceIdentityIsCanonicalAndPrivateModelsDoNotExposeAuthority() {
        val source = WorkingLibraryCapture.source(tree)
        assertEquals(source, WorkingLibraryCapture.source(Uri.parse("$tree/document/ignored?ignored=true#fragment")))
        assertEquals(64, source.key.length)
        assertEquals("WorkingLibrarySource(private)", source.toString())
        val f = fixture(); val plan = capturer(f.documents).scan(tree, "tapas")
        assertEquals("WorkingCapturePlan(private)", plan.toString())
        assertEquals("WorkingLibraryDocument(private)", plan.files.first().document.toString())
        failure(LibraryCaptureFailure.ACCESS) { WorkingLibraryCapture.source(Uri.parse("file:///PRIVATE")) }
    }
}
