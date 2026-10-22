package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DownloadStatusTest {
    private fun state(account: String, worker: String = """{"running":true,"state":"ready"}""") =
        DownloadStatus.fromEnvelope(JSONObject("""{"ok":true,"result":{"worker":$worker,"account":$account}}"""))

    @Test fun idle_worker_does_not_mask_failed_or_stopped_plan() {
        assertEquals(DownloadState.Failed(BetaCodes.JOB_FAILED), state("""{"status":"held","code":"job_failed"}"""))
        assertEquals(DownloadState.Paused("user"), state("""{"status":"stopped"}"""))
    }

    @Test fun policy_and_signin_holds_keep_their_recovery_reason() {
        assertEquals(DownloadState.Paused(BetaCodes.METERED_HOLD), state("""{"status":"held","code":"network_policy"}"""))
        assertEquals(DownloadState.Paused(BetaCodes.CONNECTION_UNVERIFIED), state("""{"status":"held","code":"network_unavailable"}"""))
        assertEquals(DownloadState.Failed(BetaCodes.LOGIN_REQUIRED), state("""{"status":"held","code":"login_required"}"""))
    }

    @Test fun stopped_worker_is_recoverable_even_when_plan_still_says_running() {
        assertEquals(DownloadState.Failed(BetaCodes.WORKER_OFFLINE), state("""{"status":"running"}""", """{"running":false}"""))
    }

    @Test fun running_list_scan_displays_observed_phase_and_list_counts() {
        val result = state("""{"status":"running","phase":"lists","lists":{"checked":2,"total":4}}""") as DownloadState.Running
        assertEquals("Reading library lists (2 of 4)…", result.description)
    }

    @Test fun finished_is_based_on_plan_and_keeps_coverage_limit_visible() {
        val result = state("""{"status":"finished"}""") as DownloadState.Finished
        assertTrue(result.summary.contains("coverage is not verified"))
    }

    @Test fun malformed_or_failed_status_never_claims_progress() {
        for (body in listOf("{}", """{"ok":false}""", """{"ok":true,"result":{}}"""))
            assertTrue(DownloadStatus.fromEnvelope(JSONObject(body)) is DownloadState.Failed)
    }
}
