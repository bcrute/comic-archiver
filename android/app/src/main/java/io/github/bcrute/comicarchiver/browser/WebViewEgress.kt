package io.github.bcrute.comicarchiver.browser

import android.os.Handler
import android.os.Looper
import android.webkit.HttpAuthHandler
import androidx.webkit.ProxyConfig
import androidx.webkit.ProxyController
import androidx.webkit.WebViewFeature
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.util.concurrent.Executor

/** Owns the process-wide WebView override and the authenticated loopback endpoint. */
internal object WebViewEgress {
    @Volatile var ready: Boolean = false
        private set

    private var endpoint: PyObject? = null

    /** Python bootstrap calls this on Main. Page loads wait for the override callback. */
    fun install() {
        if (Looper.myLooper() != Looper.getMainLooper() || endpoint != null) return
        try {
            if (!WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)) return
            val connector = Python.getInstance().getModule("comic_archive.browser_egress").callAttr("BrowserEgress")
            endpoint = connector
            val proxy = ProxyConfig.Builder()
                .addProxyRule(connector.callAttr("proxy_url").toString())
                .removeImplicitRules()
                .build()
            val main = Handler(Looper.getMainLooper())
            ProxyController.getInstance().setProxyOverride(proxy, Executor { main.post(it) }) {
                ready = endpoint === connector
            }
        } catch (_: Exception) {
            val connector = endpoint
            endpoint = null
            ready = false
            try { connector?.callAttr("close") } catch (_: Exception) {}
        }
    }

    fun authenticate(handler: HttpAuthHandler, host: String, realm: String) {
        val password = if (ready) try {
            endpoint?.callAttr("password_for", host, realm)?.toString()
        } catch (_: Exception) { null } else null
        if (password.isNullOrEmpty()) handler.cancel() else handler.proceed("browser", password)
    }
}
