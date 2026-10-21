package io.github.bcrute.comicarchiver.signin

import android.net.Uri
import android.webkit.CookieManager
import android.webkit.WebView

/**
 * The sign-in leaf the app wires to its WebView screen.
 *
 * The app keeps the WebView and the screens; this controller carries the state machine the
 * desktop sign-in window has: [start] opens a bounded wait, the WebView reports finished
 * site pages through [SignInWebViewClient], and once the store holds the site's cookies the
 * [onSettle] callback fires so the app can have the embedded downloader verify the session
 * with its existing who-am-i check (through the injected transport in
 * comic_archive.android_transport). The outcome returns here as [markConnected] or
 * [fail]; the controller never reads account details itself.
 *
 * All methods may be called from any thread; WebView callbacks arrive on the main thread.
 * Nothing here logs a cookie value, URL or account detail.
 *
 * @param cookieStore the app's live cookie store, shared with the downloader's transport.
 * @param cookieHosts the site's exact cookie hosts; only pages on these hosts settle a sign-in.
 * @param onSettle called at most once per started sign-in, when the site's cookies first appear.
 * @param timeoutSeconds the sign-in wait's bound, as the desktop window's 900 seconds.
 */
class SignInSessionController(
    private val cookieStore: WebViewCookieStore,
    private val cookieHosts: Set<String>,
    private val onSettle: () -> Unit = {},
    private val timeoutSeconds: Long = SIGNIN_SECONDS,
) {
    private val lock = Any()
    private var state = SignInStatus(SignInStatus.IDLE)
    private var startedAtNanos = 0L
    private var settleAnnounced = false

    /** The seam the embedded downloader's transport asks for live cookie headers. */
    val cookieProvider: CookieHeaderProvider get() = cookieStore

    fun status(): SignInStatus = synchronized(lock) { waiting(); state.copy() }

    /** Ask for a sign-in. An open, unfinished request is kept as it is, as on the desktop. */
    fun start(): SignInStatus = synchronized(lock) {
        if (!waiting()) {
            startedAtNanos = System.nanoTime()
            settleAnnounced = false
            state = SignInStatus(SignInStatus.RUNNING)
        }
        state.copy()
    }

    /** The person went back, or the app is closing; an unfinished sign-in is stopped. */
    fun cancel() {
        synchronized(lock) {
            if (waiting()) state = SignInStatus(SignInStatus.CANCELLED, "signin_incomplete")
        }
    }

    /** The verification outcome, as a fixed code; unknown codes are a programming error. */
    fun fail(code: String) {
        require(code in SignInStatus.CODES) { "Unsupported sign-in result." }
        synchronized(lock) {
            if (waiting()) state = SignInStatus(SignInStatus.ERROR, code)
        }
    }

    /** Verification succeeded: the site's session works through the transport. */
    fun markConnected() {
        synchronized(lock) {
            if (waiting()) state = SignInStatus(SignInStatus.CONNECTED)
        }
    }

    /**
     * Clear the app's whole WebView cookie store; the downloader must already be stopped,
     * the app owns that ordering (stop downloader, clear store, then the session record).
     * [onCleared] reports on the main thread whether every cookie was removed.
     */
    fun signOut(onCleared: (Boolean) -> Unit) {
        synchronized(lock) { state = SignInStatus(SignInStatus.IDLE) }
        cookieStore.clearAllAsync(onCleared)
    }

    /** Called by [SignInWebViewClient] on the main thread for every page that finished. */
    fun onPageFinished(url: String) {
        var announce = false
        // A store that cannot answer simply has not settled; the wait's bound decides the rest.
        val hasCookies = try {
            cookieStore.hasCookies(url)
        } catch (_: Exception) {
            false
        }
        synchronized(lock) {
            if (waiting() && !state.settled && isSiteUrl(url)) {
                if (hasCookies) {
                    state = state.copy(settled = true)
                    if (!settleAnnounced) {
                        settleAnnounced = true
                        announce = true
                    }
                }
            }
        }
        if (announce) {
            cookieStore.flush()
            onSettle()
        }
    }

    /** A finished page on the site's own cookie hosts only; nothing else can settle a sign-in. */
    private fun isSiteUrl(url: String): Boolean {
        val address = try {
            Uri.parse(url)
        } catch (_: Exception) {
            return false
        } ?: return false
        if (!address.scheme.equals("https", ignoreCase = true)) return false
        val host = address.host?.lowercase() ?: return false
        return host in cookieHosts
    }

    companion object {
        /** The desktop sign-in window's 900-second limit, kept the same here. */
        const val SIGNIN_SECONDS = 900L

        /**
         * The sign-in page's own needs only: its scripts and its storage on, every surface
         * the page does not need off. A JavaScript interface is never added, here or
         * anywhere else: the site's pages cannot reach the app or the downloader.
         */
        fun configure(view: WebView) {
            view.settings.javaScriptEnabled = true
            view.settings.domStorageEnabled = true
            view.settings.saveFormData = false
            view.settings.allowFileAccess = false
            view.settings.allowContentAccess = false
            CookieManager.getInstance().setAcceptCookie(true)
        }
    }
}
