package io.github.bcrute.comicarchiver

import android.app.Application
import android.content.Context
import android.webkit.WebView
import io.github.bcrute.comicarchiver.runtime.SupportLog
import io.github.bcrute.comicarchiver.runtime.ChaquopyBridge
import io.github.bcrute.comicarchiver.runtime.FolderGrants
import io.github.bcrute.comicarchiver.runtime.NetworkStatusWriter
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.PythonRuntime
import io.github.bcrute.comicarchiver.runtime.SignInProbe
import io.github.bcrute.comicarchiver.runtime.SiteWorkspace
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryLocations
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import io.github.bcrute.comicarchiver.signin.SiteBrowserProfiles
import io.github.bcrute.comicarchiver.signin.SiteBrowserProfile
import io.github.bcrute.comicarchiver.controller.DefaultAppController
import io.github.bcrute.comicarchiver.controller.SiteChoice
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.browser.NativeBrowserFactory
import io.github.bcrute.comicarchiver.browser.WebViewEgress
import io.github.bcrute.comicarchiver.export.SafExportRuntime
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONObject
import io.github.bcrute.comicarchiver.words.say

/**
 * Starts the embedded Python runtime once per process (native line; the bootstrap form is
 * verified by the first fleet build) and owns the process-wide singletons the runtime
 * wiring shares: the selected WebView cookie store (the authority for sign-in and the
 * worker alike), the bridge, the sign-in probe the engine's onSettle runs, and the
 * net.json writer whose facts the core's network hold reads. Only one provider/workspace
 * is active at a time; saved libraries and browser profiles stay separate.
 */
open class App : Application() {
    lateinit var cookieStore: WebViewCookieStore
        private set

    open val bridge: PythonBridge by lazy { ChaquopyBridge() }
    internal open val updateBackend: io.github.bcrute.comicarchiver.ui.UpdateBackend =
        io.github.bcrute.comicarchiver.ui.PythonUpdateBackend

    private val processScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    internal val openedLibrary by lazy { io.github.bcrute.comicarchiver.library.OpenedLibrary(this, bridge, processScope) }
    private val sitePreferences by lazy { getSharedPreferences("comicarchiver_sites", MODE_PRIVATE) }
    private val profiles by lazy { SiteBrowserProfiles(signInSites) }
    private val probes = mutableMapOf<String, SignInProbe>()
    private val exportRuntimes = mutableMapOf<String, SafExportRuntime>()
    internal val libraryLocations by lazy { WorkingLibraryLocations(filesDir) }
    internal val workingLibraries by lazy { io.github.bcrute.comicarchiver.runtime.WorkingLibraries(this, processScope) }
    internal open fun createWorkingLibraryDocuments(): io.github.bcrute.comicarchiver.runtime.WorkingLibraryDocuments =
        io.github.bcrute.comicarchiver.runtime.ResolverWorkingLibraryDocuments(this)
    internal open fun createWorkingLibraryPreparation(): io.github.bcrute.comicarchiver.runtime.WorkingLibraryPreparation =
        io.github.bcrute.comicarchiver.runtime.WorkingLibraryPreparation(signInSites.keys)
    private val initialLibraryId by lazy { runCatching { libraryLocations.activeId() }.getOrDefault(WorkingLibraryLocations.DEFAULT) }
    @Volatile private var selectedLibraryId: String? = null
    val currentLibraryId: String get() = selectedLibraryId ?: initialLibraryId
    val readingBrowser by lazy {
        NativeBrowserFactory(this, { browserProfile("tapas") },
            // The first site reads while it is saving, whichever site is being shown; while
            // it is the one shown, its sign-in must still stand.
            { controller.isSaving("tapas") && ((selectedKey ?: initialSiteKey) != "tapas" ||
                (signInEngine.state.value as? SignInState.SignedIn)?.site == "tapas") },
            browserReady = ::browserConnectionReady,
            boundView = { owner, selected -> createReadingSurface(owner, selected.key) })
    }
    @Volatile private var selectedKey: String? = null

    val initialSiteKey: String by lazy {
        val saved = sitePreferences.getString("selected", "tapas") ?: "tapas"
        if (saved in signInSites) saved else "tapas"
    }
    val workspace: SiteWorkspace get() = workspaceFor(selectedKey ?: initialSiteKey)

    fun workspaceFor(key: String, libraryId: String = currentLibraryId): SiteWorkspace {
        val address = signInSites[key] ?: throw IllegalArgumentException("Choose a supported site.")
        return try { SiteWorkspace.from(libraryLocations.root(libraryId), key, address, libraryId) }
        catch (_: Exception) {
            // A display-only placeholder. No writer, probe or core preparation may use it.
            SiteWorkspace.from(filesDir.canonicalFile.resolve("unavailable-working-library"), key, address, libraryId, false)
        }
    }

    fun browserProfile(key: String): SiteBrowserProfile = profiles.open(key)

    protected open fun signInAvailable(key: String): Boolean = key == "tapas" || profiles.supportsIsolatedSites()

    protected open fun browserConnectionReady(): Boolean = WebViewEgress.ready

    protected open fun cookieStoreForSite(key: String): WebViewCookieStore = profiles.open(key).cookies

    /** Profile binding is the first use of every newly created surface. */
    open fun createSignInSurface(context: Context, key: String): WebView {
        val profile = profiles.open(key)
        val surface = WebView(context)
        try { profile.bind(surface); return surface }
        catch (error: Exception) { surface.destroy(); throw error }
    }

    /** Same profile binding, separate worker-owned window/client. Tests replace only responses. */
    open fun createReadingSurface(context: Context, key: String): WebView = createSignInSurface(context, key)

    fun siteChoices(): List<SiteChoice> = signInSites.map { (key, address) ->
        val enabled = signInAvailable(key)
        SiteChoice(key, address.name.ifBlank { if (key == "tapas") "Tapas" else key }, true,
            if (enabled) null else say("app.saved_files_remain_available_update_android"),
            signInAvailable = enabled)
    }

    /** The controller calls this after the core and selected profile have both changed. */
    fun selectWorkspace(key: String, libraryId: String = currentLibraryId) {
        val target = workspaceFor(key, libraryId)
        check(target.available) { "The working library is unavailable." }
        if (libraryId != currentLibraryId) forgetNetworkWriters()
        startNetworkWriter(target)
        selectedKey = key
        selectedLibraryId = libraryId
        sitePreferences.edit().putString("selected", key).apply()
    }

    internal fun restoreWorkspaceSelection(previous: SiteWorkspace) {
        if (previous.libraryId != currentLibraryId) forgetNetworkWriters()
        if (previous.available) startNetworkWriter(previous)
        selectedKey = previous.key
        selectedLibraryId = previous.libraryId
        sitePreferences.edit().putString("selected", previous.key).apply()
    }

    // Activity recreation reattaches a surface, not a new worker/admission/session.
    // A new OS process creates these afresh and its controller always starts Held.
    val signInEngine: WebViewSignInEngine by lazy {
        lateinit var engine: WebViewSignInEngine
        engine = WebViewSignInEngine(
            cookieStore,
            browserReady = ::browserConnectionReady,
            // Only a phone with no network at all is told so at once; one whose network may or may not
            // reach the internet is given the page's own time limit.
            online = {
                val manager = getSystemService(android.net.ConnectivityManager::class.java)
                manager == null || manager.activeNetwork != null
            },
            onSettle = { engine.loadedSiteKey()?.let { probeFor(it).settled(engine) } },
            sites = signInSites,
            onRestore = { engine.loadedSiteKey()?.let { probeFor(it).settled(engine) } },
            storeFor = { cookieStoreForSite(it) },
            onStoreSelected = { key, store ->
                selectRuntimeCookieStore(store, key)
                cookieStore = store
            },
            onStoreUnavailable = { deactivateRuntimeCookieStore() },
            onAttempt = { key, token, bound, active ->
                val target = workspaceFor(key)
                check(target.available) { "The working library is unavailable." }
                val answer = JSONObject(bridge.dispatch("signin_attempt", JSONObject()
                    .put("session_root", target.session.absolutePath).put("site_key", key)
                    .put("attempt", token).put("timeout_ms", bound).put("active", active).toString()))
                check(answer.optBoolean("ok")) { "The sign-in attempt could not be prepared." }
            },
            onVerified = { key, token ->
                val target = workspaceFor(key)
                val answer = if (!target.available) JSONObject().put("ok", false) else JSONObject(bridge.dispatch("signin_commit", JSONObject()
                    .put("session_root", target.session.absolutePath).put("site_key", key)
                    .put("attempt", token).toString()))
                answer.optBoolean("ok") && answer.optJSONObject("result")?.optBoolean("signed_in") == true
            },
            savedSignIn = { key ->
                val target = workspaceFor(key)
                target.available && target.session.resolve("android.json").isFile &&
                    target.session.resolve("site").isFile
            },
        )
        engine
    }
    val controller: DefaultAppController by lazy {
        DefaultAppController(this, signInEngine, processScope)
    }
    val exports: SafExportRuntime get() = exportsFor(selectedKey ?: initialSiteKey)

    fun exportsFor(key: String, libraryId: String = currentLibraryId): SafExportRuntime {
        val target = workspaceFor(key, libraryId)
        return exportRuntimes.getOrPut(target.identity + if (target.available) "" else "/unavailable") {
            SafExportRuntime(this, bridge, target.root, processScope, siteKey = key, libraryId = libraryId,
                canStart = { val state = controller.state.value
                    target.available && state.workspaceReady && !state.siteBusy && !state.libraryBusy && state.libraryId == libraryId && state.site == key &&
                        workspaceFor(key, libraryId).available })
        }
    }

    /** The engine's site table, fed from the core's own sites module (no second list);
     * the beta minimum stands in only if that feed answers nothing usable. */
    open val signInSites: Map<String, SiteAddress> by lazy {
        PythonRuntime.siteAddresses().ifEmpty { WebViewSignInEngine.BETA_SITES }
    }

    /** The onSettle runner: the core's verified who-am-i, wired into the engine by the UI. */
    private fun probeFor(key: String): SignInProbe {
        val target = workspaceFor(key)
        check(target.available) { "The working library is unavailable." }
        return probes.getOrPut(target.identity) { SignInProbe(bridge, target.session.absolutePath, siteKey = key) }
    }

    override fun onCreate() {
        super.onCreate()
        cookieStore = WebViewCookieStore(WebViewSignInEngine.BETA_SITES.getValue("tapas").cookieHosts)
        releaseUnrecordedFolderGrants()
        startRuntime()
    }

    /** Before any activity exists, so no picker result can be mid-way to its record. */
    protected open fun releaseUnrecordedFolderGrants() {
        try {
            val exports = SafExportRuntime.recordedTrees(this)
            val opened = io.github.bcrute.comicarchiver.library.OpenedLibrary.recordedTree(this)
            val working = (libraryLocations.libraries().mapNotNull { it.source } +
                listOfNotNull(libraryLocations.pending()?.source)).map { it.key }.toSet()
            FolderGrants.releaseUnrecorded(contentResolver) { uri ->
                val text = uri.toString()
                text in exports || text == opened || try {
                    io.github.bcrute.comicarchiver.runtime.WorkingLibraryCapture.source(uri).key in working
                } catch (_: Exception) { false }
            }
        } catch (_: Exception) {
            // Unsure what is recorded (for example an unreadable library list): keep them all.
        }
    }

    /** The host bootstrap seam: screen/lifecycle tests supply a synthetic bridge. */
    protected open fun startRuntime() {
        PythonRuntime.ensureStarted(this, cookieStore, readingBrowser)
        // The app's own log, for Save log for support; the app runs the same without it.
        try {
            bridge.dispatch("support_start", JSONObject()
                .put("log_root", SupportLog.folder(this).absolutePath).toString())
        } catch (_: Exception) {}
        // The core holds every download until its status file speaks; start writing it now.
        workspace.takeIf { it.available }?.let(::startNetworkWriter)
    }

    /** Tests replace native bootstrap and this authority switch together. */
    protected open fun selectRuntimeCookieStore(store: WebViewCookieStore, siteKey: String) {
        PythonRuntime.selectCookieStore(store, siteKey)
    }

    protected open fun deactivateRuntimeCookieStore() {
        PythonRuntime.deactivateCookieStore()
    }

    /** Each opened site gets a connection report of its own, kept fresh while the app runs. */
    protected open fun startNetworkWriter(target: SiteWorkspace) {
        NetworkStatusWriter.start(this, target.network)
    }

    /** A site opened for its counts gets its connection report too, ready for when it is started. */
    internal fun keepConnectionReport(target: SiteWorkspace) = startNetworkWriter(target)

    protected open fun forgetNetworkWriters() {
        NetworkStatusWriter.forgetAll()
    }

    override fun onTerminate() {
        processScope.cancel()
        super.onTerminate()
    }
}

/** The app-wide singletons, reached from services that cannot hold Activity references. */
val Context.app: App
    get() = applicationContext as App
