package io.github.bcrute.comicarchiver.controller

import android.content.Context
import android.content.Intent
import android.os.Build
import android.webkit.WebView
import io.github.bcrute.comicarchiver.app
import io.github.bcrute.comicarchiver.signin.SignInEngine
import io.github.bcrute.comicarchiver.worker.DefaultDownloadHost
import io.github.bcrute.comicarchiver.worker.DownloadHost
import io.github.bcrute.comicarchiver.worker.DownloadHostService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Runtime AppController (native line) against the frozen contract. Every download action
 * crosses the Python bridge, so Go-only admission, holds and the drain protocol behave
 * exactly as on the desktop; the controller only maps public state, never policy.
 *
 * The UI attaches its sign-in WebView before [startSignIn] through [attachSignInWebView]
 * (the frozen interface deliberately carries no WebView parameter).
 */
class DefaultAppController(
    private val context: Context,
    private val engine: SignInEngine,
    private val scope: CoroutineScope,
) : AppController {

    private val bridge get() = context.app.bridge
    private val workspaceRoot = context.filesDir.resolve("workspace").absolutePath
    private val sessionRoot = context.filesDir.resolve("workspace/.comic-session").absolutePath
    private val downloads: DownloadHost = DefaultDownloadHost(
        context, bridge, sessionRoot, workspaceRoot + "/Library", workspaceRoot + "/net.json", scope)
    private var signInWebView: WebView? = null
    private var poll: Job? = null
    /** The last user admission: downloads never report Running without it. */
    @Volatile private var admitted = false

    private val _state = MutableStateFlow(
        AppState(site = "tapas", signIn = SignInState.SignedOut, download = DownloadState.Held,
                 library = emptyList(), notice = null))
    override val state: StateFlow<AppState> = _state.asStateFlow()

    init {
        scope.launch { prepareWorkspace() }
        scope.launch {
            engine.state.collect { value -> _state.value = _state.value.copy(signIn = value) }
        }
    }

    /** The UI's composable-hosted WebView; attach before startSignIn. Not part of the frozen interface. */
    fun attachSignInWebView(webView: WebView) {
        signInWebView = webView
    }

    override fun selectSite(key: String) {
        engine.load(key)
        _state.value = _state.value.copy(site = key)
    }

    override fun startSignIn() {
        val webView = signInWebView
        if (webView == null) {
            _state.value = _state.value.copy(
                notice = Notice(BetaCodes.WORKER_OFFLINE, "The sign-in page is not showing yet."))
        } else {
            engine.startSignIn(webView)
        }
    }

    override fun cancelSignIn() = engine.cancelSignIn()

    override fun signOut() {
        scope.launch(Dispatchers.IO) {
            // Contract §3 order, awaited end to end: the downloader drains before any
            // cookie or record goes, so no in-flight request races the cleared store; the
            // engine closes the session and clears the jar on the main thread; only then
            // is the sign-in record forgotten, so a restart can never resume it.
            bridge.dispatch("pause", JSONObject().put("root", workspaceRoot).toString())
            bridge.dispatch("worker_stop", JSONObject().put("session_root", sessionRoot)
                .put("wait_seconds", 15).toString())
            withContext(Dispatchers.Main) { engine.signOut() }
            bridge.dispatch("signout_record", JSONObject().put("session_root", sessionRoot).toString())
            val intent = Intent(context, DownloadHostService::class.java)
                .setAction(DownloadHostService.ACTION_STOP)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else @Suppress("DEPRECATION") context.startService(intent)
            admitted = false
            withContext(Dispatchers.Main) {
                _state.value = _state.value.copy(download = DownloadState.Held)
            }
        }
    }

    override fun go() {
        admitted = true
        _state.value = _state.value.copy(download = DownloadState.Planning)
        downloads.go()
        startPolling()
    }

    override fun pause() {
        admitted = false
        downloads.pause()
        _state.value = _state.value.copy(download = DownloadState.Paused("user"))
        startPolling()
    }

    override fun stop() {
        admitted = false
        downloads.checkpointAndStop("user")
        _state.value = _state.value.copy(download = DownloadState.Held)
    }

    override fun requestExport(folderUri: String) {
        scope.launch(Dispatchers.IO) {
            val envelope = JSONObject(bridge.dispatch(
                "export_manifest", JSONObject().put("root", workspaceRoot).toString()))
            if (!envelope.optBoolean("ok")) {
                _state.value = _state.value.copy(notice = Notice(
                    envelope.optString("code", BetaCodes.JOB_FAILED),
                    envelope.optString("message", "The export could not start.")))
            }
        }
    }

    override fun cancelExport() {
        // The exporter's own cancel path belongs to the UI/export line; nothing to ask yet.
    }

    override fun openNotice(notice: Notice) {
        // The UI's "Got it" calls this with the shown notice: asserting the same notice
        // again clears it (dismiss semantics); a different one replaces it.
        _state.value = if (_state.value.notice == notice) _state.value.copy(notice = null)
        else _state.value.copy(notice = notice)
    }

    private suspend fun prepareWorkspace() {
        val envelope = JSONObject(bridge.dispatch(
            "prepare_workspace", JSONObject().put("root", workspaceRoot).toString()))
        if (!envelope.optBoolean("ok")) {
            _state.value = _state.value.copy(notice = Notice(
                envelope.optString("code", BetaCodes.JOB_FAILED),
                envelope.optString("message", "The workspace could not be opened.")))
        }
        // Cold start is Held whatever the workspace said; a held download waits for Go.
    }

    private fun startPolling() {
        poll?.cancel()
        // The poll's bridge calls block on Python: keep them off the main thread.
        poll = scope.launch(Dispatchers.IO) {
            var tick = 0
            while (isActive) {
                mapWorkerStatus()
                if (tick % 5 == 0) refreshLibrary()  // Cheap stat walk; the manifest ops carry the digests.
                tick += 1
                delay(2000)
            }
        }
    }

    private suspend fun mapWorkerStatus() {
        val envelope = JSONObject(bridge.dispatch(
            "worker_status", JSONObject().put("session_root", sessionRoot).toString()))
        if (!envelope.optBoolean("ok")) return
        val result = envelope.getJSONObject("result")
        val running = result.optBoolean("running", false)
        val workerState = result.optString("state", "unknown")
        val download = when {
            !running || !admitted -> _state.value.download.let {
                if (!admitted && it is DownloadState.Running) DownloadState.Held else it
            }
            workerState == "busy" -> DownloadState.Running(0, 0)  // Counts arrive with the manifest ops.
            else -> DownloadState.Planning
        }
        _state.value = _state.value.copy(download = download)
    }

    private suspend fun refreshLibrary() {
        val envelope = JSONObject(bridge.dispatch(
            "library_listing", JSONObject().put("root", workspaceRoot).toString()))
        if (!envelope.optBoolean("ok")) return
        val entries = envelope.optJSONArray("result") ?: return
        val library = (0 until entries.length()).mapNotNull { index ->
            val item = entries.optJSONObject(index) ?: return@mapNotNull null
            LibraryEntry(
                seriesId = item.optString("seriesId"),
                title = item.optString("title"),
                kind = if (item.optString("kind") == "novels") LibraryKind.NOVELS
                       else LibraryKind.COMIC,
                episodeCount = item.optInt("episodeCount", 0),
                savedCount = item.optInt("savedCount", 0),
            )
        }
        _state.value = _state.value.copy(library = library)
    }
}
