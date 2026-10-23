package io.github.bcrute.comicarchiver.browser

import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import androidx.webkit.JavaScriptExecutionException
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import androidx.webkit.WebViewOutcomeReceiver
import androidx.webkit.WebMessageCompat
import org.json.JSONObject
import java.net.URI
import java.util.UUID

/**
 * The downloader's trusted script entry point. The site receives no Java interface,
 * page-world message object or command listener. Its sole injected listener is in a
 * separate execution world, restricted to exact HTTPS origins and the main frame.
 *
 * Construct/notify navigation/close on Main before loading the reading WebView; call
 * [execute] only from its owning worker. This is not installed on the sign-in surface.
 */
internal class IsolatedScriptChannel(
    private val view: WebView,
    allowedOrigins: Set<String>,
) {
    private val main = Handler(Looper.getMainLooper())
    private val requests = ScriptRequests(maxPending = 1)
    private val origins = allowedOrigins.toSet()
    private var session = UUID.randomUUID().toString()
    private var proxy: JavaScriptReplyProxy? = null
    private var attachedDocument: String? = null
    private var closed = false
    private val world = run {
        requireMain()
        require(origins.isNotEmpty() && origins.all { exactHttpsOrigin(it) }) { "Choose exact HTTPS reading origins." }
        check(available()) { "Update Android System WebView to use browser reading." }
        WebViewCompat.getExecutionWorld(view, WORLD)
    }
    private var script: ScriptHandler? = null

    init {
        WebViewCompat.addWebMessageListener(view, OBJECT, origins, world) { source, message, origin, mainFrame, reply ->
            if (closed || source !== view || !mainFrame || origin.toString() !in origins) return@addWebMessageListener
            // Only the small, isolated bootstrap message establishes a reply proxy.
            if (message.type != WebMessageCompat.TYPE_STRING) return@addWebMessageListener
            val text = message.data ?: return@addWebMessageListener
            if (text.length > 512) return@addWebMessageListener
            val value = try { JSONObject(text) } catch (_: Exception) { return@addWebMessageListener }
            if (value.optString("session") != session || value.optString("kind") != "ready") return@addWebMessageListener
            val token = value.optString("document")
            // A duplicate bootstrap may not replace the current document's authority.
            if (attachedDocument != null && attachedDocument != token) return@addWebMessageListener
            if (requests.attach(token)) {
                attachedDocument = token
                proxy = reply
            }
        }
        try {
            installBootstrap()
        } catch (_: Exception) {
            WebViewCompat.removeWebMessageListener(view, world, OBJECT)
            requests.close()
            throw ScriptRequestException(ScriptFailure.SCRIPT)
        }
    }

    /**
     * Arm before loadUrl or a pointer action that may navigate. The next document
     * gets a new native nonce, so a queued ready message from an older page cannot
     * reattach its reply proxy while the replacement page is loading.
     */
    fun prepareNavigation() {
        requireMain()
        if (closed) throw ScriptRequestException(ScriptFailure.CLOSED)
        navigationStarted()
        try {
            script?.remove()
            script = null
            session = UUID.randomUUID().toString()
            installBootstrap()
        } catch (_: Exception) {
            close()
            throw ScriptRequestException(ScriptFailure.SCRIPT)
        }
    }

    private fun installBootstrap() {
        script = WebViewCompat.addJavaScriptOnEvent(view, bootstrap(session),
            WebViewCompat.INJECTION_EVENT_DOCUMENT_START, origins, world)
    }

    /** Returns the engine's JSON-encoded JavaScript value, without decoding/printing it. */
    fun execute(source: String, timeoutMs: Long): String {
        check(Looper.myLooper() != Looper.getMainLooper()) { "Browser reads must run on their worker." }
        require(timeoutMs in 1..45_000)
        if (source.toByteArray(Charsets.UTF_8).size > MAX_SCRIPT_BYTES) throw ScriptRequestException(ScriptFailure.TOO_LARGE)
        val ticket = requests.begin()
        val posted = main.post {
            if (!requests.current(ticket)) return@post
            val current = proxy
            if (current == null || attachedDocument != ticket.document) {
                requests.refuse(ticket, ScriptFailure.CHANGED)
                return@post
            }
            try {
                current.executeJavaScript(source, object : WebViewOutcomeReceiver<String, JavaScriptExecutionException> {
                    override fun onResult(result: String) {
                        if (result.toByteArray(Charsets.UTF_8).size > MAX_RESULT_BYTES) requests.refuse(ticket, ScriptFailure.TOO_LARGE)
                        else requests.complete(ticket, result)
                    }
                    override fun onError(error: JavaScriptExecutionException) {
                        // The engine's exception can contain source/URLs; expose fixed words only.
                        requests.refuse(ticket, ScriptFailure.SCRIPT)
                    }
                })
            } catch (_: Exception) {
                requests.refuse(ticket, ScriptFailure.SCRIPT)
            }
        }
        if (!posted) requests.refuse(ticket, ScriptFailure.CLOSED)
        return requests.await(ticket, timeoutMs)
    }

    fun navigationStarted() {
        requireMain()
        proxy = null
        attachedDocument = null
        requests.navigationStarted()
    }

    /** Releases waiters immediately; renderer cancellation belongs to the browser owner. */
    fun cancelPending() { requests.cancelPending() }

    fun close() {
        requireMain()
        if (closed) return
        closed = true
        proxy = null
        attachedDocument = null
        requests.close()
        try { script?.remove(); script = null }
        finally { WebViewCompat.removeWebMessageListener(view, world, OBJECT) }
    }

    companion object {
        private const val OBJECT = "comicArchiveReader"
        private const val WORLD = "comic-archiver-reader"
        internal const val MAX_SCRIPT_BYTES = 1024 * 1024
        internal const val MAX_RESULT_BYTES = 32 * 1024 * 1024

        fun available(): Boolean =
            WebViewFeature.isFeatureSupported(WebViewFeature.JS_INJECTION_IN_FRAME_AND_WORLD) &&
                WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)

        private fun requireMain() {
            check(Looper.myLooper() == Looper.getMainLooper()) { "The reading browser belongs to the main thread." }
        }

        internal fun exactHttpsOrigin(value: String): Boolean = try {
            val uri = URI(value)
            uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.rawAuthority == uri.host &&
                uri.rawUserInfo == null && uri.port == -1 && uri.rawPath.isNullOrEmpty() &&
                uri.rawQuery == null && uri.rawFragment == null && !value.contains('*')
        } catch (_: Exception) { false }

        private fun bootstrap(session: String): String = """
            (() => {
                if (window !== window.top) return;
                const document = String(performance.timeOrigin) + ':' + crypto.randomUUID();
                $OBJECT.postMessage(JSON.stringify({kind:'ready', session:${JSONObject.quote(session)}, document}));
            })();
        """.trimIndent()
    }
}
