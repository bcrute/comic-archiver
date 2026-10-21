package io.github.bcrute.comicarchiver.signin

/**
 * The leaf sign-in state, mirroring the desktop sign-in window's states and codes
 * (comic_archive.window_signin) so the app and the downloader speak of one flow.
 *
 * @param status one of [IDLE], [RUNNING], [CONNECTED], [ERROR], [CANCELLED].
 * @param code set with [ERROR] and [CANCELLED]: one of [CODES]; null while no failure is reported.
 * @param settled true while [status] is [RUNNING] once the site's own cookies have appeared
 * in the store and verification (the downloader's existing who-am-i check through the
 * injected transport) may run.
 */
data class SignInStatus(
    val status: String,
    val code: String? = null,
    val settled: Boolean = false,
) {
    companion object {
        const val IDLE = "idle"
        const val RUNNING = "running"
        const val CONNECTED = "connected"
        const val ERROR = "error"
        const val CANCELLED = "cancelled"

        /** The failure codes the desktop sign-in window also reports; no others are accepted. */
        val CODES = setOf("network_held", "account_mismatch", "signin_failed", "signin_incomplete")
    }
}
