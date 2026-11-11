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
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
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
    /** Go was pressed and the host has not answered yet; the site stays shown until it does. */
    private val goPending = MutableStateFlow(false)
    /** Sites left saving while another is shown, as each desktop site saves beside the others. */
    private class Away(val host: DefaultDownloadHost, val download: DownloadState,
        val observations: DownloadObservations?, val admittedAtMillis: Long?, var watch: Job? = null)
    private val away = java.util.concurrent.ConcurrentHashMap<String, Away>()

    /** Whether a site is saving now, shown or not. The reading browser asks this off the main thread. */
    fun isSaving(key: String): Boolean {
        if (away.keys.any { it == context.app.workspaceFor(key).identity }) return true
        val shown = _state.value
        return shown.site == key && (admitted || shown.download is DownloadState.Planning ||
            shown.download is DownloadState.Running)
    }

    private fun withSaving(sites: List<SiteChoice>): List<SiteChoice> =
        sites.map { it.copy(saving = isSaving(it.key)) }

    /** Keep watch on a site left saving, so its mark goes when its download ends by itself. */
    private fun watch(target: SiteWorkspace): Job = scope.launch(Dispatchers.IO) {
        while (isActive) {
            delay(5000)
            val envelope = try {
                JSONObject(bridge.dispatch("download_status",
                    JSONObject().put("root", target.root.absolutePath).toString()))
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { null }
            // A failed read cannot prove that a live task stopped, and a wait for the
            // connection ends by itself: only an ended download loses its mark.
            val download = envelope?.takeIf { it.optBoolean("ok") }?.let(DownloadStatus::fromEnvelope)
            if (download is DownloadState.Failed || download is DownloadState.Finished ||
                download == DownloadState.Paused("user")) {
                withContext(Dispatchers.Main) {
                    away.remove(target.identity)
                    _state.value = _state.value.copy(sites = withSaving(_state.value.sites))
                }
                return@launch
            }
        }
    }
    private var allJob: Job? = null
    @Volatile private var othersOpenedFor: String? = null

    /** As the desktop's Start all: every site that is signed in and not saving is started in turn.
     *  A site's sign-in lives with its own screen, so each is shown while it is started. */
    override fun goAll() {
        if (allJob?.isActive == true || _state.value.siteBusy || _state.value.libraryBusy || !workspaceReady) return
        if (_state.value.signOutPending || screenHasActivity()) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish sign-in, export or cloud setup before starting every site."))
            return
        }
        val home = location.key
        val keys = listOf(home) + _state.value.sites.filter { it.enabled && it.signInAvailable && it.key != home }.map { it.key }
        allJob = scope.launch {
            _state.value = _state.value.copy(startingAll = true)
            try {
                for (key in keys) {
                    if (isSaving(key)) continue
                    if (location.key != key && !show(key)) break
                    if (!signedInHere(key) || !mayStartHere()) continue
                    go()
                    withTimeoutOrNull(60_000) { goPending.first { !it } }
                }
                if (location.key != home) show(home)
            } finally {
                _state.value = _state.value.copy(startingAll = false)
            }
        }
    }

    private suspend fun show(key: String): Boolean {
        if (_state.value.signOutPending || screenHasActivity() || _state.value.siteBusy || _state.value.libraryBusy) return false
        // A switch always runs to its end, so Pause all cannot leave one half done.
        return withContext(NonCancellable) { switchWorkspace(context.app.workspaceFor(key), false) }
    }

    /** Wait for the shown site's saved sign-in to be checked; only a standing sign-in starts. */
    private suspend fun signedInHere(key: String): Boolean {
        var checking = false
        val settled = withTimeoutOrNull(15_000) {
            engine.state.first { now ->
                if (now is SignInState.Restoring) checking = true
                (now as? SignInState.SignedIn)?.site == key || checking && now !is SignInState.Restoring
            }
        }
        return (settled as? SignInState.SignedIn)?.site == key
    }

    /** What Go itself would refuse with a message is passed over quietly for Start all. */
    private fun mayStartHere(): Boolean {
        val shown = _state.value
        val settings = shown.settings
        return workspaceReady && !shown.signOutPending && !shown.cloudBusy && shown.cloud?.taskRunning != true &&
            shown.cloud?.authStatus != "running" &&
            (settings == null || settings.speeds.any { it.key == settings.speed && it.available })
    }

    override fun pauseAll() {
        allJob?.cancel()
        if (_state.value.libraryBusy || _state.value.siteBusy || !workspaceReady) return
        for (kept in away.values) { kept.watch?.cancel(); kept.host.pause() }
        away.clear()
        _state.value = _state.value.copy(sites = withSaving(_state.value.sites))
        if (admitted || _state.value.download is DownloadState.Planning ||
            _state.value.download is DownloadState.Running) pause()
        _state.value = _state.value.copy(sites = withSaving(_state.value.sites))
    }

    /** Open the library's other sites for their counts, as the desktop opens every site at once. */
    private suspend fun openOthers(shown: SiteWorkspace) = withContext(Dispatchers.IO) {
        if (othersOpenedFor == shown.libraryId) return@withContext
        othersOpenedFor = shown.libraryId
        for (choice in context.app.siteChoices()) {
            if (choice.key == shown.key || !choice.enabled) continue
            try {
                val target = context.app.workspaceFor(choice.key, shown.libraryId)
                if (!target.available || context.app.libraryLocations.root(target.libraryId) != target.base) continue
                context.app.keepConnectionReport(target)
                bridge.dispatch("open_site", JSONObject().put("root", target.root.absolutePath)
                    .put("base_root", target.base.absolutePath).put("site_key", target.key).toString())
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { /* Its counts are left out until the site is opened by hand. */ }
        }
    }

    private suspend fun readAllSites(target: SiteWorkspace, version: Long) {
        val result = try {
            JSONObject(bridge.dispatch("all_sites", JSONObject().put("base_root", target.base.absolutePath).toString()))
                .takeIf { it.optBoolean("ok") }?.optJSONObject("result")
        } catch (cancelled: CancellationException) { throw cancelled }
          catch (_: Exception) { null }
        val names = result?.optJSONArray("sites")
        val read = if (result == null || names == null) null else AllSites(
            (0 until names.length()).map { names.optString(it) }, result.optLong("episodes"), result.optLong("files"))
        withContext(Dispatchers.Main) {
            if (current(target, version) && read != null) _state.value = _state.value.copy(allSites = read)
        }
    }
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

    /** Work that belongs to the screen being shown and cannot follow the user to another site. */
    private fun screenHasActivity(): Boolean {
        val auth = engine.state.value
        return goPending.value || _state.value.settingsBusy || _state.value.cloudBusy ||
            _state.value.cloud?.authStatus == "running" || context.app.exports.isActive ||
            auth is SignInState.Page || auth is SignInState.Settling || auth is SignInState.Restoring
    }

    private fun contextHasActivity(): Boolean =
        screenHasActivity() || allJob?.isActive == true || away.isNotEmpty() || admitted || _state.value.download is DownloadState.Planning ||
            _state.value.download is DownloadState.Running || _state.value.cloud?.taskRunning == true

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
        if (key == location.key && workspaceReady || _state.value.siteBusy || _state.value.libraryBusy ||
            allJob?.isActive == true) return
        // A site that is saving goes on saving while another is shown.
        if (_state.value.signOutPending || screenHasActivity()) {
            _state.value = _state.value.copy(notice = Notice(BetaCodes.JOB_FAILED,
                "Finish sign-in, export or cloud setup before switching sites."))
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
        val previousHost = downloads
        // Within one working library the site left behind keeps saving; another library is
        // opened only with nothing saving at all.
        val leftSaving = !commitLibrary && previous.libraryId == target.libraryId &&
            (admitted || before.download is DownloadState.Planning || before.download is DownloadState.Running)
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
                        if (previousReady && !leftSaving && !downloads.drain()) throw IllegalStateException("worker did not drain")
                        val opened = withContext(Dispatchers.IO) { prepareWorkspace(target) }
                        if (!opened.optBoolean("ok")) failure = workspaceNotice(opened)
                        else {
                            coreChanged = true
                            engine.load(target.key)
                            context.app.selectWorkspace(target.key, target.libraryId)
                            runtimeChanged = true
                            if (commitLibrary) withContext(Dispatchers.IO) { context.app.libraryLocations.select(target.libraryId) }
                            if (leftSaving) away[previous.identity] = Away(previousHost, before.download,
                                before.observations, before.admittedAtMillis).also { it.watch = watch(previous) }
                            val returning = away.remove(target.identity)
                            returning?.watch?.cancel()
                            location = target
                            downloads = returning?.host ?: downloadHost(target)
                            admitted = returning != null
                            siteLoaded = target.key
                            workspaceReady = true
                            try { surfaceRelease?.invoke() } catch (_: Exception) { }
                            signInWebView = null
                            _state.value = AppState(target.key, engine.state.value, returning?.download ?: DownloadState.Held,
                                emptyList(), null, theme = before.theme, sites = before.sites, libraryId = target.libraryId,
                                libraryBusy = before.libraryBusy, workspaceReady = true,
                                observations = returning?.observations, admittedAtMillis = returning?.admittedAtMillis,
                                startingAll = before.startingAll,
                                allSites = before.allSites.takeIf { before.libraryId == target.libraryId },
                                signOutPending = sessionActions.getBoolean("signout_${target.key}", false))
                            _state.value = _state.value.copy(sites = withSaving(before.sites))
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
                        // A site that was saving and was never drained is still saving.
                        downloads = if (leftSaving) previousHost else downloadHost(previous)
                        admitted = leftSaving
                        _state.value = before.copy(download = if (leftSaving) before.download else DownloadState.Held,
                            siteBusy = false,
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
        goPending.value = true
        poll?.cancel()
        _state.value = _state.value.copy(download = DownloadState.Planning, notice = null,
            admittedAtMillis = SystemClock.elapsedRealtime(), observations = null, statusReadFailed = false)
        downloads.go result@{ error ->
            goPending.value = false
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
            try { openOthers(target) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { }
            while (isActive) {
                if (admitted || tick % 5 == 0) mapWorkerStatus(target, version)
                if (tick % 5 == 0) {
                    try { refreshLibrary(target, version) }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { /* A library read failure must not stop live status/Pause updates. */ }
                    readSettings(target, version)
                    readCloud(target, version)
                    readAllSites(target, version)
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
