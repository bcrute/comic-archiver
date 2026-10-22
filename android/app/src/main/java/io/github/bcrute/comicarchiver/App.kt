package io.github.bcrute.comicarchiver

import android.app.Application
import android.content.Context
import io.github.bcrute.comicarchiver.runtime.ChaquopyBridge
import io.github.bcrute.comicarchiver.runtime.NetworkStatusWriter
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.PythonRuntime
import io.github.bcrute.comicarchiver.runtime.SignInProbe
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import io.github.bcrute.comicarchiver.controller.DefaultAppController
import io.github.bcrute.comicarchiver.export.SafExportRuntime
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel

/**
 * Starts the embedded Python runtime once per process (native line; the bootstrap form is
 * verified by the first fleet build) and owns the process-wide singletons the runtime
 * wiring shares: the one WebView cookie store (the cookie authority for sign-in and the
 * worker alike), the bridge, the sign-in probe the engine's onSettle runs, and the
 * net.json writer whose facts the core's network hold reads. The beta loads exactly one
 * site, tapas.
 */
open class App : Application() {
    lateinit var cookieStore: WebViewCookieStore
        private set

    open val bridge: PythonBridge by lazy { ChaquopyBridge() }

    private val processScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    // Activity recreation reattaches a surface, not a new worker/admission/session.
    // A new OS process creates these afresh and its controller always starts Held.
    val signInEngine: WebViewSignInEngine by lazy {
        lateinit var engine: WebViewSignInEngine
        engine = WebViewSignInEngine(
            cookieStore,
            onSettle = { signInProbe.settled(engine) },
            sites = signInSites,
            onRestore = { signInProbe.settled(engine) },
        )
        engine
    }
    val controller: DefaultAppController by lazy {
        DefaultAppController(this, signInEngine, processScope)
    }
    val exports: SafExportRuntime by lazy {
        SafExportRuntime(this, bridge, filesDir.resolve("workspace"), processScope)
    }

    /** The engine's site table, fed from the core's own sites module (no second list);
     * the beta minimum stands in only if that feed answers nothing usable. */
    open val signInSites: Map<String, SiteAddress> by lazy {
        PythonRuntime.siteAddresses().ifEmpty { WebViewSignInEngine.BETA_SITES }
    }

    /** The onSettle runner: the core's verified who-am-i, wired into the engine by the UI. */
    val signInProbe: SignInProbe by lazy {
        SignInProbe(bridge, filesDir.resolve("workspace/.comic-session").absolutePath)
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
        NetworkStatusWriter.start(this, filesDir.resolve("workspace/net.json"))
    }

    override fun onTerminate() {
        processScope.cancel()
        super.onTerminate()
    }
}

/** The app-wide singletons, reached from services that cannot hold Activity references. */
val Context.app: App
    get() = applicationContext as App
