package io.github.bcrute.comicarchiver.worker

import android.content.Context
import android.content.Intent
import android.os.Build
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.CancellationException
import io.github.bcrute.comicarchiver.controller.BetaCodes
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

    private val actions = Mutex()

    override fun start() {
        scope.launch(Dispatchers.IO) { startWorker() }
    }

    override fun go(onResult: (String?) -> Unit) {
        scope.launch(start = CoroutineStart.UNDISPATCHED) {
            val error = actions.withLock {
                withContext(Dispatchers.IO) { try {
                    startService()
                    DownloadCommands(bridge, workspaceRoot(), workerArgs()).go()
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    BetaCodes.JOB_FAILED
                } }
            }
            withContext(Dispatchers.Main) { onResult(error) }
        }
    }

    override fun pause() {
        scope.launch(start = CoroutineStart.UNDISPATCHED) {
            actions.withLock {
                withContext(Dispatchers.IO) {
                    bridge.dispatch("pause", JSONObject().put("root", workspaceRoot()).toString())
                }
            }
        }
    }

    override fun checkpointAndStop(reason: String) {
        // One stop path (user Stop, notification action, onTimeout): pause the plan, then
        // drain the worker through the existing protocol — in order, so the drain cannot
        // overtake the pause — and the service stops itself after.
        scope.launch(Dispatchers.IO) {
            JSONObject(bridge.dispatch("pause",
                JSONObject().put("root", workspaceRoot()).toString()))
            JSONObject(bridge.dispatch("worker_stop",
                JSONObject().put("session_root", sessionRoot).put("wait_seconds", 15).toString()))
            val intent = Intent(context, DownloadHostService::class.java)
                .setAction(DownloadHostService.ACTION_STOP)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else @Suppress("DEPRECATION") context.startService(intent)
        }
    }

    private fun workerArgs(): String = JSONObject()
        .put("session_root", sessionRoot).put("library_root", libraryRoot)
        .put("network_status_path", networkStatusPath).toString()

    private fun startWorker() {
        startService()
        JSONObject(bridge.dispatch("worker_start", workerArgs()))
    }

    private fun startService() {
        val intent = Intent(context, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START)
        // startForegroundService whenever it exists: a background Go (notification, engine)
        // must still bring the service up, and the service then reaches startForeground.
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
