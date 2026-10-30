package io.github.bcrute.comicarchiver.controller

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.webkit.WebView
import io.github.bcrute.comicarchiver.app
import io.github.bcrute.comicarchiver.library.ArchiveViewer
import io.github.bcrute.comicarchiver.library.ArchiveOpenResult
import io.github.bcrute.comicarchiver.signin.SignInEngine
import io.github.bcrute.comicarchiver.worker.DefaultDownloadHost
import io.github.bcrute.comicarchiver.worker.DownloadHostService
import io.github.bcrute.comicarchiver.runtime.SiteWorkspace
import kotlinx.coroutines.CancellationException
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
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import kotlin.coroutines.resume

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
    @Volatile private var location = context.app.workspace
    private fun downloadHost(target: SiteWorkspace) = DefaultDownloadHost(context, bridge,
        target.session.absolutePath, target.library.absolutePath, target.network.absolutePath, scope)
    private var downloads = downloadHost(location)
    @Volatile private var siteVersion = 0L
    private var workspaceReady = false
    private val siteActions = Mutex()
    private var signInWebView: WebView? = null
    private var surfaceOwner: Any? = null
    private var surfaceFactory: ((String) -> Unit)? = null
    private var surfaceRelease: (() -> Unit)? = null
    private var poll: Job? = null
    /** The site key the engine actually loaded; null while no offered site matches. */
    private var siteLoaded: String? = null
    /** The last user admission: downloads never report Running without it. */
    @Volatile private var admitted = false
    private var admissionVersion = 0L
    private val settingsActions = Mutex()
    private val cloudActions = Mutex()
    private var catalogVersion = 0L
    private val preferences = context.getSharedPreferences("comicarchiver_appearance", Context.MODE_PRIVATE)
    private val sessionActions = context.getSharedPreferences("comicarchiver_session_actions", Context.MODE_PRIVATE)

    private val _state = MutableStateFlow(
        AppState(site = location.key, signIn = SignInState.SignedOut, download = DownloadState.Held,
                 library = emptyList(), notice = null,
                 sites = context.app.siteChoices(), siteBusy = true, libraryId = location.libraryId, workspaceReady = false,
                 signOutPending = sessionActions.getBoolean("signout_${location.key}", false),
                 theme = AppTheme.entries.firstOrNull { it.name == preferences.getString("theme", null) }
                     ?: AppTheme.SYSTEM))
    override val state: StateFlow<AppState> = _state.asStateFlow()

    init {
        // Arm the persisted site before an Activity attaches its profile-bound surface.
        // Core preparation completes before restore or any download admission.
        loadSite(_state.value.site)
        scope.launch {
            val opened = withContext(Dispatchers.IO) { prepareWorkspace(location) }
            workspaceReady = opened.optBoolean("ok")
            _state.value = _state.value.copy(siteBusy = false, workspaceReady = workspaceReady,
                notice = if (workspaceReady) _state.value.notice else workspaceNotice(opened))
            if (workspaceReady && !_state.value.signOutPending) signInWebView?.let(engine::restoreSignIn)
            // The saved library is useful while held, too. Reading it must not depend
            // on Go (and this poll cannot admit downloads: mapWorkerStatus checks
            // admitted before consulting live worker state).
            if (workspaceReady) startPolling()
        }
        scope.launch {
            engine.state.collect { value ->
                if (value !is SignInState.SignedIn || value.site == location.key)
                    _state.value = _state.value.copy(signIn = value, signInOrigin = engine.currentOrigin())
            }
        }
    }

    /** The UI's composable-hosted WebView; attach before startSignIn. Not part of the frozen interface. */
    fun attachSignInWebView(webView: WebView, siteKey: String = location.key, libraryId: String = location.libraryId) {
        if (siteKey != location.key || libraryId != location.libraryId) return
        signInWebView = webView
        if (workspaceReady && !_state.value.siteBusy && !_state.value.libraryBusy && !_state.value.signOutPending) engine.restoreSignIn(webView)
    }

    fun attachSurfaceFactory(owner: Any, release: () -> Unit = {}, factory: (String) -> Unit) {
        surfaceOwner = owner
        surfaceFactory = factory
        surfaceRelease = release
    }

    fun detachSurfaceFactory(owner: Any) {
        if (surfaceOwner === owner) { surfaceOwner = null; surfaceFactory = null; surfaceRelease = null }
    }

    /** Release the activity surface; verified auth and download admission live on. */
    fun detachSignInWebView(webView: WebView) {
        if (signInWebView !== webView) return
        val signIn = engine.state.value
        if (signIn is SignInState.Page || signIn is SignInState.Settling ||
            signIn is SignInState.Restoring) engine.cancelSignIn()
        signInWebView = null
    }

    private fun contextHasActivity(): Boolean {
        val auth = engine.state.value
        return admitted || _state.value.download is DownloadState.Planning ||
            _state.value.download is DownloadState.Running || _state.value.settingsBusy ||
            _state.value.cloudBusy || _state.value.cloud?.taskRunning == true ||
            _state.value.cloud?.authStatus == "running" || context.app.exports.isActive ||
            auth is SignInState.Page || auth is SignInState.Settling || auth is SignInState.Restoring
    }

    /** Process-owned import lease; activity recreation cannot silently release this admission guard. */
    internal fun reserveLibraryChange(): Boolean {
        if (_state.value.siteBusy || _state.value.libraryBusy || _state.value.signOutPending || contextHasActivity()) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish sign-in, saving, export or cloud activity before changing the working library."))
            return false
        }
        _state.value = _state.value.copy(libraryBusy = true, notice = null)
        return true
    }

    internal fun releaseLibraryChange() {
        _state.value = _state.value.copy(libraryBusy = false)
        if (workspaceReady && !_state.value.siteBusy && !_state.value.signOutPending) {
            try { signInWebView?.let(engine::restoreSignIn) }
            catch (_: Exception) { _state.value = _state.value.copy(notice = Notice(BetaCodes.LOGIN_REQUIRED,
                "Open the site to verify sign-in for this working library.")) }
        }
    }

    override fun selectSite(key: String) {
        if (key == location.key && workspaceReady || _state.value.siteBusy || _state.value.libraryBusy) return
        if (_state.value.signOutPending || contextHasActivity()) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish sign-in, saving, export or cloud activity before switching sites."))
            return
        }
        val choice = _state.value.sites.firstOrNull { it.key == key }
        if (choice == null || !choice.enabled) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.UNSUPPORTED_SITE,
                choice?.reason ?: "Choose a supported site."))
            return
        }
        val target = context.app.workspaceFor(key)
        scope.launch { switchWorkspace(target, false) }
    }

    /** Only the import/location coordinator calls this while holding its process lease. */
    internal suspend fun selectWorkingLibrary(id: String, siteKey: String = location.key): Boolean {
        if (!_state.value.libraryBusy || _state.value.siteBusy || contextHasActivity()) return false
        if (location.libraryId == id && location.key == siteKey && workspaceReady) return true
        return switchWorkspace(context.app.workspaceFor(siteKey, id), true)
    }

    private suspend fun switchWorkspace(target: SiteWorkspace, commitLibrary: Boolean): Boolean {
        val previous = location
        val before = _state.value
        val previousReady = workspaceReady
        val version = ++siteVersion
        admissionVersion++; catalogVersion++
        admitted = false
        val oldPoll = poll
        oldPoll?.cancel(); poll = null
        _state.value = before.copy(siteBusy = true, notice = null)
        var successful = false
        siteActions.withLock {
            oldPoll?.join()
            settingsActions.withLock {
                cloudActions.withLock {
                    var coreChanged = false
                    var runtimeChanged = false
                    var failure: Notice? = null
                    try {
                        if (previousReady && !downloads.drain()) throw IllegalStateException("worker did not drain")
                        val opened = withContext(Dispatchers.IO) { prepareWorkspace(target) }
                        if (!opened.optBoolean("ok")) failure = workspaceNotice(opened)
                        else {
                            coreChanged = true
                            engine.load(target.key)
                            context.app.selectWorkspace(target.key, target.libraryId)
                            runtimeChanged = true
                            if (commitLibrary) withContext(Dispatchers.IO) { context.app.libraryLocations.select(target.libraryId) }
                            location = target
                            downloads = downloadHost(target)
                            siteLoaded = target.key
                            workspaceReady = true
                            try { surfaceRelease?.invoke() } catch (_: Exception) { }
                            signInWebView = null
                            _state.value = AppState(target.key, engine.state.value, DownloadState.Held, emptyList(), null,
                                theme = before.theme, sites = before.sites, libraryId = target.libraryId,
                                libraryBusy = before.libraryBusy, workspaceReady = true,
                                signOutPending = sessionActions.getBoolean("signout_${target.key}", false))
                            successful = true
                        }
                    } catch (cancelled: CancellationException) { throw cancelled }
                      catch (_: Exception) {
                        failure = Notice(BetaCodes.JOB_FAILED,
                            "The working library could not be opened. The previous library is retained. Try again.")
                    }
                    if (failure != null) {
                        workspaceReady = previousReady
                        if (runtimeChanged || commitLibrary) {
                            try {
                                withContext(Dispatchers.IO) { context.app.libraryLocations.rollbackSelection(previous.libraryId) }
                                context.app.restoreWorkspaceSelection(previous)
                            } catch (_: Exception) { workspaceReady = false }
                        }
                        if (coreChanged && previousReady) {
                            val restored = withContext(Dispatchers.IO) { prepareWorkspace(previous) }
                            workspaceReady = workspaceReady && restored.optBoolean("ok")
                            if (workspaceReady) {
                                try { engine.load(previous.key) } catch (_: Exception) { workspaceReady = false }
                            }
                        }
                        location = previous
                        downloads = downloadHost(previous)
                        _state.value = before.copy(download = DownloadState.Held, siteBusy = false,
                            workspaceReady = workspaceReady, signIn = engine.state.value, notice = failure,
                            catalog = before.catalog.copy(loading = false, openingId = null))
                        if (workspaceReady && coreChanged) signInWebView?.let(engine::restoreSignIn)
                    }
                    if (version == siteVersion) _state.value = _state.value.copy(siteBusy = false)
                }
            }
            if (workspaceReady) startPolling()
        }
        return successful
    }

    override fun startSignIn() {
        if (_state.value.libraryBusy || _state.value.siteBusy || _state.value.signOutPending || !workspaceReady) return
        if (_state.value.sites.firstOrNull { it.key == location.key }?.signInAvailable == false) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.UNSUPPORTED_SITE,
                "Saved files are available. Update Android System WebView to sign in to this site."))
            return
        }
        if (engine.state.value is SignInState.Restoring || engine.state.value is SignInState.Page ||
            engine.state.value is SignInState.Settling) return
        if (admitted || _state.value.download is DownloadState.Planning ||
            _state.value.download is DownloadState.Running) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Pause saving before starting sign-in again."))
            return
        }
        try { surfaceFactory?.invoke(location.key) } catch (_: Exception) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "The sign-in page could not be opened. Update Android System WebView and try again."))
            return
        }
        val webView = signInWebView
        when {
            siteLoaded == null -> _state.value = _state.value.copy(
                notice = Notice(BetaCodes.UNSUPPORTED_SITE,
                                "This app cannot sign in to that site yet."))
            webView == null -> _state.value = _state.value.copy(
                notice = Notice(BetaCodes.JOB_FAILED, "The sign-in page is not showing yet. Try sign-in again."))
            else -> try {
                engine.startSignIn(webView)
                _state.value = _state.value.copy(notice = null)
            } catch (_: Exception) {
                engine.cancelSignIn()
                _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                    "The sign-in page could not be opened. Update Android System WebView and try again."))
            }
        }
    }

    override fun cancelSignIn() {
        // The engine's own live state decides — not this controller's collected copy,
        // which can lag — so a cancel only ever speaks to an attempt in flight (the Back
        // handler's case). A completed sign-in is not an attempt and is never ended here;
        // the activity's own destruction calls this too, and cookies are never touched.
        val signIn = engine.state.value
        if (signIn is SignInState.Page || signIn is SignInState.Settling ||
            signIn is SignInState.Restoring) engine.cancelSignIn()
    }

    override fun signOut() {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        if (_state.value.cloudBusy || _state.value.cloud?.taskRunning == true ||
            _state.value.cloud?.authStatus == "running" || context.app.exports.isActive) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish export and cloud activity before signing out."))
            return
        }
        admissionVersion++
        admitted = false
        val target = location
        _state.value = _state.value.copy(siteBusy = true, signOutPending = true, notice = null)
        scope.launch {
          siteActions.withLock {
            // Contract §3 order, awaited end to end: the downloader drains before any
            // cookie or record goes, so no in-flight request races the cleared store; the
            // engine closes the session and clears the jar on the main thread; only then
            // is the sign-in record forgotten, so a restart can never resume it.
            try {
                val remembered = withContext(Dispatchers.IO) {
                    sessionActions.edit().putBoolean("signout_${target.key}", true).commit()
                }
                if (!remembered) throw IllegalStateException("sign-out intent not saved")
                if (!downloads.drain()) throw IllegalStateException("worker did not drain")
                val cleared = withTimeoutOrNull(5_000) {
                    suspendCancellableCoroutine<Boolean> { continuation ->
                        engine.signOut { if (continuation.isActive) continuation.resume(it) }
                    }
                } ?: false
                if (!cleared) throw IllegalStateException("profile not cleared")
                // A cleared profile must not retain a live surface with old page/history
                // state. The next explicit sign-in creates a newly bound WebView.
                surfaceRelease?.invoke()
                val forgotten = withContext(Dispatchers.IO) { JSONObject(bridge.dispatch("signout_record",
                    JSONObject().put("session_root", target.session.absolutePath).toString())) }
                if (!forgotten.optBoolean("ok")) throw IllegalStateException("record not forgotten")
                val completed = withContext(Dispatchers.IO) {
                    sessionActions.edit().remove("signout_${target.key}").commit()
                }
                if (!completed) throw IllegalStateException("sign-out completion not saved")
                _state.value = _state.value.copy(download = DownloadState.Held,
                    signOutPending = false, notice = null)
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) {
                _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                    "Sign-out did not finish. The library is retained. Retry sign-out after activity stops."))
            } finally {
                _state.value = _state.value.copy(siteBusy = false)
            }
          }
        }
    }

    override fun go() {
        // UI disabling can lag a tap. Admission belongs to the controller, including
        // the interval before the host answers and while observations are stale.
        if (admitted || _state.value.download is DownloadState.Planning ||
            _state.value.download is DownloadState.Running) return
        if (_state.value.libraryBusy) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish or stop the working-library change before pressing Go."))
            return
        }
        if (_state.value.signOutPending) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish signing out before starting another download."))
            return
        }
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady || engine.state.value !is SignInState.SignedIn) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.LOGIN_REQUIRED,
                "Open the site and finish sign-in before pressing Go."))
            return
        }
        if (_state.value.cloudBusy || _state.value.cloud?.taskRunning == true ||
            _state.value.cloud?.authStatus == "running") {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish the cloud activity before pressing Go."))
            return
        }
        val settings = _state.value.settings
        if (settings != null && settings.speeds.none { it.key == settings.speed && it.available }) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "The stored speed cannot run here. Choose Medium or Slow in Settings, or update Android System WebView."))
            return
        }
        val version = ++admissionVersion
        admitted = false
        poll?.cancel()
        _state.value = _state.value.copy(download = DownloadState.Planning, notice = null,
            admittedAtMillis = SystemClock.elapsedRealtime(), observations = null, statusReadFailed = false)
        downloads.go result@{ error ->
            if (version != admissionVersion) return@result
            admitted = error == null
            if (error == BetaCodes.LOGIN_REQUIRED) engine.requireSignIn()
            _state.value = _state.value.copy(download =
                if (error == null) DownloadState.Planning else DownloadState.Failed(error),
                admittedAtMillis = _state.value.admittedAtMillis.takeIf { error == null })
            startPolling()
        }
    }

    override fun pause() {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        admissionVersion++
        admitted = false
        downloads.pause()
        _state.value = _state.value.copy(download = DownloadState.Paused("user"))
        startPolling()
    }

    override fun stop() {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        admissionVersion++
        admitted = false
        downloads.checkpointAndStop("user")
        _state.value = _state.value.copy(download = DownloadState.Held)
    }

    override fun refreshSettings() {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        val target = location; val version = siteVersion
        scope.launch(Dispatchers.IO) { readSettings(target, version) }
    }

    override fun setTheme(value: AppTheme) {
        preferences.edit().putString("theme", value.name).apply()
        _state.value = _state.value.copy(theme = value)
    }

    override fun setWifiOnly(value: Boolean) {
        changeSettings(JSONObject().put("wifi_only", value))
    }

    override fun setDownloadSpeed(value: String) {
        changeSettings(JSONObject().put("speed", value))
    }

    override fun setSiteDownloadSpeed(site: String, value: String) {
        if (site == location.key) return
        changeSettings(JSONObject().put("site_key", site).put("speed", value))
    }

    override fun refreshCloud() {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        val target = location; val version = siteVersion
        scope.launch(Dispatchers.IO) { readCloud(target, version) }
    }

    override fun cloudAction(request: CloudRequest) {
        if (_state.value.libraryBusy || _state.value.cloudBusy || _state.value.siteBusy || _state.value.signOutPending || !workspaceReady) return
        val target = location; val version = siteVersion
        val needsWorker = request is CloudRequest.Test || request is CloudRequest.Retry ||
            request is CloudRequest.Cleanup || request is CloudRequest.Resume
        if (needsWorker && _state.value.signIn !is SignInState.SignedIn) {
            _state.value = _state.value.copy(cloudNotice = Notice(BetaCodes.LOGIN_REQUIRED,
                "Sign in to the site before starting a cloud task."))
            return
        }
        _state.value = _state.value.copy(cloudBusy = true, cloudNotice = null)
        scope.launch(Dispatchers.IO) {
            cloudActions.withLock {
                var refusal: String? = null
                val result = try {
                    if (needsWorker) {
                        val intent = Intent(context, DownloadHostService::class.java)
                            .setAction(DownloadHostService.ACTION_START)
                            .putExtra(DownloadHostService.EXTRA_WORKSPACE, target.root.absolutePath)
                        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
                        else @Suppress("DEPRECATION") context.startService(intent)
                    }
                    val answer = bridge.dispatch("cloud_action", request.json().put("root", target.root.absolutePath).toString())
                    val envelope = JSONObject(answer)
                    if (envelope.opt("ok") == true) CloudStatus.parse(answer)
                    else {
                        refusal = envelope.optString("message").takeIf { it.isNotBlank() && it.length <= 1500 }
                        null
                    }
                } catch (cancelled: CancellationException) { throw cancelled }
                  catch (_: Exception) { null }
                withContext(Dispatchers.Main) {
                    if (!current(target, version)) return@withContext
                    _state.value = _state.value.copy(cloud = result ?: _state.value.cloud, cloudBusy = false,
                        cloudNotice = if (result == null) Notice(BetaCodes.JOB_FAILED,
                            refusal ?: "The cloud action did not finish. Refresh cloud settings and try again.") else null)
                }
            }
            // Re-read actual policy even when the mutation returned a refusal.
            readCloud(target, version)
        }
    }

    private suspend fun readCloud(target: SiteWorkspace, version: Long) {
        cloudActions.withLock {
            val status = try {
                CloudStatus.parse(bridge.dispatch("cloud_status", JSONObject().put("root", target.root.absolutePath).toString()))
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { null }
            withContext(Dispatchers.Main) {
                if (!current(target, version)) return@withContext
                _state.value = _state.value.copy(cloud = status ?: _state.value.cloud,
                    cloudCheckedAtMillis = if (status != null) SystemClock.elapsedRealtime() else _state.value.cloudCheckedAtMillis,
                    cloudReadFailed = status == null,
                    cloudNotice = if (status == null) Notice(BetaCodes.JOB_FAILED,
                        "Cloud settings could not be read. Try Refresh cloud settings.") else _state.value.cloudNotice)
            }
        }
    }

    override fun loadCatalog(query: CatalogQuery, refresh: Boolean) {
        if (_state.value.siteBusy || !workspaceReady) return
        val target = location; val site = siteVersion
        val prior = _state.value.catalog
        if (prior.loading && prior.query == query && !refresh) return
        val version = ++catalogVersion
        _state.value = _state.value.copy(catalog = prior.copy(query = query, loading = true,
            error = null, feedback = null, feedbackFor = null))
        scope.launch(Dispatchers.IO) {
            val page = try {
                FileCatalog.parse(bridge.dispatch("file_catalog", JSONObject().put("root", target.root.absolutePath)
                    .put("options", query.json(refresh)).toString()))
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { null }
            withContext(Dispatchers.Main) {
                if (version != catalogVersion || !current(target, site)) return@withContext
                _state.value = _state.value.copy(catalog = _state.value.catalog.copy(
                    page = page ?: _state.value.catalog.page, loading = false,
                    query = if (page == null) query else query.copy(offset = page.offset, limit = page.limit),
                    error = if (page == null) "Saved files could not be loaded. Try Refresh files." else null,
                ))
            }
        }
    }

    override fun openSavedFile(id: String) {
        if (_state.value.siteBusy || !workspaceReady) return
        val target = location; val version = siteVersion
        if (_state.value.catalog.openingId != null) return
        _state.value = _state.value.copy(catalog = _state.value.catalog.copy(openingId = id,
            feedback = "Opening in your device's reader…", feedbackFor = id))
        scope.launch(Dispatchers.IO) {
            val file = try {
                val answer = JSONObject(bridge.dispatch("file_open", JSONObject().put("root", target.root.absolutePath)
                    .put("id", id).toString()))
                if (answer.optBoolean("ok")) answer.getJSONObject("result") else null
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { null }
            withContext(Dispatchers.Main) {
                if (!current(target, version)) return@withContext
                val result = file?.let { ArchiveViewer(context, target.library).open(it.optString("relpath"), it.optString("kind")) }
                val feedback = when (result) {
                    ArchiveOpenResult.REQUESTED -> "Sent to your device's reader."
                    ArchiveOpenResult.NO_READER -> "No app could open this file. Choose a CBZ or EPUB reader and try again."
                    else -> "This file could not be opened. Refresh files and try again."
                }
                _state.value = _state.value.copy(catalog = _state.value.catalog.copy(
                    openingId = null, feedback = feedback, feedbackFor = id))
            }
        }
    }

    private fun changeSettings(args: JSONObject) {
        if (_state.value.libraryBusy || _state.value.siteBusy || _state.value.signOutPending ||
            _state.value.settingsBusy || !workspaceReady) return
        val target = location; val version = siteVersion
        _state.value = _state.value.copy(settingsBusy = true, settingsNotice = null)
        scope.launch(Dispatchers.IO) {
            settingsActions.withLock {
                val settings = try {
                    SettingsStatus.fromEnvelope(JSONObject(bridge.dispatch("configure_settings",
                        args.put("root", target.root.absolutePath).toString())))
                } catch (_: Exception) { null }
                val actual = settings ?: try {
                    SettingsStatus.fromEnvelope(JSONObject(bridge.dispatch("settings",
                        JSONObject().put("root", target.root.absolutePath).toString())))
                } catch (_: Exception) { null }
                withContext(Dispatchers.Main) {
                    if (!current(target, version)) return@withContext
                    _state.value = _state.value.copy(
                        settings = actual,
                        settingsBusy = false,
                        settingsNotice = if (settings == null) Notice(BetaCodes.JOB_FAILED,
                            "The download setting could not be saved. Try again.") else null,
                    )
                }
            }
        }
    }

    private suspend fun readSettings(target: SiteWorkspace, version: Long) {
        settingsActions.withLock {
            val settings = try {
                SettingsStatus.fromEnvelope(JSONObject(bridge.dispatch("settings",
                    JSONObject().put("root", target.root.absolutePath).toString())))
            } catch (_: Exception) { null }
            withContext(Dispatchers.Main) {
                if (!current(target, version)) return@withContext
                _state.value = _state.value.copy(
                    settings = settings,
                    settingsNotice = if (settings == null) Notice(BetaCodes.JOB_FAILED,
                        "Download settings could not be read. Try again.") else null,
                )
            }
        }
    }

    override fun requestExport(folderUri: String) {
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        val target = location; val version = siteVersion
        scope.launch(Dispatchers.IO) {
            val envelope = try {
                JSONObject(bridge.dispatch(
                    "export_manifest", JSONObject().put("root", target.root.absolutePath).toString()))
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { JSONObject().put("ok", false) }
            if (!envelope.optBoolean("ok")) {
                withContext(Dispatchers.Main) { if (current(target, version)) _state.value = _state.value.copy(notice = Notice(
                    envelope.optString("code", BetaCodes.JOB_FAILED),
                    envelope.optString("message", "The export could not start."))) }
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

    private fun prepareWorkspace(target: SiteWorkspace): JSONObject = try {
        check(target.available && context.app.libraryLocations.root(target.libraryId) == target.base) { "Working library unavailable" }
        JSONObject(bridge.dispatch("prepare_workspace", JSONObject().put("root", target.root.absolutePath)
            .put("base_root", target.base.absolutePath).put("site_key", target.key)
            .put("network_schema", 1).toString()))
    } catch (_: Exception) {
        JSONObject().put("ok", false).put("message", "The workspace could not be opened. Try the site again.")
    }

    private fun workspaceNotice(answer: JSONObject) = Notice(answer.optString("code", BetaCodes.JOB_FAILED),
        answer.optString("message", "The workspace could not be opened. Try the site again."))

    private fun current(target: SiteWorkspace, version: Long): Boolean =
        siteVersion == version && location.identity == target.identity && location.root == target.root

    /** Arm the engine with one site key. An unoffered key leaves the engine untouched and
     *  reports [BetaCodes.UNSUPPORTED_SITE]; a successful load only then updates state. */
    private fun loadSite(key: String) {
        try {
            engine.load(key)
            siteLoaded = key
        } catch (_: Exception) {
            siteLoaded = null
            _state.value = _state.value.copy(
                notice = Notice(BetaCodes.UNSUPPORTED_SITE,
                                "This app cannot sign in to that site yet."))
        }
    }

    private fun startPolling() {
        poll?.cancel()
        val target = location; val version = siteVersion
        // The poll's bridge calls block on Python: keep them off the main thread.
        poll = scope.launch(Dispatchers.IO) {
            var tick = 0
            while (isActive) {
                if (admitted || tick % 5 == 0) mapWorkerStatus(target, version)
                if (tick % 5 == 0) {
                    try { refreshLibrary(target, version) }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { /* A library read failure must not stop live status/Pause updates. */ }
                    readSettings(target, version)
                    readCloud(target, version)
                } else if (_state.value.cloud?.taskRunning == true || _state.value.cloud?.authStatus == "running") {
                    readCloud(target, version)
                }
                tick += 1
                delay(2000)
            }
        }
    }

    private suspend fun mapWorkerStatus(target: SiteWorkspace, version: Long) {
        val admission = admissionVersion
        val envelope = try {
            JSONObject(bridge.dispatch("download_status", JSONObject().put("root", target.root.absolutePath).toString()))
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            JSONObject()
        }
        withContext(Dispatchers.Main) {
            if (admission != admissionVersion || !current(target, version)) return@withContext
            val observation = DownloadObservations.parse(envelope, SystemClock.elapsedRealtime())
            val observed = _state.value.copy(observations = observation ?: _state.value.observations,
                statusReadFailed = observation == null)
            // Reading a held plan never admits it. A failed observation also cannot
            // prove that a live task stopped: retain its Pause action and refuse Go.
            if (!admitted || observation == null) {
                val restored = if (observation != null && observed.download is DownloadState.Held)
                    DownloadStatus.restored(envelope) else null
                _state.value = if (restored != null) observed.copy(download = restored) else observed
                return@withContext
            }
            val download = DownloadStatus.fromEnvelope(envelope)
            if (download is DownloadState.Failed && download.code == BetaCodes.LOGIN_REQUIRED)
                engine.requireSignIn()
            if (download is DownloadState.Failed || download is DownloadState.Finished ||
                download is DownloadState.Paused) admitted = false
            // requireSignIn can synchronously publish a newer sign-in state. Apply
            // this observation to that current state rather than restoring its old
            // SignedIn value from the snapshot captured before the callback.
            _state.value = _state.value.copy(download = download, observations = observation, statusReadFailed = false)
        }
    }

    private suspend fun refreshLibrary(target: SiteWorkspace, version: Long) {
        val envelope = JSONObject(bridge.dispatch(
            "library_listing", JSONObject().put("root", target.root.absolutePath).toString()))
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
        withContext(Dispatchers.Main) {
            if (!current(target, version)) return@withContext
            _state.value = _state.value.copy(library = library)
        }
    }
}
