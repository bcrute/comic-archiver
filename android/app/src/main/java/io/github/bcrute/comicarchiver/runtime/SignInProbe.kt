package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicReference

/**
 * The engine's onSettle body (the native seam the UI line's MainActivity fills in): the
 * embedded core's verified who-am-i through the bridge decides — cookie presence never
 * does. A verified answer is also the only thing that writes the sign-in record the
 * worker gates on; a probe that fails or cannot run tells the engine in its public words.
 * An unwired probe leaves the session to end Failed(signin_incomplete) at its own bound:
 * SignedIn is reachable only through here.
 *
 * The first settle usually happens on the site's anonymous cookies while the person is
 * still typing, so the desktop sign-in window's shape is kept: the question is asked
 * again every [retryMs] — one request in flight at a time — and a "not yet signed in"
 * answer never ends anything; the session's own bound (the engine's 900-second limit,
 * Back, or a cancelled session) does. Only a bridge that cannot even ask the question
 * reports terminal signin_unavailable. The loop carries the session it was started for,
 * and the engine rejects answers tagged with any other session, so a stale loop can
 * never authenticate a newer sign-in; a new settle cancels any loop still running. The
 * loop only ever asks the store for headers — it clears nothing, so stopping it (Bound,
 * Back, activity gone) leaves the owner's cookies exactly as they are.
 */
class SignInProbe(
    private val bridge: PythonBridge,
    private val sessionRoot: String,
    private val retryMs: Long = 3_000L,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val loop = AtomicReference<Job?>(null)

    /** Call as the engine's onSettle; runs off the main thread and answers the engine. */
    fun settled(engine: WebViewSignInEngine) {
        val session = engine.currentSession()
        loop.getAndSet(scope.launch {
            while (true) {
                if (engine.currentSession() != session) return@launch
                val state = engine.state.value
                if (state !is SignInState.Page && state !is SignInState.Settling) return@launch
                val answer = try {
                    JSONObject(bridge.dispatch(
                        "signin_probe", JSONObject().put("session_root", sessionRoot).toString()))
                } catch (_: Exception) {
                    engine.verificationFailed("signin_unavailable", session)
                    return@launch
                }
                val result = answer.optJSONObject("result")
                when {
                    answer.optBoolean("ok") && result?.optBoolean("signed_in") == true -> {
                        // The only path that writes the sign-in record (the bridge writes it
                        // inside this dispatch, after its own verified who-am-i); the engine
                        // rejects it unless this exact session is still the open one.
                        engine.verified(session)
                        return@launch
                    }
                    answer.optBoolean("ok") && result?.optString("reason") == "not_signed_in" -> {
                        // The person is still signing in; the page stays usable. Ask again.
                    }
                    answer.optBoolean("ok") -> {
                        // The site or transport answered "unavailable": the window keeps
                        // waiting, as the desktop's does, until the bound decides.
                    }
                    else -> {
                        // The bridge could not even ask: honest terminal failure, retryable
                        // by starting the sign-in again.
                        engine.verificationFailed("signin_unavailable", session)
                        return@launch
                    }
                }
                delay(retryMs)
            }
        })?.cancel()
    }
}
