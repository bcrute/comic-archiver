package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.export.ExportFailure
import io.github.bcrute.comicarchiver.export.ExportSummary
import io.github.bcrute.comicarchiver.export.JournalEntry
import io.github.bcrute.comicarchiver.export.JournalState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Focused JVM tests for the UI vocabulary and state shapes: the frozen issue set, one
 * notice + one recovery action per issue, cold-start held, the running/hold invariant,
 * and summary-to-panel mapping that never flattens a partial run into success.
 */
class UiModelsTest {

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
    fun `cold start is held, not running, with nothing in flight`() {
        val state = DownloadsStateUi.coldStart()
        assertFalse(state.running)
        assertNotNull(state.holdReason)
        assertNull(state.active)
        assertEquals(0, state.queueDepth)
    }

    @Test
    fun `running and a hold reason cannot describe the state together`() {
        assertThrows(IllegalArgumentException::class.java) {
            DownloadsStateUi(
                signedIn = true,
                running = true,
                holdReason = HoldReasonUi.NOT_STARTED,
                queueDepth = 0,
                active = null,
                notice = null,
            )
        }
        assertThrows(IllegalArgumentException::class.java) {
            DownloadsStateUi(
                signedIn = true,
                running = false,
                holdReason = null,
                queueDepth = 0,
                active = null,
                notice = null,
            )
        }
    }

    @Test
    fun `every internal export failure maps to exactly one public issue`() {
        // Total by construction (exhaustive when); this pins the intended mapping.
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.VERIFICATION_MISMATCH))
        assertEquals(PublicIssue.SOURCE_REFUSED, publicIssueFor(ExportFailure.SOURCE_REFUSED))
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.TARGET_REFUSED))
        assertEquals(PublicIssue.STORAGE_LOW, publicIssueFor(ExportFailure.STORAGE_LOW))
        assertEquals(
            PublicIssue.EXPORT_INTERRUPTED,
            publicIssueFor(ExportFailure.GRANT_LOST),
        )
        assertEquals(
            PublicIssue.EXPORT_INTERRUPTED,
            publicIssueFor(ExportFailure.INTERRUPTED),
        )
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
        val panel = ExportPanelStateUi.fromSummary(summary, total = 9)
        assertEquals(ExportPhaseUi.GRANT_LOST, panel.phase)
    }

    @Test
    fun `failures surface with the failed count and their public issue`() {
        val failedEntry = JournalEntry(
            episodeId = "ep1", seriesId = "s1", kind = io.github.bcrute.comicarchiver.export.ExportKind.CBZ,
            requestedName = "ep1.cbz", actualName = "ep1.cbz", bytes = 1, sha256 = "0".repeat(64),
            state = JournalState.FAILED, targetUri = null, attempt = 1,
            failure = ExportFailure.STORAGE_LOW,
        )
        val summary = ExportSummary(
            verified = 0, failed = 1, alreadyVerified = 0,
            failures = listOf(failedEntry), interrupted = false, grantLost = false,
        )
        val panel = ExportPanelStateUi.fromSummary(summary, total = 2)
        assertEquals(ExportPhaseUi.FINISHED_WITH_FAILURES, panel.phase)
        assertEquals(1, panel.progress?.failed)
        assertEquals(PublicIssue.STORAGE_LOW, panel.lastIssue)
    }

    @Test
    fun `a fully verified run maps to all verified`() {
        val summary = ExportSummary(
            verified = 4, failed = 0, alreadyVerified = 5,
            failures = emptyList(), interrupted = false, grantLost = false,
        )
        val panel = ExportPanelStateUi.fromSummary(summary, total = 9)
        assertEquals(ExportPhaseUi.ALL_VERIFIED, panel.phase)
        assertNull(panel.lastIssue)
        assertTrue(panel.progress!!.verified + panel.progress.alreadyVerified == 9)
    }

    @Test
    fun `fixtures are labelled and never claim to be live data`() {
        assertTrue(UiFixtures.LABEL.contains("FIXTURE"))
        assertTrue(UiFixtures.running.running) // sanity: the running fixture really runs
    }
}
