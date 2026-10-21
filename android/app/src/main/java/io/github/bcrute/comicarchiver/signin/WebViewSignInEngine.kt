package io.github.bcrute.comicarchiver.signin

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.webkit.CookieManager
import android.webkit.WebView
import io.github.bcrute.comicarchiver.controller.SignInState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** One site the engine can load: its own pages, sign-in path and exact cookie hosts.
 * The runtime feeds this from the Python sites module through the bridge at construction;
 * BETA_SITES is only the beta minimum until that wiring exists. */
data class SiteAddress(val home: String, val signInPath: String, val cookieHosts: Set<String>)

/**
 * The frozen [SignInEngine] contract implemented over the WebView cookie
 * store and a UI-owned WebView.
 *
 * Cookie presence is never treated as an authenticated sign-in: [Settling] only says the
 * store holds the site's cookies. [SignedIn] is produced exclusively by [verified], and
 * only while a sign-in session this engine started is still open inside its bound — so a
 * real verified probe must have answered, in time, for the state to exist at all. The
 * runtime's wiring (see the engine handoff): on [onSettle] it runs the embedded core's
 * signed_in_probe through the live transport and reports back through [verified] or
 * [verificationFailed]. An unwired probe leaves the session to end Failed(signin_incomplete)
 * at the bound; it can never become SignedIn. The engine never reads account details itself.
 *
 * The site address table is caller-supplied ([sites]), so there is no second sites table
 * here once the bridge feeds it from the Python sites module; [BETA_SITES] is the beta
 * minimum. JavaScript never gains a callable surface: no addJavascriptInterface, no script
 * evaluation. Nothing is logged — no cookie value, URL or account detail.
 *
 * Engine methods are called from the app's main thread; WebView callbacks arrive there too.
 * [load] prepares the platform cookie store on the main thread, before any worker thread
 * can ask it a question. [signOut] first closes the session (no further settle or probe is
 * accepted, the wait's bound is cancelled) and only then clears the app's whole WebView
 * cookie store — the store keeps every site's cookies together, so OAuth and other-site
 * state never survive; the downloader must already be stopped, the app owns that ordering.
 *
 * @param store the app's live cookie store, shared with the downloader's Transport.
 * @param onSettle called at most once per started sign-in, when the site's cookies first
 * appear; the runtime then runs the core verification.
 * @param timeoutMs the sign-in wait's bound, as the desktop sign-in window's 900 seconds.
 * @param sites the sites this app can load, by site key.
 */
class WebViewSignInEngine(
    private val store: WebViewCookieStore,
    private val onSettle: () -> Unit = {},
    private val timeoutMs: Long = SIGNIN_TIMEOUT_MS,
    private val sites: Map<String, SiteAddress> = BETA_SITES,
) : SignInEngine {

    private val main = Handler(Looper.getMainLooper())
    private val _state = MutableStateFlow<SignInState>(SignInState.SignedOut)
    override val state: StateFlow<SignInState> = _state

    private var siteKey: String? = null
    private var address: SiteAddress? = null
    private var settleAnnounced = false
    private var sessionOpen = false
    private var startedAtMillis = 0L
    private val timeout = Runnable { finish(SignInState.Failed("signin_incomplete")) }

    /** Point the engine at one site key; the previously loaded in-memory state goes. */
    override fun load(siteKey: String) {
        val next = sites[siteKey] ?: throw IllegalArgumentException("This app does not offer that site.")
        synchronized(this) {
            this.siteKey = siteKey
            address = next
            settleAnnounced = false
        }
        // The platform store initialises on the main thread, before any worker asks it.
        store.prepare()
        finish(SignInState.SignedOut)
    }

    /** Live cookie provider for the loaded site; the source itself is site-scoped. */
    override fun cookieSource(): CookieHeaderSource = store

    /** Whether the platform store holds the loaded site's cookies. Cookie presence only:
     * an authentication claim needs the core's verified who-am-i, see [verified]. */
    override fun isSignedIn(): Boolean {
        val site = address ?: return false
        return try {
            store.hasCookies(site.home)
        } catch (_: Exception) {
            false
        }
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
        synchronized(this) {
            settleAnnounced = false
            sessionOpen = true
            startedAtMillis = SystemClock.elapsedRealtime()
        }
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

    /**
     * Sign out: the session closes first — no further page, settle or probe answer is
     * accepted, and the wait's bound is cancelled — and only then is the app's whole
     * WebView cookie store cleared. [SignedOut] follows the store's own answer.
     */
    override fun signOut() {
        synchronized(this) { sessionOpen = false }
        main.removeCallbacks(timeout)
        store.clearAllAsync { cleared ->
            finish(if (cleared) SignInState.SignedOut
                   else SignInState.Failed("signout_incomplete"))
        }
    }

    /** Only the embedded core's verified who-am-i success may produce SignedIn, and only
     * while this engine's own sign-in session is open inside its bound. */
    fun verified() {
        if (!sessionInProgress()) return
        finish(SignInState.SignedIn(synchronized(this) { siteKey } ?: return))
    }

    /** The core's verification failed; its fixed code is the honest report. */
    fun verificationFailed(code: String) {
        if (!sessionInProgress()) return
        finish(SignInState.Failed(code))
    }

    /** Called by [SignInWebViewClient] on the main thread for every page that finished. */
    fun onPageFinished(url: String) {
        val site = address ?: return
        if (!synchronized(this) { sessionOpen }) return  // A closed sign-in accepts no settle.
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

    /**
     * Whether a sign-in this engine started is still open and inside its bound, checked
     * atomically with the session flags. A session past its bound closes itself here —
     * an abandoned WebView or a late probe answer must never extend it — reporting the
     * desktop window's incomplete code.
     */
    private fun sessionInProgress(): Boolean {
        val (open, started) = synchronized(this) { sessionOpen to startedAtMillis }
        if (!open) return false
        if (SystemClock.elapsedRealtime() - started >= timeoutMs) {
            finish(SignInState.Failed("signin_incomplete"))
            return false
        }
        return true
    }

    private fun finish(next: SignInState) {
        synchronized(this) { sessionOpen = false }
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

        /** The beta minimum; the integrated runtime feeds [sites] from the Python sites module. */
        val BETA_SITES: Map<String, SiteAddress> = mapOf(
            "tapas" to SiteAddress("https://tapas.io", "/account/signin", setOf("tapas.io")),
        )
    }
}
