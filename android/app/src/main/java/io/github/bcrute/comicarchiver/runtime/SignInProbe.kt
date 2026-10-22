package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.signin.SignInVerification
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import kotlin.coroutines.CoroutineContext

/**
 * The engine's onSettle body (the native seam the UI line's MainActivity fills in): the
 * embedded core's verified who-am-i through the bridge decides — cookie presence never
 * does. The bridge writes the sign-in record the worker gates on inside its own dispatch,
 * once its own verified who-am-i has passed; that write can land even when the native
 * callback below is then rejected as stale. What the session tag guarantees is narrower
 * and exact: a callback from an earlier session can never transition the native engine.
 *
 * The first settle usually happens on the site's anonymous cookies while the person is
 * still typing, so the desktop sign-in window's shape is kept: the question is asked
 * again every [retryMs] — exactly one network probe in flight at a time, also across a
 * replacement loop, because cancellation cannot interrupt a blocking dispatch (the
 * earlier loop is cancelled and joined, and the dispatch gate serialises the question
 * itself) — and a "not yet signed in" answer never ends anything; the session's own
 * bound (the engine's 900-second limit, Back, or a cancelled session) does. Only a
 * bridge that cannot even ask the question reports terminal signin_unavailable.
 *
 * The engine's answers are delivered on [engineContext] (the main dispatcher in the
 * app), so the session check and the state transition cannot race the main-thread
 * cancel/start/timeout paths. The loop only ever asks the store for headers — it clears
 * nothing, so stopping it (bound, Back, activity gone) leaves the owner's cookies
 * exactly as they are. [settled] is called from the main thread only, so replacement
 * ordering is serial by construction.
 */
class SignInProbe(
    private val bridge: PythonBridge,
    private val sessionRoot: String,
    private val retryMs: Long = 3_000L,
    private val engineContext: CoroutineContext = Dispatchers.Main,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val dispatchGate = Mutex()

    @Volatile private var loop: Job? = null

    /** Call as the engine's onSettle; runs off the main thread and answers the engine. */
    fun settled(engine: SignInVerification) {
        val session = engine.currentSession()
        val prior = loop
        prior?.cancel()
        loop = scope.launch {
            // The earlier loop's in-flight question still completes after its
            // cancellation; wait for it before this loop's first dispatch.
            prior?.join()
            ask(engine, session)
        }
    }

    private suspend fun ask(engine: SignInVerification, session: Long) {
        while (true) {
            if (engine.currentSession() != session) return
            val state = engine.signInState()
            if (state !is SignInState.Page && state !is SignInState.Settling) return
            val answer = dispatchGate.withLock {
                // The bridge answers failures as ok=false envelopes; a throw here means
                // the question could not even be asked.
                try {
                    JSONObject(bridge.dispatch(
                        "signin_probe", JSONObject().put("session_root", sessionRoot).toString()))
                } catch (_: Exception) {
                    null
                }
            }
            val result = answer?.optJSONObject("result")
            when {
                answer != null && answer.optBoolean("ok") &&
                    result?.optBoolean("signed_in") == true -> {
                    withContext(engineContext) { engine.verified(session) }
                    return
                }
                answer != null && answer.optBoolean("ok") -> {
                    // "Not yet signed in" (the person is still typing) or the site's own
                    // "unavailable": the page stays usable; the bound decides the rest.
                }
                else -> {
                    // The bridge could not even ask: honest terminal failure, retryable
                    // by starting the sign-in again.
                    withContext(engineContext) {
                        engine.verificationFailed("signin_unavailable", session)
                    }
                    return
                }
            }
            delay(retryMs)
        }
    }
}
