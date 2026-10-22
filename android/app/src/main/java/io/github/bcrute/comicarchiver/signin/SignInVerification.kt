package io.github.bcrute.comicarchiver.signin

import io.github.bcrute.comicarchiver.controller.SignInState

/**
 * The sign-in engine surface the settle-time probe talks to: the session it was started
 * for, the live state it polls, and the two answers it reports — [WebViewSignInEngine]
 * implements it; the JVM tests fake it to cover the probe's retry, cancel and stale
 * answer behaviour off-device. Answers carry the session they belong to, so the engine
 * can reject a stale callback without the probe knowing why.
 */
interface SignInVerification {
    /** The sign-in session running now, or 0 while none is open. */
    fun currentSession(): Long

    /** The engine's live state, as the frozen [SignInEngine] exposes it. */
    fun signInState(): SignInState

    /** A verified who-am-i for [session]; the engine decides whether it still counts. */
    fun verified(session: Long)

    /** The honest failure code for [session]; the engine decides whether it still counts. */
    fun verificationFailed(code: String, session: Long)

    /** A one-shot restore found no verified session; interactive sign-in stays available. */
    fun restoreNotVerified(session: Long) { verificationFailed("login_required", session) }
}
