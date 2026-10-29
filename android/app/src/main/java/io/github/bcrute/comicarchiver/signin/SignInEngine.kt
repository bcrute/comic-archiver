package io.github.bcrute.comicarchiver.signin

import android.webkit.WebView
import io.github.bcrute.comicarchiver.controller.SignInState
import kotlinx.coroutines.flow.StateFlow

/**
 * Frozen sign-in/session contract.
 *
 * The concrete implementation is owned by the WebView/session line (its own leaf component
 * may be richer internally; this is the surface AppController and the native runtime
 * consume). It holds no credentials of its own: cookies live only in the platform's
 * app-private WebView cookie store, which stays authoritative for domain, path, expiry,
 * SameSite, HttpOnly and deletion semantics. Sign-out clears the loaded site's session from
 * that store so no other-origin state remains. No object is exposed to page JavaScript.
 */
interface SignInEngine {
    /** Sign-in surface the UI renders; SignedOut when no site is loaded or after signOut(). */
    val state: StateFlow<SignInState>

    /** Point the engine at one site key (e.g. "tapas"); clears previously loaded in-memory state. */
    fun load(siteKey: String)

    /** Live cookie provider for the loaded site; the source is site-scoped (see CookieHeaderSource). */
    fun cookieSource(): CookieHeaderSource

    /** Whether the platform store currently holds the loaded site's session cookies. */
    fun isSignedIn(): Boolean

    /** Public HTTPS origin only, without path, query, user info or credentials. */
    fun currentOrigin(): String? = null

    /** Drive the given UI-owned, composable-hosted WebView through the site's real sign-in page. */
    fun startSignIn(webView: WebView)

    /** Verify an existing saved session once per process, without admitting downloads. */
    fun restoreSignIn(webView: WebView) {}

    fun cancelSignIn()

    /** A worker's verified authentication refusal requires a fresh sign-in journey. */
    fun requireSignIn() { cancelSignIn() }

    fun signOut()

    /** Completion belongs to the selected profile's clear callback. */
    fun signOut(onCleared: (Boolean) -> Unit) {
        signOut()
        onCleared(state.value is SignInState.SignedOut)
    }
}
