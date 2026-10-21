package io.github.bcrute.comicarchiver.controller

import android.content.Context
import android.webkit.WebView
import io.github.bcrute.comicarchiver.app
import io.github.bcrute.comicarchiver.signin.SignInEngine
import io.github.bcrute.comicarchiver.worker.DefaultDownloadHost
import io.github.bcrute.comicarchiver.worker.DownloadHost
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * Runtime AppController (native line) against the frozen contract. Every download action
 * crosses the Python bridge, so Go-only admission, holds and the drain protocol behave
 * exactly as on the desktop; the controller only maps public state, never policy.
 *
 * The UI attaches its sign-in WebView before [startSignIn] through [attachSignInWebView]
 * (the frozen interface deliberately carries no WebView parameter). Still unimplemented
 * bridge ops (library/export listing) surface as their public-safe notices instead of
 * silent successes.
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
        downloads.checkpointAndStop("sign_out")  // The downloader stops before the session goes.
        engine.signOut()
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
        _state.value = _state.value.copy(notice = notice)
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
        poll = scope.launch {
            while (isActive) {
                mapWorkerStatus()
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
}
