package io.github.bcrute.comicarchiver.browser

import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.os.SystemClock
import android.view.InputDevice
import android.view.MotionEvent
import android.webkit.ConsoleMessage
import android.webkit.GeolocationPermissions
import android.webkit.JsPromptResult
import android.webkit.JsResult
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import com.chaquo.python.PyObject
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs

/** Native ABI used only by the owning Python reader; no method is exposed to site JavaScript. */
class NativeReadingBrowser internal constructor(
    private val surface: ReaderSurface,
    policy: PyObject,
    private val released: (NativeReadingBrowser) -> Unit,
) {
    private val view get() = surface.view
    private val scripts = IsolatedScriptChannel(view, setOf(ORIGIN))
    @Volatile private var rules: PyObject? = policy
    @Volatile private var closed = false
    @Volatile private var gone = false
    private var url = "about:blank"
    private var ended = 0L
    private val failures = AtomicLong()
    @Volatile private var failure: Int? = null
    private val agent = view.settings.userAgentString
    private var downTime = 0L

    init {
        view.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(browser: WebView, request: WebResourceRequest): WebResourceResponse? {
                if (request.url.scheme in setOf("data", "blob")) return null
                return if (allow(request)) null else WebResourceResponse("text/plain", "UTF-8", 403,
                    "Refused", mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(byteArrayOf()))
            }
            override fun shouldOverrideUrlLoading(browser: WebView, request: WebResourceRequest): Boolean =
                closed || request.method !in setOf("GET", "HEAD", "OPTIONS") || !readingUrl(request.url)

            override fun onPageStarted(browser: WebView, address: String, icon: Bitmap?) {
                scripts.navigationStarted()
                url = address
                if (!readingUrl(Uri.parse(address))) {
                    browser.stopLoading()
                    failed(-1)
                }
            }
            override fun onPageFinished(browser: WebView, address: String) { ended++ }
            override fun onReceivedError(browser: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) failed(error.errorCode)
            }
            override fun onReceivedHttpError(browser: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame) failed(response.statusCode)
            }
            override fun onReceivedSslError(browser: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel()
                failed(-2)
            }
            override fun onRenderProcessGone(browser: WebView, detail: RenderProcessGoneDetail): Boolean {
                gone = true
                failed(-3)
                closeOnMain()
                return true
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage): Boolean = true
            override fun onJsAlert(browser: WebView, url: String, message: String, result: JsResult): Boolean {
                result.cancel(); return true
            }
            override fun onJsConfirm(browser: WebView, url: String, message: String, result: JsResult): Boolean {
                result.cancel(); return true
            }
            override fun onJsPrompt(browser: WebView, url: String, message: String, defaultValue: String, result: JsPromptResult): Boolean {
                result.cancel(); return true
            }
            override fun onPermissionRequest(request: PermissionRequest) { request.deny() }
            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                callback.invoke(origin, false, false)
            }
        }
        view.setDownloadListener { _, _, _, _, _ -> /* Only the core writes archives. */ }
    }

    private fun failed(code: Int) { failure = code; failures.incrementAndGet() }

    private fun allow(request: WebResourceRequest): Boolean {
        val uri = request.url
        if (closed || request.method !in setOf("GET", "HEAD", "OPTIONS") || uri.scheme != "https" ||
            uri.userInfo != null || uri.port !in setOf(-1, 443) || uri.host.isNullOrEmpty()) return false
        val selected = rules ?: return false
        return try {
            val admitted = selected.callAttr("allowRequest", uri.toString(), request.method,
                if (request.isForMainFrame) "document" else "other").toBoolean()
            !closed && admitted
        } catch (_: Exception) { false }
    }

    fun state(): String = onBrowserMain {
        JSONObject().put("v", 1).put("url", url).put("ended", ended)
            .put("failures", failures.get()).put("failure", failure ?: JSONObject.NULL)
            .put("closed", closed).put("gone", gone).put("agent", agent).toString()
    }

    fun load(address: String) = onBrowserMain {
        checkOpen()
        if (!readingUrl(Uri.parse(address))) throw ScriptRequestException(ScriptFailure.SCRIPT)
        scripts.prepareNavigation()
        scripts.navigationStarted()
        url = address
        view.loadUrl(address)
    }

    fun prepareNavigation() = onBrowserMain { checkOpen(); scripts.prepareNavigation() }

    fun execute(source: String, timeoutMs: Long): String {
        checkOpen()
        return scripts.execute(source, timeoutMs)
    }

    fun pointer(kind: String, x: Double, y: Double) = onBrowserMain {
        checkOpen()
        if (kind !in setOf("move", "press", "release")) throw ScriptRequestException(ScriptFailure.SCRIPT)
        val position = position(x, y)
        val now = SystemClock.uptimeMillis()
        if (kind == "press") downTime = now
        val action = when (kind) { "move" -> MotionEvent.ACTION_HOVER_MOVE; "press" -> MotionEvent.ACTION_DOWN; else -> MotionEvent.ACTION_UP }
        val event = mouseEvent(if (downTime == 0L) now else downTime, now, action, position.first, position.second,
            if (kind == "press") MotionEvent.BUTTON_PRIMARY else 0)
        try {
            if (kind == "move") view.dispatchGenericMotionEvent(event) else view.dispatchTouchEvent(event)
        } finally {
            event.recycle()
            if (kind == "release") downTime = 0
        }
    }

    fun wheel(x: Double, y: Double, deltaX: Double, deltaY: Double) = onBrowserMain {
        checkOpen()
        if (!deltaX.isFinite() || !deltaY.isFinite() || abs(deltaX) > 100000 || abs(deltaY) > 100000)
            throw ScriptRequestException(ScriptFailure.SCRIPT)
        val position = position(x, y)
        val now = SystemClock.uptimeMillis()
        val event = mouseEvent(now, now, MotionEvent.ACTION_SCROLL, position.first, position.second,
            deltaX = deltaX.toFloat(), deltaY = deltaY.toFloat())
        try { view.dispatchGenericMotionEvent(event) } finally { event.recycle() }
    }

    @Suppress("DEPRECATION")
    private fun position(x: Double, y: Double): Pair<Float, Float> {
        if (!x.isFinite() || !y.isFinite()) throw ScriptRequestException(ScriptFailure.SCRIPT)
        val scale = view.scale
        val at = Pair((x * scale).toFloat(), (y * scale).toFloat())
        if (!scale.isFinite() || scale <= 0 || at.first < 0 || at.second < 0 ||
            at.first >= view.width || at.second >= view.height) throw ScriptRequestException(ScriptFailure.SCRIPT)
        return at
    }

    private fun checkOpen() { if (closed || gone) throw ScriptRequestException(ScriptFailure.CLOSED) }

    /** Destroy the reading document too, so stopped fetches cannot keep transferring. */
    fun interrupt() = onBrowserMain { closeOnMain() }

    fun close() = onBrowserMain { closeOnMain() }

    private fun closeOnMain() {
        if (closed) return
        closed = true
        rules = null
        try { scripts.close() }
        finally { try { surface.close() } finally { released(this) } }
    }

    companion object {
        internal const val ORIGIN = "https://tapas.io"
        private fun readingUrl(uri: Uri): Boolean = uri.scheme == "https" && uri.host == "tapas.io" &&
            uri.userInfo == null && uri.port in setOf(-1, 443)

        private fun mouseEvent(down: Long, now: Long, action: Int, x: Float, y: Float,
                              buttons: Int = 0, deltaX: Float = 0f, deltaY: Float = 0f): MotionEvent {
            val properties = MotionEvent.PointerProperties().apply { id = 0; toolType = MotionEvent.TOOL_TYPE_MOUSE }
            val coordinates = MotionEvent.PointerCoords().apply {
                this.x = x; this.y = y; pressure = 1f; size = 1f
                setAxisValue(MotionEvent.AXIS_HSCROLL, deltaX / 100f)
                setAxisValue(MotionEvent.AXIS_VSCROLL, -deltaY / 100f)
            }
            return MotionEvent.obtain(down, now, action, 1, arrayOf(properties), arrayOf(coordinates), 0,
                buttons, 1f, 1f, 0, 0, InputDevice.SOURCE_MOUSE, 0)
        }
    }
}
