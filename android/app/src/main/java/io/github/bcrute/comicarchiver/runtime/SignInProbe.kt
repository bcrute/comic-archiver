package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * The engine's onSettle body (the native seam the UI line's MainActivity fills in): the
 * embedded core's verified who-am-i through the bridge decides — cookie presence never
 * does. A verified answer is also the only thing that writes the sign-in record the
 * worker gates on; a probe that fails or cannot run tells the engine in its public words.
 * An unwired probe leaves the session to end Failed(signin_incomplete) at its own bound:
 * SignedIn is reachable only through here.
 */
class SignInProbe(
    private val bridge: PythonBridge,
    private val sessionRoot: String,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Call as the engine's onSettle; runs off the main thread and answers the engine. */
    fun settled(engine: WebViewSignInEngine) {
        scope.launch {
            val answer = JSONObject(bridge.dispatch(
                "signin_probe", JSONObject().put("session_root", sessionRoot).toString()))
            val result = answer.optJSONObject("result")
            if (answer.optBoolean("ok") && result?.optBoolean("signed_in") == true) {
                engine.verified()
            } else {
                engine.verificationFailed(
                    if (result?.optString("reason") == "not_signed_in") "not_signed_in"
                    else "signin_unavailable")
            }
        }
    }
}
