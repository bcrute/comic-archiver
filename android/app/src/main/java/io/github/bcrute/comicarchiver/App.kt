package io.github.bcrute.comicarchiver

import android.app.Application
import android.content.Context
import io.github.bcrute.comicarchiver.runtime.ChaquopyBridge
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.PythonRuntime
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore

/**
 * Starts the embedded Python runtime once per process (native line; the bootstrap form is
 * verified by the first fleet build) and owns the process-wide singletons the runtime
 * wiring shares: the one WebView cookie store (the cookie authority for sign-in and the
 * worker alike) and the bridge. The beta loads exactly one site, tapas.
 */
class App : Application() {
    lateinit var cookieStore: WebViewCookieStore
        private set

    val bridge: PythonBridge by lazy { ChaquopyBridge() }

    override fun onCreate() {
        super.onCreate()
        cookieStore = WebViewCookieStore(setOf("tapas.io"))
        PythonRuntime.ensureStarted(this, cookieStore)
    }
}

/** The app-wide singletons, reached from services that cannot hold Activity references. */
val Context.app: App
    get() = applicationContext as App
