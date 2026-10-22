package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.export.ExportFailure
import io.github.bcrute.comicarchiver.export.ExportSummary
import io.github.bcrute.comicarchiver.export.JournalEntry
import io.github.bcrute.comicarchiver.export.JournalState
import io.github.bcrute.comicarchiver.export.ExportKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Focused JVM tests for the UI vocabulary and the controller→screen mapping: the frozen
 * issue set, one notice + one recovery action per issue, the Held-only cold start, every
 * download DTO shape, and summary-to-panel mapping that never flattens a partial run
 * into success.
 */
class UiMappingTest {

    private val comic = LibraryEntry("s1", "Sample Comic", LibraryKind.COMIC, 3, 3)
    private val novel = LibraryEntry("s2", "Sample Novel", LibraryKind.NOVELS, 5, 2)

    private fun state(
        signIn: SignInState = SignInState.SignedIn("tapas"),
        download: DownloadState = DownloadState.Held,
        library: List<LibraryEntry> = listOf(comic, novel),
        notice: Notice? = null,
    ) = AppState("tapas", signIn, download, library, notice)

    @Test
    fun `the public issue set is exactly the frozen contract set`() {
        assertEquals(
            setOf(
                "login_required", "metered_hold", "connection_unverified", "storage_low",
                "source_refused", "job_failed", "export_interrupted", "worker_offline",
                "unsupported_site",
            ),
            PublicIssue.entries.map { it.wireName }.toSet(),
        )
    }

    @Test
    fun `every issue has one non-empty notice and one recovery action`() {
        for (issue in PublicIssue.entries) {
            val words = NoticeText.wordsFor(issue)
            assertTrue("notice blank for ${issue.wireName}", words.notice.isNotBlank())
            assertTrue("action blank for ${issue.wireName}", words.recoveryAction.isNotBlank())
        }
    }

    @Test
    fun `cold start maps to held, not running, signed out`() {
        val ui = UiFixtures.coldStart.toDownloadsScreenUi()
        assertTrue(ui.download is DownloadsViewUi.Held)
        assertEquals(HoldReasonUi.NOT_STARTED, (ui.download as DownloadsViewUi.Held).reason)
        assertFalse(ui.signedIn)
    }

    @Test
    fun `planning maps to running with zero units, never to a hold`() {
        val ui = state(download = DownloadState.Planning).toDownloadsScreenUi()
        val running = ui.download as DownloadsViewUi.Running
        assertEquals(0, running.doneUnits)
        assertEquals(0, running.totalUnits)
    }

    @Test
    fun `running carries its counts`() {
        val ui = state(download = DownloadState.Running(done = 34, total = 80)).toDownloadsScreenUi()
        val running = ui.download as DownloadsViewUi.Running
        assertEquals(34, running.doneUnits)
        assertEquals(80, running.totalUnits)
    }

    @Test
    fun `known pause reasons map to their holds, others to user-paused`() {
        val metered = state(download = DownloadState.Paused(BetaCodes.METERED_HOLD))
            .toDownloadsScreenUi().download as DownloadsViewUi.Held
        assertEquals(HoldReasonUi.METERED_OR_UNKNOWN_NETWORK, metered.reason)

        val unverified = state(download = DownloadState.Paused(BetaCodes.CONNECTION_UNVERIFIED))
            .toDownloadsScreenUi().download as DownloadsViewUi.Held
        assertEquals(HoldReasonUi.CONNECTION_UNVERIFIED, unverified.reason)

        val other = state(download = DownloadState.Paused("anything_else"))
            .toDownloadsScreenUi().download as DownloadsViewUi.Held
        assertEquals(HoldReasonUi.PAUSED_BY_USER, other.reason)
    }

    @Test
    fun `finished carries the controller's summary words`() {
        val ui = state(download = DownloadState.Finished("37 items saved"))
            .toDownloadsScreenUi()
        val finished = ui.download as DownloadsViewUi.Finished
        assertEquals("37 items saved", finished.summary)
    }

    @Test
    fun `a failed download surfaces a notice and stays held`() {
        val ui = state(download = DownloadState.Failed(BetaCodes.WORKER_OFFLINE))
            .toDownloadsScreenUi()
        assertTrue(ui.download is DownloadsViewUi.Held)
        assertEquals(PublicIssue.WORKER_OFFLINE, ui.notice?.issue)
    }

    @Test
    fun `an unknown code still renders, as job_failed`() {
        val ui = state(notice = Notice("something_new", "Words.")).toDownloadsScreenUi()
        assertEquals(PublicIssue.JOB_FAILED, ui.notice?.issue)
        assertEquals("Words.", ui.notice?.detail)
    }

    @Test
    fun `sign-in screen derives busy and failure notice from sign-in state`() {
        val idle = state(signIn = SignInState.SignedOut).toSignInScreenUi()
        assertFalse(idle.busy)
        assertNull(idle.notice)

        val busy = state(signIn = SignInState.Settling).toSignInScreenUi()
        assertTrue(busy.busy)

        val failed = state(signIn = SignInState.Failed(BetaCodes.LOGIN_REQUIRED))
            .toSignInScreenUi()
        assertEquals(PublicIssue.LOGIN_REQUIRED, failed.notice?.issue)
    }

    @Test
    fun `remaining counts only unsaved episodes and never goes negative`() {
        val ui = state(library = listOf(comic, novel)).toDownloadsScreenUi()
        assertEquals(3, ui.remaining) // novel has 5 - 2 saved; comic complete

        val over = state(
            library = listOf(LibraryEntry("s", "T", LibraryKind.COMIC, 4, 9)),
        ).toDownloadsScreenUi()
        assertEquals(0, over.remaining)
    }

    @Test
    fun `library rows label the format from the controller kind`() {
        val rows = state().toLibraryUi()
        assertEquals("CBZ", rows[0].formatLabel)
        assertEquals("EPUB", rows[1].formatLabel)
        assertEquals(5, rows[1].episodeCount)
        assertEquals(2, rows[1].savedCount)
    }

    @Test
    fun `every internal export failure maps to exactly one public issue`() {
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.VERIFICATION_MISMATCH))
        assertEquals(PublicIssue.SOURCE_REFUSED, publicIssueFor(ExportFailure.SOURCE_REFUSED))
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.TARGET_REFUSED))
        assertEquals(PublicIssue.STORAGE_LOW, publicIssueFor(ExportFailure.STORAGE_LOW))
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, publicIssueFor(ExportFailure.GRANT_LOST))
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, publicIssueFor(ExportFailure.INTERRUPTED))
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.MANIFEST_INVALID))
    }

    @Test
    fun `summary mapping never calls an interrupted run complete`() {
        val summary = ExportSummary(
            verified = 3, failed = 0, alreadyVerified = 2,
            failures = emptyList(), interrupted = true, grantLost = false,
        )
        val panel = ExportPanelStateUi.fromSummary(summary, total = 9)
        assertEquals(ExportPhaseUi.INTERRUPTED, panel.phase)
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, panel.lastIssue)
    }

    @Test
    fun `grant loss maps to a re-pick phase`() {
        val summary = ExportSummary(
            verified = 1, failed = 0, alreadyVerified = 0,
            failures = emptyList(), interrupted = true, grantLost = true,
        )
        assertEquals(ExportPhaseUi.GRANT_LOST, ExportPanelStateUi.fromSummary(summary, 9).phase)
    }

    @Test
    fun `failures surface with the failed count and their public issue`() {
        val failedEntry = JournalEntry(
            episodeId = "ep1", seriesId = "s1", kind = ExportKind.CBZ,
            requestedName = "ep1.cbz", actualName = "ep1.cbz", bytes = 1, sha256 = "0".repeat(64),
            state = JournalState.FAILED, targetUri = null, attempt = 1,
            failure = ExportFailure.STORAGE_LOW,
        )
        val panel = ExportPanelStateUi.fromSummary(
            ExportSummary(0, 1, 0, listOf(failedEntry), interrupted = false, grantLost = false),
            total = 2,
        )
        assertEquals(ExportPhaseUi.FINISHED_WITH_FAILURES, panel.phase)
        assertEquals(1, panel.progress?.failed)
        assertEquals(PublicIssue.STORAGE_LOW, panel.lastIssue)
    }

    @Test
    fun `a fully verified run maps to all verified`() {
        val panel = ExportPanelStateUi.fromSummary(
            ExportSummary(4, 0, 5, emptyList(), interrupted = false, grantLost = false),
            total = 9,
        )
        assertEquals(ExportPhaseUi.ALL_VERIFIED, panel.phase)
        assertNull(panel.lastIssue)
        assertEquals(9, panel.progress!!.verified + panel.progress.alreadyVerified)
    }

    @Test
    fun `fixtures are labelled and built on the frozen DTOs`() {
        assertTrue(UiFixtures.LABEL.contains("FIXTURE"))
        // The fixtures carry the controller's frozen DTOs (the UI types are only what
        // UiMapping produces from them at render time).
        assertTrue(UiFixtures.running.download is DownloadState.Running)
    }
    @Test
    fun `live status details reach the screen without being discarded`() {
        val ui = state(download = DownloadState.Running(0, 0, "Downloading episodes…", "22 episodes saved\n316 image pages"))
            .toDownloadsScreenUi().download as DownloadsViewUi.Running
        assertEquals("Downloading episodes…", ui.description)
        assertEquals("22 episodes saved\n316 image pages", ui.detail)
    }

}
