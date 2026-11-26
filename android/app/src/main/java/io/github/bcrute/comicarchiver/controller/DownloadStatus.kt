package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject
import io.github.bcrute.comicarchiver.words.say

/** Interpret the durable plan, rather than mistaking an idle worker for progress. */
object DownloadStatus {
    fun fromEnvelope(envelope: JSONObject): DownloadState {
        if (!envelope.optBoolean("ok")) return DownloadState.Failed(BetaCodes.JOB_FAILED)
        val result = envelope.optJSONObject("result") ?: return DownloadState.Failed(BetaCodes.JOB_FAILED)
        val worker = result.optJSONObject("worker")
        if (worker == null || !worker.optBoolean("running") || worker.optBoolean("stop_requested"))
            return DownloadState.Failed(BetaCodes.WORKER_OFFLINE)
        val account = result.optJSONObject("account") ?: return DownloadState.Failed(BetaCodes.JOB_FAILED)
        return when (account.optString("status")) {
            "held" -> when (account.optString("code")) {
                "network_policy" -> DownloadState.Paused(BetaCodes.METERED_HOLD)
                "network_unavailable" -> DownloadState.Paused(BetaCodes.CONNECTION_UNVERIFIED)
                "login_required" -> DownloadState.Failed(BetaCodes.LOGIN_REQUIRED)
                else -> DownloadState.Failed(BetaCodes.JOB_FAILED)
            }
            "stopped" -> DownloadState.Paused("user")
            "finished" -> DownloadState.Finished(say("downloads.saved_available_items_account_coverage_is"))
            "running" -> {
                val progress = result.optJSONObject("progress")
                val counts = result.optJSONObject("counts")
                val phase = progress?.optString("phase").orEmpty()
                val description = when (phase) {
                    "archive" -> say("dashboard.downloading_episodes")
                    "account_list" -> say("downloads.reading_your_library")
                    "scan", "history_series" -> say("downloads.checking_available_episodes")
                    "authentication" -> say("downloads.checking_sign_in")
                    "metadata" -> say("downloads.reading_series_information")
                    "history" -> say("downloads.reading_account_history")
                    else -> when (account.optString("phase")) {
                        "lists" -> {
                            val lists = account.optJSONObject("lists")
                            val checked = lists?.optInt("checked", 0) ?: 0
                            val total = lists?.optInt("total", 0) ?: 0
                            say("dashboard.finding_your_series") + " | " + say("dashboard.of_read", "n" to checked, "n2" to total)
                        }
                        "series" -> say("dashboard.checking_series_and_downloading")
                        "archive" -> say("dashboard.downloading_episodes")
                        "existing_backlog" -> say("dashboard.finishing_saved_downloads")
                        "verify" -> say("dashboard.finishing_download")
                        else -> say("downloads.starting")
                    }
                }
                val details = buildList {
                    count(counts, "saved_episodes")?.let { add(if (it == 1L) say("downloads.1_episode_saved") else say("downloads.episodes_saved", "it" to it)) }
                    if (phase == "account_list") {
                        add(LIST_FIRST)
                        val series = count(progress, "observed_library_series_count")
                        val pages = count(progress, "reading_list_page_count")
                        if (series != null) add(say("downloads.series_found_in_this_list", "series" to series))
                        if (pages != null) add(say("downloads.list_pages_checked", "pages" to pages))
                    }
                    val checked = count(progress, "series_checks_completed")
                    val target = count(progress, "series_check_target_count")
                    if (checked != null && target != null && target > 0)
                        add(say("downloads.of_series_checked", "checked" to checked, "target" to target))
                    val pages = count(progress, "downloaded_pages")
                    val bytes = count(progress, "downloaded_bytes")
                    if (pages != null) add(say("downloads.image_pages_in_this_batch", "pages" to pages))
                    count(progress, "downloaded_chapters")?.takeIf { it > 0 }?.let {
                        add(say("downloads.text_chapters_in_this_batch", "it" to it))
                    }
                    if (bytes != null) add(say("downloads.downloaded_in_this_batch", "value" to (displayBytes(bytes))))
                    count(progress, "age_seconds")?.let {
                        add(if (it < 2) say("downloads.progress_updated_just_now") else say("downloads.last_progress_s_ago", "it" to it))
                    }
                    val retry = account.optJSONObject("retry")
                    count(retry, "seconds")?.let { add(say("downloads.waiting_s_before_retrying", "it" to it)) }
                }
                DownloadState.Running(0, 0, description, details.joinToString("\n").ifBlank { null })
            }
            else -> DownloadState.Failed(BetaCodes.JOB_FAILED)
        }
    }

    /**
     * What a cold start shows for the stored plan without admitting it: a download the user
     * paused reads as paused again after a restart or update. Only Go resumes it.
     */
    fun restored(envelope: JSONObject): DownloadState? =
        envelope.takeIf { it.optBoolean("ok") }?.optJSONObject("result")?.optJSONObject("account")
            ?.takeIf { it.optString("status") == "stopped" }?.let { DownloadState.Paused("user") }

    /**
     * Whether the engine has nothing running or in flight for the site, so nothing needs the app
     * kept alive or the phone kept awake: the download finished, failed or waits for the person,
     * and the downloader is idle or gone. A wait the engine ends by itself (a retry, the
     * connection) is reported as running and is never over; nor is a step still finishing after
     * Pause, a drain under way, or anything a failed reading cannot prove.
     */
    fun over(envelope: JSONObject): Boolean {
        val worker = envelope.takeIf { it.opt("ok") == true }?.optJSONObject("result")?.optJSONObject("worker")
            ?: return false
        val running = worker.opt("running")
        val idle = running == false ||
            running == true && !worker.optBoolean("stop_requested") && worker.optString("state") == "ready"
        val download = fromEnvelope(envelope)
        return idle && (download is DownloadState.Failed || download is DownloadState.Finished ||
            download == DownloadState.Paused("user"))
    }

    // Lists are pages in a shifting order: read in one go, a series cannot slip past unseen.
    val LIST_FIRST get() = say("downloads.saving_starts_once_every_list_is")

    private fun count(value: JSONObject?, key: String): Long? {
        val raw = value?.opt(key)
        return when (raw) {
            is Int -> raw.toLong().takeIf { it >= 0 }
            is Long -> raw.takeIf { it >= 0 }
            else -> null
        }
    }

    private fun displayBytes(bytes: Long): String = when {
        bytes >= 1024L * 1024 * 1024 -> say("downloads.gib", "value" to (bytes / (1024L * 1024 * 1024)))
        bytes >= 1024L * 1024 -> say("downloads.mib", "value" to (bytes / (1024L * 1024)))
        bytes >= 1024L -> say("downloads.kib", "value" to (bytes / 1024L))
        else -> "$bytes B"
    }
}
