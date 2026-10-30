package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject

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
            "finished" -> DownloadState.Finished("Saved available items; account coverage is not verified.")
            "running" -> {
                val progress = result.optJSONObject("progress")
                val counts = result.optJSONObject("counts")
                val phase = progress?.optString("phase").orEmpty()
                val description = when (phase) {
                    "archive" -> "Downloading episodes…"
                    "account_list" -> "Reading your library…"
                    "scan", "history_series" -> "Checking available episodes…"
                    "authentication" -> "Checking sign-in…"
                    "metadata" -> "Reading series information…"
                    "history" -> "Reading account history…"
                    else -> when (account.optString("phase")) {
                        "lists" -> {
                            val lists = account.optJSONObject("lists")
                            "Reading library lists (${lists?.optInt("checked", 0) ?: 0} of ${lists?.optInt("total", 0) ?: 0})…"
                        }
                        "series" -> "Checking series and available episodes…"
                        "archive", "existing_backlog" -> "Saving available episodes…"
                        "verify" -> "Checking saved files…"
                        else -> "Starting…"
                    }
                }
                val details = buildList {
                    count(counts, "saved_episodes")?.let { add(if (it == 1L) "1 episode saved" else "$it episodes saved") }
                    if (phase == "account_list") {
                        add(LIST_FIRST)
                        val series = count(progress, "observed_library_series_count")
                        val pages = count(progress, "reading_list_page_count")
                        if (series != null) add("$series series found in this list")
                        if (pages != null) add("$pages list pages checked")
                    }
                    val checked = count(progress, "series_checks_completed")
                    val target = count(progress, "series_check_target_count")
                    if (checked != null && target != null && target > 0)
                        add("$checked of $target series checked")
                    val pages = count(progress, "downloaded_pages")
                    val bytes = count(progress, "downloaded_bytes")
                    if (pages != null) add("$pages image pages in this batch")
                    count(progress, "downloaded_chapters")?.takeIf { it > 0 }?.let {
                        add("$it text chapters in this batch")
                    }
                    if (bytes != null) add("${displayBytes(bytes)} downloaded in this batch")
                    count(progress, "age_seconds")?.let {
                        add(if (it < 2) "Progress updated just now" else "Last progress $it s ago")
                    }
                    val retry = account.optJSONObject("retry")
                    count(retry, "seconds")?.let { add("Waiting $it s before retrying") }
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

    // Lists are pages in a shifting order: read in one go, a series cannot slip past unseen.
    const val LIST_FIRST = "Saving starts once every list is read, so nothing is missed. A large library takes a few minutes."

    private fun count(value: JSONObject?, key: String): Long? {
        val raw = value?.opt(key)
        return when (raw) {
            is Int -> raw.toLong().takeIf { it >= 0 }
            is Long -> raw.takeIf { it >= 0 }
            else -> null
        }
    }

    private fun displayBytes(bytes: Long): String = when {
        bytes >= 1024L * 1024 * 1024 -> "${bytes / (1024L * 1024 * 1024)} GiB"
        bytes >= 1024L * 1024 -> "${bytes / (1024L * 1024)} MiB"
        bytes >= 1024L -> "${bytes / 1024L} KiB"
        else -> "$bytes B"
    }
}
