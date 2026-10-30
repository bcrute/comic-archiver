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
    private fun live(progress: String, counts: String = """{"saved_episodes":22}""") =
        DownloadStatus.fromEnvelope(JSONObject("""{"ok":true,"result":{"worker":{"running":true,"state":"busy"},"account":{"status":"running","phase":"series"},"progress":$progress,"counts":$counts}}""")) as DownloadState.Running

    @Test fun actual_transfer_overrides_generic_account_series_phase_and_exposes_counters() {
        val result = live("""{"phase":"archive","downloaded_pages":316,"downloaded_bytes":67108864,"age_seconds":1}""")
        assertEquals("Downloading episodes…", result.description)
        assertTrue(result.detail!!.contains("22 episodes saved"))
        assertTrue(result.detail!!.contains("316 image pages"))
        assertTrue(result.detail!!.contains("64 MiB"))
        assertEquals(0, result.total) // No invented account-wide percentage.
    }

    @Test fun changing_progress_changes_the_visible_feedback() {
        val first = live("""{"phase":"archive","downloaded_pages":10,"downloaded_bytes":1048576}""")
        val second = live("""{"phase":"archive","downloaded_pages":20,"downloaded_bytes":2097152}""")
        assertNotEquals(first.detail, second.detail)
        assertTrue(second.detail!!.contains("20 image pages"))
    }

    @Test fun discovery_displays_observed_pages_and_series_without_waiting_for_the_list_to_finish() {
        val result = live("""{"phase":"account_list","reading_list_page_count":53,"observed_library_series_count":265,"age_seconds":8}""")
        assertEquals("Reading your library…", result.description)
        assertTrue(result.detail!!.contains("265 series found"))
        assertTrue(result.detail!!.contains("53 list pages"))
        assertTrue(result.detail!!.contains("Last progress 8 s ago"))
        assertTrue(result.detail!!.contains(DownloadStatus.LIST_FIRST))
    }

    @Test fun a_cold_start_shows_a_paused_download_as_paused_and_nothing_else() {
        fun restored(status: String) = DownloadStatus.restored(JSONObject(
            """{"ok":true,"result":{"worker":{"running":false},"account":{"status":"$status"}}}"""))
        assertEquals(DownloadState.Paused("user"), restored("stopped"))
        listOf("running", "held", "finished").forEach { assertNull(restored(it)) }
        assertNull(DownloadStatus.restored(JSONObject("""{"ok":false}""")))
    }

    @Test fun one_saved_episode_is_singular() {
        val result = live("""{"phase":"archive"}""", """{"saved_episodes":1}""")
        assertEquals("1 episode saved", result.detail)
    }

    @Test fun absent_invalid_and_unknown_counters_do_not_become_progress() {
        val result = live("""{"downloaded_pages":-1,"downloaded_bytes":"PRIVATE","age_seconds":false}""", "{}")
        assertNull(result.detail)
        assertFalse(result.description!!.contains("PRIVATE"))
    }

}
