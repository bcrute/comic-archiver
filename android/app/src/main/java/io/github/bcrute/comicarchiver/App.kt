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

/**
 * Starts the embedded Python runtime once per process (native line; the bootstrap form is
 * verified by the first fleet build) and owns the process-wide singletons the runtime
 * wiring shares: the one WebView cookie store (the cookie authority for sign-in and the
 * worker alike), the bridge, the sign-in probe the engine's onSettle runs, and the
 * net.json writer whose facts the core's network hold reads. The beta loads exactly one
 * site, tapas.
 */
class App : Application() {
    lateinit var cookieStore: WebViewCookieStore
        private set

    val bridge: PythonBridge by lazy { ChaquopyBridge() }

    /** The engine's site table, fed from the core's own sites module (no second list). */
    val signInSites: Map<String, SiteAddress> by lazy { PythonRuntime.siteAddresses() }

    /** The onSettle runner: the core's verified who-am-i, wired into the engine by the UI. */
    val signInProbe: SignInProbe by lazy {
        SignInProbe(bridge, filesDir.resolve("workspace/.comic-session").absolutePath)
    }

    override fun onCreate() {
        super.onCreate()
        cookieStore = WebViewCookieStore(setOf("tapas.io"))
        PythonRuntime.ensureStarted(this, cookieStore)
        // The core holds every download until its status file speaks; start writing it now.
        NetworkStatusWriter.start(this, filesDir.resolve("workspace/net.json"))
    }
}

/** The app-wide singletons, reached from services that cannot hold Activity references. */
val Context.app: App
    get() = applicationContext as App
