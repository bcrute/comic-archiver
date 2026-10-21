package io.github.bcrute.comicarchiver.worker

import android.content.Context
import android.content.Intent
import android.os.Build
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Runtime DownloadHost (native line): the foreground service owns the worker's lifetime,
 * and every transition goes through the Python bridge so Go-only admission, the
 * status/drain protocol and network holds behave exactly as on the desktop.
 */
class DefaultDownloadHost(
    private val context: Context,
    private val bridge: PythonBridge,
    private val sessionRoot: String,
    private val libraryRoot: String,
    private val networkStatusPath: String,
    private val scope: CoroutineScope,
) : DownloadHost {

    private fun call(op: String, args: String, onResult: (JSONObject) -> Unit = {}) {
        scope.launch(Dispatchers.IO) {
            val envelope = JSONObject(bridge.dispatch(op, args))
            withContext(Dispatchers.Main) { onResult(envelope) }
        }
    }

    override fun start() {
        val intent = Intent(context, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START)
        // startForegroundService whenever it exists: a background Go (notification, engine)
        // must still bring the service up, and the service then reaches startForeground.
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
        else @Suppress("DEPRECATION") context.startService(intent)
        call("worker_start", JSONObject()
            .put("session_root", sessionRoot)
            .put("library_root", libraryRoot)
            .put("network_status_path", networkStatusPath).toString())
    }

    override fun go() {
        start()
        call("go", JSONObject().put("root", workspaceRoot()).toString())
    }

    override fun pause() {
        call("pause", JSONObject().put("root", workspaceRoot()).toString())
    }

    override fun checkpointAndStop(reason: String) {
        // One stop path (user Stop, notification action, onTimeout): pause the plan, then
        // drain the worker through the existing protocol; the service stops itself after.
        call("pause", JSONObject().put("root", workspaceRoot()).toString())
        call("worker_stop", JSONObject().put("session_root", sessionRoot)
            .put("wait_seconds", 15).toString())
        val intent = Intent(context, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_STOP)
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
        else @Suppress("DEPRECATION") context.startService(intent)
    }

    override fun isRunning(): Boolean {
        val envelope = JSONObject(bridge.dispatch(
            "worker_status", JSONObject().put("session_root", sessionRoot).toString()))
        return envelope.optBoolean("ok") && envelope.getJSONObject("result").optBoolean("running", false)
    }

    private fun workspaceRoot(): String = context.filesDir.resolve("workspace").absolutePath
}
