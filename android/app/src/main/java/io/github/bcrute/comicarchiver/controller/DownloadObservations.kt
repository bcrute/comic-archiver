package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject

/** Read-only observations, separate from the user's Go admission. No diagnostic strings. */
data class DownloadObservations(
    val receivedAtMillis: Long,
    val worker: ObservedWorker,
    val savedEpisodes: Long? = null,
    val observedSeries: Long? = null,
    val activityAgeSeconds: Long? = null,
    val listsChecked: Long? = null,
    val listsTotal: Long? = null,
    val currentList: String? = null,
    val retrySeconds: Long? = null,
    val retryReason: String? = null,
) {
    companion object {
        fun parse(envelope: JSONObject, receivedAtMillis: Long): DownloadObservations? {
            if (envelope.opt("ok") != true) return null
            val result = envelope.optJSONObject("result") ?: return null
            val worker = result.optJSONObject("worker") ?: return null
            val account = result.optJSONObject("account") ?: return null
            if (account.optString("status") !in setOf("held", "stopped", "finished", "running") &&
                account.opt("configured") != false) return null
            val running = worker.opt("running") as? Boolean ?: return null
            val state = when {
                !running -> ObservedWorker.OFF
                worker.optBoolean("stop_requested") -> ObservedWorker.STOPPING
                worker.optString("state") == "busy" -> ObservedWorker.WORKING
                worker.optString("state") == "ready" -> ObservedWorker.READY
                else -> ObservedWorker.AVAILABLE
            }
            val counts = result.optJSONObject("counts")
            val progress = result.optJSONObject("progress")
            val lists = account.optJSONObject("lists")?.takeIf { account.optString("phase") == "lists" }
            val total = count(lists, "total")?.takeIf { it > 0 }
            val checked = count(lists, "checked")?.takeIf { total != null && it <= total }
            val current = when (lists?.optString("current")) {
                "unlocked" -> "Bought"
                "subscribed" -> "Subscribed"
                "updated" -> "Updated"
                "recent" -> "Recent"
                "free_episodes" -> "Free episodes"
                else -> null
            }
            val retry = account.optJSONObject("retry")?.takeIf { account.optString("status") == "running" }
            return DownloadObservations(receivedAtMillis, state, count(counts, "saved_episodes"),
                count(counts, "observed_series"), count(progress, "age_seconds"), checked,
                total.takeIf { checked != null }, current, count(retry, "seconds"),
                retry?.optString("reason")?.takeIf { it in setOf("source", "images", "connection") })
        }

        private fun count(value: JSONObject?, key: String): Long? = when (val raw = value?.opt(key)) {
            is Int -> raw.toLong().takeIf { it >= 0 }
            is Long -> raw.takeIf { it >= 0 }
            else -> null
        }
    }
}

enum class ObservedWorker(val label: String) {
    OFF("Off"), STOPPING("Stopping"), WORKING("Working"), READY("Ready"), AVAILABLE("Available")
}
