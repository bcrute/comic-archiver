package io.github.bcrute.comicarchiver.runtime

import android.app.Application
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Storage/selection transactions only. Fake verification receipts do not qualify archive adoption. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class WorkingLibraryLocationsTest {
    @get:Rule val temporary = TemporaryFolder()
    private val source = WorkingLibrarySource("a".repeat(64), "content://fixture.documents/tree/library")
    private val digest = "b".repeat(64)

    private fun failure(expected: WorkingLibraryFailure, action: () -> Unit) {
        try { action(); fail("Expected a fixed public library refusal") }
        catch (error: WorkingLibraryException) {
            assertEquals(expected, error.failure)
            assertEquals(expected.words, error.message)
            assertFalse(error.message.orEmpty().contains("PRIVATE"))
        }
    }

    private fun begin(locations: WorkingLibraryLocations): PendingWorkingLibrary =
        (locations.begin("Picked library", source) as WorkingLibraryStart.Copy).pending

    private fun staged(locations: WorkingLibraryLocations): Pair<PendingWorkingLibrary, File> {
        val pending = begin(locations)
        val file = locations.copyRoot(pending.id).resolve("Library/series/finished.cbz")
        file.parentFile!!.mkdirs()
        file.writeText("registry fixture, not a verified archive")
        return pending to file
    }

    @Test fun theLegacyRootAndItsExistingFilesKeepTheirIdentity() {
        val owner = temporary.newFolder("private")
        val archive = owner.resolve("workspace/Library/series/old.cbz")
        archive.parentFile!!.mkdirs(); archive.writeText("original")
        val locations = WorkingLibraryLocations(owner)
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
        assertEquals(owner.resolve("workspace"), locations.root(WorkingLibraryLocations.DEFAULT))
        assertEquals("original", archive.readText())
        assertFalse(owner.resolve("working-libraries").exists())
        assertFalse(owner.resolve("library-locations.json").exists())
    }

    @Test fun aPendingCopySurvivesColdReopenWithoutChangingTheActiveLibrary() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, file) = staged(locations)
        val cold = WorkingLibraryLocations(owner)
        assertEquals(pending, cold.pending())
        assertEquals(WorkingLibraryLocations.DEFAULT, cold.current().id)
        assertEquals(file, cold.copyRoot(pending.id).resolve("Library/series/finished.cbz"))
        assertEquals(1, cold.libraries().size)
    }

    @Test fun verificationAndCompletionMakeALibrarySelectableButDoNotSelectIt() {
        val owner = temporary.newFolder("private")
        val original = owner.resolve("workspace/Library/original.cbz")
        original.parentFile!!.mkdirs(); original.writeText("keep")
        val locations = WorkingLibraryLocations(owner)
        val (pending, staged) = staged(locations)
        val bytes = staged.readBytes()
        locations.verified(pending.id, digest)
        failure(WorkingLibraryFailure.NOT_READY) { locations.copyRoot(pending.id) }
        val completed = locations.complete(pending.id)
        assertNull(locations.pending())
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
        assertArrayEquals(bytes, locations.root(completed.id).resolve("Library/series/finished.cbz").readBytes())
        locations.select(completed.id)
        assertEquals(completed, WorkingLibraryLocations(owner).current())
        locations.select(WorkingLibraryLocations.DEFAULT)
        assertEquals("keep", original.readText())
        assertArrayEquals(bytes, locations.root(completed.id).resolve("Library/series/finished.cbz").readBytes())
    }

    @Test fun choosingTheSameSourceReopensItsStoredRootWithoutAnotherCopy() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, _) = staged(locations)
        locations.verified(pending.id, digest)
        val completed = locations.complete(pending.id)
        val repeated = locations.begin("A new display label", source)
        assertEquals(WorkingLibraryStart.Existing(completed), repeated)
        assertNull(locations.pending())
        assertEquals(2, locations.libraries().size)
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
    }

    @Test fun anUnverifiedCopyCannotCompleteOrBecomeTheActiveRoot() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val pending = begin(locations)
        failure(WorkingLibraryFailure.NOT_READY) { locations.complete(pending.id) }
        failure(WorkingLibraryFailure.BUSY) { locations.select(pending.id) }
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
        assertEquals(pending, locations.pending())
    }

    @Test fun aSecondImportAndAnOldTicketCannotMutateTheNewChoice() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val first = begin(locations)
        failure(WorkingLibraryFailure.BUSY) { locations.begin("Another", source.copy(key = "c".repeat(64))) }
        locations.cancel(first.id)
        val second = begin(locations)
        failure(WorkingLibraryFailure.STALE) { locations.verified(first.id, digest) }
        failure(WorkingLibraryFailure.STALE) { locations.complete(first.id) }
        failure(WorkingLibraryFailure.STALE) { locations.cancel(first.id) }
        assertEquals(second, locations.pending())
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
    }

    @Test fun cancellingRemovesOnlyTheUncommittedCopyAndDoesNotFollowItsSymlink() {
        val owner = temporary.newFolder("private")
        val outside = temporary.newFolder("source")
        val original = outside.resolve("original.cbz").apply { writeText("source original") }
        val locations = WorkingLibraryLocations(owner)
        val (pending, staged) = staged(locations)
        Files.createSymbolicLink(staged.parentFile!!.resolve("source-link").toPath(), outside.toPath())
        locations.cancel(pending.id)
        assertNull(locations.pending())
        assertFalse(staged.exists())
        assertEquals("source original", original.readText())
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
    }

    @Test fun aCrashAfterThePrivateRenameResumesTheSameCopyWithoutActivation() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, staged) = staged(locations)
        val bytes = staged.readBytes()
        val payload = locations.copyRoot(pending.id)
        locations.verified(pending.id, digest)
        val target = owner.resolve("working-libraries/${pending.id}")
        assertTrue(payload.renameTo(target)) // Fault boundary before the registry commit.
        val cold = WorkingLibraryLocations(owner)
        assertEquals(WorkingLibraryLocations.DEFAULT, cold.current().id)
        assertEquals(digest, cold.pending()?.digest)
        val completed = cold.complete(pending.id)
        assertArrayEquals(bytes, cold.root(completed.id).resolve("Library/series/finished.cbz").readBytes())
        assertEquals(WorkingLibraryLocations.DEFAULT, cold.current().id)
        assertNull(cold.pending())
    }

    @Test fun aFailedRegistryCommitRetainsTheOldRootAndCanFinishTheRenamedCopy() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, _) = staged(locations)
        locations.verified(pending.id, digest)
        val blocked = owner.resolve("library-locations.json.new")
        blocked.mkdirs(); blocked.resolve("block-write").writeText("keep AtomicFile from opening a stream")
        failure(WorkingLibraryFailure.UNAVAILABLE) { locations.complete(pending.id) }
        blocked.deleteRecursively()
        val cold = WorkingLibraryLocations(owner)
        assertEquals(WorkingLibraryLocations.DEFAULT, cold.current().id)
        assertEquals(digest, cold.pending()?.digest)
        val completed = cold.complete(pending.id)
        assertEquals(2, cold.libraries().size)
        assertTrue(cold.root(completed.id).resolve("Library/series/finished.cbz").isFile)
    }

    @Test fun cancellingAnInterruptedRenameCannotDeleteAnExistingLibrary() {
        val owner = temporary.newFolder("private")
        val original = owner.resolve("workspace/Library/keep.cbz")
        original.parentFile!!.mkdirs(); original.writeText("keep")
        val locations = WorkingLibraryLocations(owner)
        val (pending, _) = staged(locations)
        val payload = locations.copyRoot(pending.id)
        locations.verified(pending.id, digest)
        val target = owner.resolve("working-libraries/${pending.id}")
        assertTrue(payload.renameTo(target))
        WorkingLibraryLocations(owner).cancel(pending.id)
        assertFalse(target.exists())
        assertEquals("keep", original.readText())
        assertNull(locations.pending())
        assertEquals(1, locations.libraries().size)
    }

    @Test fun aMissingPreviouslySelectedRootIsNotRecreatedAsAnEmptyLibrary() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, _) = staged(locations)
        locations.verified(pending.id, digest)
        val complete = locations.complete(pending.id)
        locations.select(complete.id)
        val root = locations.root(complete.id)
        root.deleteRecursively()
        failure(WorkingLibraryFailure.UNAVAILABLE) { WorkingLibraryLocations(owner).current() }
        failure(WorkingLibraryFailure.UNAVAILABLE) { locations.root(complete.id) }
        assertFalse(root.exists())
        locations.select(WorkingLibraryLocations.DEFAULT)
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
    }

    @Test fun aCorruptRegistryOrForeignMarkerNeverFallsBackToAnotherLibrary() {
        val owner = temporary.newFolder("private")
        val locations = WorkingLibraryLocations(owner)
        val (pending, _) = staged(locations)
        locations.verified(pending.id, digest)
        val complete = locations.complete(pending.id)
        val marker = locations.root(complete.id).resolve(".working-library.json")
        marker.writeText("{\"v\":1,\"id\":\"PRIVATE\"}")
        failure(WorkingLibraryFailure.CORRUPT) { locations.select(complete.id) }
        assertEquals(WorkingLibraryLocations.DEFAULT, locations.current().id)
        val file = owner.resolve("library-locations.json")
        file.writeText("{\"v\":9,\"active\":\"PRIVATE\"}")
        val invalid = file.readBytes()
        failure(WorkingLibraryFailure.CORRUPT) { WorkingLibraryLocations(owner).current() }
        assertArrayEquals(invalid, file.readBytes())
    }

    @Test fun invalidSourcesAndSymlinkedNamespacesCannotChooseAnArbitraryFilesystemRoot() {
        val owner = temporary.newFolder("private")
        val outside = temporary.newFolder("outside")
        val sentinel = outside.resolve("keep").apply { writeText("keep") }
        val locations = WorkingLibraryLocations(owner)
        for (invalid in listOf(source.copy(tree = "file:///PRIVATE"), source.copy(key = "PRIVATE"),
            source.copy(tree = "content://fixture.documents/document/PRIVATE"))) {
            failure(WorkingLibraryFailure.CORRUPT) { locations.begin("Picked library", invalid) }
        }
        assertNull(locations.pending())
        Files.createSymbolicLink(owner.resolve("working-libraries").toPath(), outside.toPath())
        failure(WorkingLibraryFailure.CORRUPT) { begin(locations) }
        assertEquals("keep", sentinel.readText())
        assertEquals(listOf(sentinel.name), outside.listFiles()!!.map { it.name })
    }

    @Test fun concurrentOwnersCannotReserveTwoImportsOrLoseTheFirstPendingChoice() {
        val owner = temporary.newFolder("private")
        val pool = Executors.newFixedThreadPool(2)
        val start = CountDownLatch(1)
        try {
            val attempts = (0..1).map {
                pool.submit<Any> {
                    assertTrue(start.await(2, TimeUnit.SECONDS))
                    try { WorkingLibraryLocations(owner).begin("Picked library", source) }
                    catch (error: WorkingLibraryException) { error.failure }
                }
            }
            start.countDown()
            val outcomes = attempts.map { it.get(5, TimeUnit.SECONDS) }
            assertEquals(1, outcomes.count { it is WorkingLibraryStart.Copy })
            assertEquals(1, outcomes.count { it == WorkingLibraryFailure.BUSY })
            val copied = (outcomes.single { it is WorkingLibraryStart.Copy } as WorkingLibraryStart.Copy).pending
            val cold = WorkingLibraryLocations(owner)
            assertEquals(copied, cold.pending())
            assertEquals(1, cold.libraries().size)
            assertEquals(WorkingLibraryLocations.DEFAULT, cold.current().id)
        } finally { pool.shutdownNow(); pool.awaitTermination(2, TimeUnit.SECONDS) }
    }
}
