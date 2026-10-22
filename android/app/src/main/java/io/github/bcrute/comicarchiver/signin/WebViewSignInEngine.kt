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

/** One site the engine can load: its own pages, sign-in path, exact cookie hosts and the
 * extra landing hosts its sign-in journey may travel (its mobile mirror). The runtime feeds
 * this from the Python sites module through the bridge at construction; BETA_SITES is only
 * the beta minimum until that wiring exists. */
data class SiteAddress(
    val home: String,
    val signInPath: String,
    val cookieHosts: Set<String>,
    val signInHosts: Set<String> = emptySet(),
)

/** The sign-in journey's landing-host rule, kept pure so its boundary is testable off-device:
 * a finished page settles only on the site's own hosts, over HTTPS, by exact match. Landing
 * hosts are navigation hosts only — never cookie targets; the cookie seams keep answering
 * for [SiteAddress.cookieHosts] alone. */
object SignInLanding {
    /** Whether a finished page's scheme/host belongs to the site's own sign-in journey. */
    fun admits(host: String?, https: Boolean, site: SiteAddress): Boolean {
        if (!https || host == null) return false
        val name = host.lowercase()
        return name in site.cookieHosts || name in site.signInHosts
    }
}

/**
 * The frozen [SignInEngine] contract implemented over the WebView cookie
 * store and a UI-owned WebView.
 *
 * Cookie presence is never treated as an authenticated sign-in: [Settling] only says the
 * store holds the site's cookies. [SignedIn] is produced exclusively by [verified], and
 * only while a sign-in session this engine started is still open inside its bound — so a
 * real verified probe must have answered, in time, for the state to exist at all, and an
 * answer tagged with any earlier session authenticates nothing. The runtime's wiring
 * (see the engine handoff): on [onSettle] it keeps running the embedded core's
 * signed_in_probe through the live transport, one question in flight at a time, and
 * reports back through [verified] or [verificationFailed] — a "not yet signed in"
 * answer never ends the session. An unwired probe leaves the session to end
 * Failed(signin_incomplete) at the bound; it can never become SignedIn. The engine never
 * reads account details itself.
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
 * appear (usually while the person is still typing: the site's anonymous cookies already
 * settle the store); the runtime then keeps asking the core's verification question
 * within the session's bound until a verified answer or the bound itself ends it.
 * @param timeoutMs the sign-in wait's bound, as the desktop sign-in window's 900 seconds.
 * @param sites the sites this app can load, by site key.
 */
class WebViewSignInEngine(
    private val store: WebViewCookieStore,
    private val onSettle: () -> Unit = {},
    private val timeoutMs: Long = SIGNIN_TIMEOUT_MS,
    private val sites: Map<String, SiteAddress> = BETA_SITES,
    private val onRestore: () -> Unit = {},
) : SignInEngine, SignInVerification {

    private val main = Handler(Looper.getMainLooper())
    private val _state = MutableStateFlow<SignInState>(SignInState.SignedOut)
    override val state: StateFlow<SignInState> = _state

    private var siteKey: String? = null
    private var address: SiteAddress? = null
    private var settleAnnounced = false
    private var sessionOpen = false
    private var sessionId = 0L
    private var startedAtMillis = 0L
    private var boundMs = timeoutMs
    private var restoreAttempted = false
    private val timeout = Runnable {
        finish(if (_state.value is SignInState.Restoring) SignInState.SignedOut
               else SignInState.Failed("signin_incomplete"))
    }

    /** Point the engine at one site key; the previously loaded in-memory state goes. */
    override fun load(siteKey: String) {
        val next = sites[siteKey] ?: throw IllegalArgumentException("This app does not offer that site.")
        synchronized(this) {
            this.siteKey = siteKey
            address = next
            settleAnnounced = false
            restoreAttempted = false
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
        store.rememberBrowserUserAgent(webView.settings.userAgentString)
        CookieManager.getInstance().setAcceptCookie(true)
        webView.webViewClient = SignInWebViewClient(this)
        synchronized(this) {
            settleAnnounced = false
            sessionOpen = true
            sessionId += 1
            startedAtMillis = SystemClock.elapsedRealtime()
            boundMs = timeoutMs
        }
        val page = site.home + site.signInPath
        _state.value = SignInState.Page(page)
        webView.loadUrl(page)
        main.removeCallbacks(timeout)
        main.postDelayed(timeout, timeoutMs)
    }

    override fun restoreSignIn(webView: WebView) {
        if (address == null || restoreAttempted || _state.value !is SignInState.SignedOut) return
        restoreAttempted = true
        store.rememberBrowserUserAgent(webView.settings.userAgentString)
        synchronized(this) {
            sessionOpen = true
            sessionId += 1
            startedAtMillis = SystemClock.elapsedRealtime()
            boundMs = minOf(timeoutMs, 15_000L)
        }
        _state.value = SignInState.Restoring
        main.removeCallbacks(timeout)
        main.postDelayed(timeout, boundMs)
        onRestore()
    }

    override fun restoreNotVerified(session: Long) {
        if (_state.value is SignInState.Restoring && sessionInProgress(session))
            finish(SignInState.SignedOut)
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

    /** The sign-in session this engine is running now, or 0 while none is open. The probe
     *  compares it so an answer from an earlier session can never settle a later one. */
    override fun currentSession(): Long =
        synchronized(this) { if (sessionOpen) sessionId else 0L }

    /** The engine's live state for the settle-time probe's loop. */
    override fun signInState(): SignInState = state.value

    /** Only the embedded core's verified who-am-i success may produce SignedIn, and only
     * while this engine's own sign-in session [session] is open inside its bound — a
     * stale answer from an earlier session authenticates nothing. */
    override fun verified(session: Long) {
        if (!sessionInProgress(session)) return
        finish(SignInState.SignedIn(synchronized(this) { siteKey } ?: return))
    }

    /** The core's verification failed for [session]; its fixed code is the honest report. */
    override fun verificationFailed(code: String, session: Long) {
        if (!sessionInProgress(session)) return
        finish(SignInState.Failed(code))
    }

    /** Called by [SignInWebViewClient] on the main thread for every page that finished. */
    fun onPageFinished(url: String) {
        val site = address ?: return
        if (!synchronized(this) { sessionOpen }) return  // A closed sign-in accepts no settle.
        val current = _state.value
        if (current !is SignInState.Page && current !is SignInState.Settling) return
        if (!isSiteUrl(url, site)) return
        // The settle question is asked about the site's canonical home, a cookie host: the
        // store answers only for its exact cookie hosts, and the journey's landing hosts
        // are deliberately not cookie hosts.
        val settled = try {
            store.hasCookies(site.home)
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
     * Whether the engine's sign-in session [session] is still the open one and inside its
     * bound, checked atomically with the session flags. A session past its bound closes
     * itself here — an abandoned WebView or a late probe answer must never extend it —
     * reporting the desktop window's incomplete code.
     */
    private fun sessionInProgress(session: Long): Boolean {
        val (open, current, started) =
            synchronized(this) { Triple(sessionOpen, sessionId, startedAtMillis) }
        if (!open || current != session) return false
        if (SystemClock.elapsedRealtime() - started >= boundMs) {
            finish(if (_state.value is SignInState.Restoring) SignInState.SignedOut
                   else SignInState.Failed("signin_incomplete"))
            return false
        }
        return true
    }

    private fun finish(next: SignInState) {
        synchronized(this) { sessionOpen = false }
        main.removeCallbacks(timeout)
        _state.value = next
    }

    /** A finished page on the site's own sign-in journey only (its hosts, HTTPS, exact
     *  match — see [SignInLanding]); nothing else settles a sign-in. */
    private fun isSiteUrl(url: String, site: SiteAddress): Boolean {
        val parsed = try {
            Uri.parse(url)
        } catch (_: Exception) {
            return false
        } ?: return false
        return SignInLanding.admits(
            parsed.host,
            parsed.scheme.equals("https", ignoreCase = true),
            site,
        )
    }

    companion object {
        /** The desktop sign-in window's 900-second limit, kept the same here. */
        const val SIGNIN_TIMEOUT_MS = 15 * 60 * 1000L

        /** The beta minimum; the integrated runtime feeds [sites] from the Python sites module. */
        val BETA_SITES: Map<String, SiteAddress> = mapOf(
            "tapas" to SiteAddress(
                "https://m.tapas.io", "/account/signin",
                setOf("m.tapas.io"), setOf("tapas.io"),
            ),
        )
    }
}
