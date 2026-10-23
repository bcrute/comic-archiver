package io.github.bcrute.comicarchiver

import android.app.Application
import android.content.Context
import android.webkit.WebView
import io.github.bcrute.comicarchiver.runtime.ChaquopyBridge
import io.github.bcrute.comicarchiver.runtime.NetworkStatusWriter
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.PythonRuntime
import io.github.bcrute.comicarchiver.runtime.SignInProbe
import io.github.bcrute.comicarchiver.runtime.SiteWorkspace
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import io.github.bcrute.comicarchiver.signin.SiteBrowserProfiles
import io.github.bcrute.comicarchiver.signin.SiteBrowserProfile
import io.github.bcrute.comicarchiver.controller.DefaultAppController
import io.github.bcrute.comicarchiver.controller.SiteChoice
import io.github.bcrute.comicarchiver.export.SafExportRuntime
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONObject

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

    private val processScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    internal val openedLibrary by lazy { io.github.bcrute.comicarchiver.library.OpenedLibrary(this, bridge, processScope) }
    private val sitePreferences by lazy { getSharedPreferences("comicarchiver_sites", MODE_PRIVATE) }
    private val profiles by lazy { SiteBrowserProfiles(signInSites) }
    private val probes = mutableMapOf<String, SignInProbe>()
    private val exportRuntimes = mutableMapOf<String, SafExportRuntime>()
    @Volatile private var selectedKey: String? = null

    val initialSiteKey: String by lazy {
        val saved = sitePreferences.getString("selected", "tapas") ?: "tapas"
        if (saved in signInSites) saved else "tapas"
    }
    val workspace: SiteWorkspace get() = workspaceFor(selectedKey ?: initialSiteKey)

    fun workspaceFor(key: String): SiteWorkspace = SiteWorkspace.from(filesDir.resolve("workspace"),
        key, signInSites[key] ?: throw IllegalArgumentException("Choose a supported site."))

    fun browserProfile(key: String): SiteBrowserProfile = profiles.open(key)

    protected open fun signInAvailable(key: String): Boolean = key == "tapas" || profiles.supportsIsolatedSites()

    protected open fun cookieStoreForSite(key: String): WebViewCookieStore = profiles.open(key).cookies

    /** Profile binding is the first use of every newly created surface. */
    open fun createSignInSurface(context: Context, key: String): WebView {
        val profile = profiles.open(key)
        val surface = WebView(context)
        try { profile.bind(surface); return surface }
        catch (error: Exception) { surface.destroy(); throw error }
    }

    fun siteChoices(): List<SiteChoice> = signInSites.map { (key, address) ->
        val enabled = signInAvailable(key)
        SiteChoice(key, address.name.ifBlank { if (key == "tapas") "Tapas" else key }, true,
            if (enabled) null else "Saved files remain available. Update Android System WebView to sign in to this site.",
            signInAvailable = enabled)
    }

    /** The controller calls this after the core and selected profile have both changed. */
    fun selectWorkspace(key: String) {
        selectedKey = key
        sitePreferences.edit().putString("selected", key).apply()
        startNetworkWriter(workspaceFor(key))
    }

    // Activity recreation reattaches a surface, not a new worker/admission/session.
    // A new OS process creates these afresh and its controller always starts Held.
    val signInEngine: WebViewSignInEngine by lazy {
        lateinit var engine: WebViewSignInEngine
        engine = WebViewSignInEngine(
            cookieStore,
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
                val answer = JSONObject(bridge.dispatch("signin_attempt", JSONObject()
                    .put("session_root", workspaceFor(key).session.absolutePath).put("site_key", key)
                    .put("attempt", token).put("timeout_ms", bound).put("active", active).toString()))
                check(answer.optBoolean("ok")) { "The sign-in attempt could not be prepared." }
            },
            onVerified = { key, token ->
                val answer = JSONObject(bridge.dispatch("signin_commit", JSONObject()
                    .put("session_root", workspaceFor(key).session.absolutePath).put("site_key", key)
                    .put("attempt", token).toString()))
                answer.optBoolean("ok") && answer.optJSONObject("result")?.optBoolean("signed_in") == true
            },
        )
        engine
    }
    val controller: DefaultAppController by lazy {
        DefaultAppController(this, signInEngine, processScope)
    }
    val exports: SafExportRuntime get() = exportsFor(selectedKey ?: initialSiteKey)

    fun exportsFor(key: String): SafExportRuntime = exportRuntimes.getOrPut(key) {
        SafExportRuntime(this, bridge, workspaceFor(key).root, processScope, siteKey = key)
    }

    /** The engine's site table, fed from the core's own sites module (no second list);
     * the beta minimum stands in only if that feed answers nothing usable. */
    open val signInSites: Map<String, SiteAddress> by lazy {
        PythonRuntime.siteAddresses().ifEmpty { WebViewSignInEngine.BETA_SITES }
    }

    /** The onSettle runner: the core's verified who-am-i, wired into the engine by the UI. */
    private fun probeFor(key: String): SignInProbe = probes.getOrPut(key) {
        SignInProbe(bridge, workspaceFor(key).session.absolutePath, siteKey = key)
    }

    override fun onCreate() {
        super.onCreate()
        cookieStore = WebViewCookieStore(WebViewSignInEngine.BETA_SITES.getValue("tapas").cookieHosts)
        startRuntime()
    }

    /** The host bootstrap seam: screen/lifecycle tests supply a synthetic bridge. */
    protected open fun startRuntime() {
        PythonRuntime.ensureStarted(this, cookieStore)
        // The core holds every download until its status file speaks; start writing it now.
        startNetworkWriter(workspace)
    }

    /** Tests replace native bootstrap and this authority switch together. */
    protected open fun selectRuntimeCookieStore(store: WebViewCookieStore, siteKey: String) {
        PythonRuntime.selectCookieStore(store, siteKey)
    }

    protected open fun deactivateRuntimeCookieStore() {
        PythonRuntime.deactivateCookieStore()
    }

    protected open fun startNetworkWriter(target: SiteWorkspace) {
        NetworkStatusWriter.start(this, target.network)
    }

    override fun onTerminate() {
        processScope.cancel()
        super.onTerminate()
    }
}

/** The app-wide singletons, reached from services that cannot hold Activity references. */
val Context.app: App
    get() = applicationContext as App
