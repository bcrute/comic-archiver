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
                val description = when (account.optString("phase")) {
                    "lists" -> {
                        val lists = account.optJSONObject("lists")
                        "Reading library lists (${lists?.optInt("checked", 0) ?: 0} of ${lists?.optInt("total", 0) ?: 0})…"
                    }
                    "series" -> "Checking series and available episodes…"
                    "archive", "existing_backlog" -> "Saving available episodes…"
                    "verify" -> "Checking saved files…"
                    else -> "Preparing…"
                }
                DownloadState.Running(0, 0, description)
            }
            else -> DownloadState.Failed(BetaCodes.JOB_FAILED)
        }
    }
}
