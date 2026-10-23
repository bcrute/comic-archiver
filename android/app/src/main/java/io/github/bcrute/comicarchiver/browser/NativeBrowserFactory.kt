package io.github.bcrute.comicarchiver.browser

import android.content.Context
import android.webkit.ServiceWorkerWebSettings
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.chaquo.python.PyObject
import io.github.bcrute.comicarchiver.signin.SiteBrowserProfile
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import java.util.concurrent.atomic.AtomicLong

/** One application-owned authority for Tapas discovery and its separate Fast metadata pool. */
class NativeBrowserFactory(
    private val context: Context,
    private val profile: () -> SiteBrowserProfile,
    private val permitted: () -> Boolean,
    private val boundView: (Context, SiteBrowserProfile) -> WebView = { owner, selected ->
        WebView(owner).also(selected::bind)
    },
) {
    private val opened = linkedSetOf<NativeReadingBrowser>()
    private var workerSettings: ServiceWorkerWebSettings? = null
    private var previousWorkerBlock = true
    private var previousWorkerCache = WebSettings.LOAD_DEFAULT
    private var workerLeases = 0
    private val generation = AtomicLong()

    fun available(): Boolean = try {
        onBrowserMain {
            IsolatedScriptChannel.available() && WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)
        }
    } catch (_: Exception) { false }

    /** This is the selected profile's live canonical-origin authority, never a copied jar. */
    fun cookieStore(): WebViewCookieStore = onBrowserMain {
        if (!permitted()) throw ScriptRequestException(ScriptFailure.CLOSED)
        profile().readingCookies()
    }

    fun open(policy: PyObject): NativeReadingBrowser {
      val expected = generation.get()
      return onBrowserMain(discard = { it.close() }) {
        if (generation.get() != expected) throw ScriptRequestException(ScriptFailure.CANCELLED)
        if (!permitted() || !available() || opened.size >= 2) throw ScriptRequestException(ScriptFailure.NOT_READY)
        val selected = profile()
        check(selected.key == "tapas") { "Choose the Tapas browser profile." }
        val surface = ReaderSurface.create(context) { owner -> boundView(owner, selected) }
        var lease = false
        try {
            val view = surface.view
            view.settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                allowFileAccess = false
                allowContentAccess = false
                mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                javaScriptCanOpenWindowsAutomatically = false
                setSupportMultipleWindows(false)
                mediaPlaybackRequiresUserGesture = true
                useWideViewPort = true
                loadWithOverviewMode = false
                blockNetworkImage = true
                cacheMode = WebSettings.LOAD_NO_CACHE
                userAgentString = desktopAgent(userAgentString)
            }
            view.setInitialScale(100)
            acquireWorkerBlock(view)
            lease = true
            selected.readingCookies().rememberBrowserUserAgent(view.settings.userAgentString)
            val browser = NativeReadingBrowser(surface, policy) { released ->
                opened.remove(released)
                releaseWorkerBlock()
            }
            opened.add(browser)
            browser
        } catch (_: Exception) {
            try { surface.close() } finally { if (lease) releaseWorkerBlock() }
            throw ScriptRequestException(ScriptFailure.NOT_READY)
        }
      }
    }

    /** Stop waiting scripts promptly; the Python controller owns durable stop admission. */
    fun interrupt() {
        generation.incrementAndGet()
        onBrowserMain {
            opened.toList().forEach { try { it.interrupt() } catch (_: Exception) { } }
            if (opened.isNotEmpty()) throw ScriptRequestException(ScriptFailure.TIMEOUT)
        }
    }

    /** Called after worker drain, before profile/workspace change or sign-out. */
    fun closeAll(): Boolean {
      generation.incrementAndGet()
      return onBrowserMain {
        opened.toList().forEach { try { it.close() } catch (_: Exception) { } }
        opened.isEmpty()
      }
    }

    private fun acquireWorkerBlock(view: WebView) {
        if (workerLeases == 0) {
            val settings = WebViewCompat.getProfile(view).serviceWorkerController.serviceWorkerWebSettings
            previousWorkerBlock = settings.blockNetworkLoads
            previousWorkerCache = settings.cacheMode
            try {
                settings.blockNetworkLoads = true
                settings.cacheMode = WebSettings.LOAD_NO_CACHE
            } catch (_: Exception) {
                try { settings.cacheMode = previousWorkerCache } catch (_: Exception) { }
                try { settings.blockNetworkLoads = previousWorkerBlock } catch (_: Exception) { }
                throw ScriptRequestException(ScriptFailure.NOT_READY)
            }
            workerSettings = settings
        }
        workerLeases++
    }

    private fun releaseWorkerBlock() {
        if (workerLeases == 0) return
        workerLeases--
        if (workerLeases == 0) {
            val old = workerSettings
            workerSettings = null
            try { old?.cacheMode = previousWorkerCache } catch (_: Exception) { }
            try { old?.blockNetworkLoads = previousWorkerBlock } catch (_: Exception) { }
        }
    }

    companion object {
        /** A desktop-site request identity retains this WebView's actual Chrome version. */
        internal fun desktopAgent(original: String): String {
            if (original.length !in 1..2048 || original.any { it.code !in 32..126 } ||
                !Regex("\\bChrome/[0-9.]+").containsMatchIn(original)) throw ScriptRequestException(ScriptFailure.NOT_READY)
            return original.replace(Regex("\\([^)]*Android[^)]*\\)"), "(X11; Linux x86_64)")
                .replace("; wv", "").replace(Regex("\\bVersion/[0-9.]+\\s*"), "")
                .replace(Regex("\\bMobile\\s*"), "").trim()
        }
    }
}
