package io.github.bcrute.comicarchiver.worker

import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import org.json.JSONObject

/** Blocking, ordered commands; the host runs them off the main thread. */
class DownloadCommands(
    private val bridge: PythonBridge,
    private val workspaceRoot: String,
    private val workerArgs: String,
) {
    fun go(): String? {
        val start = JSONObject(bridge.dispatch("worker_start", workerArgs))
        if (!start.optBoolean("ok") || !start.optJSONObject("result").let {
                it != null && it.optBoolean("running") && !it.optBoolean("stop_requested")
            }) return BetaCodes.WORKER_OFFLINE
        val go = JSONObject(bridge.dispatch("go", JSONObject().put("root", workspaceRoot).toString()))
        return if (go.optBoolean("ok")) null else BetaCodes.JOB_FAILED
    }
}
