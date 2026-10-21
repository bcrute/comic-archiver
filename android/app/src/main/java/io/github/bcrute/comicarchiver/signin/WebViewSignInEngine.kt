package io.github.bcrute.comicarchiver.signin

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import android.webkit.WebView
import io.github.bcrute.comicarchiver.controller.SignInState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * The frozen [SignInEngine] contract (baseline 2a829ea) implemented over the WebView cookie
 * store and a UI-owned WebView.
 *
 * Cookie presence is never treated as an authenticated sign-in: [Settling] only says the
 * store holds the site's cookies. [SignedIn] is produced exclusively by [verified], which
 * the runtime calls after the embedded core's own who-am-i check succeeded through the
 * injected transport; [verificationFailed] carries its failure code back. The engine never
 * reads account details itself.
 *
 * The site address table is the beta minimum (tapas); the integrated point should feed it
 * from the Python sites module through the bridge rather than growing a second table here.
 * JavaScript never gains a callable surface: no addJavascriptInterface, no script
 * evaluation. Nothing is logged — no cookie value, URL or account detail.
 *
 * Engine methods are called from the app's main thread; WebView callbacks arrive there too.
 * [signOut] clears the app's whole WebView cookie store (the store keeps every site's
 * cookies together, so OAuth and other-site state never survives); the downloader must
 * already be stopped — the app owns that ordering.
 *
 * @param store the app's live cookie store, shared with the downloader's Transport.
 * @param onSettle called at most once per started sign-in, when the site's cookies first
 * appear; the runtime then runs the core verification.
 * @param timeoutMs the sign-in wait's bound, as the desktop sign-in window's 900 seconds.
 */
class WebViewSignInEngine(
    private val store: WebViewCookieStore,
    private val onSettle: () -> Unit = {},
    private val timeoutMs: Long = SIGNIN_TIMEOUT_MS,
) : SignInEngine {

    private data class SiteAddress(val home: String, val signInPath: String, val cookieHosts: Set<String>)

    private val main = Handler(Looper.getMainLooper())
    private val _state = MutableStateFlow<SignInState>(SignInState.SignedOut)
    override val state: StateFlow<SignInState> = _state

    private var siteKey: String? = null
    private var address: SiteAddress? = null
    private var settleAnnounced = false
    private val timeout = Runnable { finish(SignInState.Failed("signin_incomplete")) }

    /** Point the engine at one site key; the previously loaded in-memory state goes. */
    override fun load(siteKey: String) {
        val next = SITES[siteKey] ?: throw IllegalArgumentException("Unsupported site.")
        synchronized(this) {
            this.siteKey = siteKey
            address = next
            settleAnnounced = false
        }
        finish(SignInState.SignedOut)
    }

    /** Live cookie provider for the loaded site; the source itself is site-scoped. */
    override fun cookieSource(): CookieHeaderSource = store

    /** Whether the platform store holds the loaded site's cookies. Cookie presence only:
     * an authentication claim needs the core's verified who-am-i, see [verified]. */
    override fun isSignedIn(): Boolean {
        val site = address ?: return false
        return store.hasCookies(site.home)
    }

    /** Drive the UI-owned WebView through the site's real sign-in page. */
    override fun startSignIn(webView: WebView) {
        val site = address ?: throw IllegalStateException("Load a site before signing in.")
        // The sign-in page's own needs only: its scripts and storage on, every surface the
        // page does not need off. A JavaScript interface is never added, here or elsewhere.
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.saveFormData = false
        webView.settings.allowFileAccess = false
        webView.settings.allowContentAccess = false
        CookieManager.getInstance().setAcceptCookie(true)
        webView.webViewClient = SignInWebViewClient(this)
        synchronized(this) { settleAnnounced = false }
        val page = site.home + site.signInPath
        _state.value = SignInState.Page(page)
        webView.loadUrl(page)
        main.removeCallbacks(timeout)
        main.postDelayed(timeout, timeoutMs)
    }

    /** The person went back, or the app is closing; an unfinished sign-in is stopped. */
    override fun cancelSignIn() {
        finish(SignInState.Failed("signin_incomplete"))
    }

    /** Clear the loaded site's session from the store; SignedOut once the store is cleared. */
    override fun signOut() {
        store.clearAllAsync { _ -> finish(SignInState.SignedOut) }
    }

    /** Only the embedded core's verified who-am-i success may produce SignedIn. */
    fun verified() {
        val key = synchronized(this) { siteKey } ?: return
        finish(SignInState.SignedIn(key))
    }

    /** The core's verification failed; its fixed code is the honest report. */
    fun verificationFailed(code: String) {
        finish(SignInState.Failed(code))
    }

    /** Called by [SignInWebViewClient] on the main thread for every page that finished. */
    fun onPageFinished(url: String) {
        val site = address ?: return
        val current = _state.value
        if (current !is SignInState.Page && current !is SignInState.Settling) return
        if (!isSiteUrl(url, site)) return
        // A store that cannot answer simply has not settled; the wait's bound decides the rest.
        val settled = try {
            store.hasCookies(url)
        } catch (_: Exception) {
            false
        }
        if (settled) {
            _state.value = SignInState.Settling
            val announce = synchronized(this) {
                val first = !settleAnnounced
                settleAnnounced = true
                first
            }
            if (announce) {
                store.flush()
                onSettle()
            }
        }
    }

    private fun finish(next: SignInState) {
        main.removeCallbacks(timeout)
        _state.value = next
    }

    /** A finished page on the site's own cookie hosts only; nothing else settles a sign-in. */
    private fun isSiteUrl(url: String, site: SiteAddress): Boolean {
        val parsed = try {
            Uri.parse(url)
        } catch (_: Exception) {
            return false
        } ?: return false
        if (!parsed.scheme.equals("https", ignoreCase = true)) return false
        val host = parsed.host?.lowercase() ?: return false
        return host in site.cookieHosts
    }

    companion object {
        /** The desktop sign-in window's 900-second limit, kept the same here. */
        const val SIGNIN_TIMEOUT_MS = 15 * 60 * 1000L

        /** The beta minimum; the integrated point feeds this from the Python sites module. */
        private val SITES = mapOf(
            "tapas" to SiteAddress("https://tapas.io", "/account/signin", setOf("tapas.io")),
        )
    }
}
