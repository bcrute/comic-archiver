package io.github.bcrute.comicarchiver.integration

import android.os.SystemClock
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import io.github.bcrute.comicarchiver.browser.IsolatedScriptChannel
import io.github.bcrute.comicarchiver.browser.ReaderSurface
import org.json.JSONObject
import org.json.JSONTokener
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Disposable-emulator-only execution of the real private window and isolated JS world. */
@RunWith(AndroidJUnit4::class)
class BrowserSurfaceTest {
    @Test fun readingWindowHasAViewportAndPageScriptsCannotReachItsCommandWorld() {
        FixtureSafety.requireEmulator()
        val app = ApplicationProvider.getApplicationContext<BetaTestApplication>()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val loaded = CountDownLatch(1)
        var surface: ReaderSurface? = null
        var channel: IsolatedScriptChannel? = null
        try {
            instrumentation.runOnMainSync {
                assertTrue("Browser execution world capability is required", IsolatedScriptChannel.available())
                val created = ReaderSurface.create(app) { context ->
                    WebView(context).also { app.browserProfile("tapas").bind(it) }
                }
                surface = created
                val browser = created.view
                browser.settings.javaScriptEnabled = true
                browser.settings.allowFileAccess = false
                browser.settings.allowContentAccess = false
                val scripts = IsolatedScriptChannel(browser, setOf("https://tapas.io"))
                channel = scripts
                browser.webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse {
                        // No fixture request may escape to the real provider, including subresources.
                        val admitted = request.method == "GET" && request.url.toString() == URL
                        return WebResourceResponse("text/html", "UTF-8", if (admitted) 200 else 403,
                            if (admitted) "OK" else "Refused", mapOf("Cache-Control" to "no-store"),
                            ByteArrayInputStream(if (admitted) HTML.toByteArray() else byteArrayOf()))
                    }
                    override fun onPageStarted(view: WebView, url: String, icon: android.graphics.Bitmap?) {
                        scripts.navigationStarted()
                    }
                    override fun onPageFinished(view: WebView, url: String) { if (url == URL) loaded.countDown() }
                }
                scripts.prepareNavigation()
                browser.loadUrl(URL)
            }
            assertTrue("Fixture page did not load", loaded.await(20, TimeUnit.SECONDS))
            val scripts = requireNotNull(channel)
            fun json(source: String): JSONObject {
                val encoded = scripts.execute(source, 5_000)
                // executeJavaScript returns a JSON-encoded JS value; our script returns JSON text.
                val text = JSONTokener(encoded).nextValue()
                assertTrue("Expected a JSON text result", text is String)
                return JSONObject(text as String)
            }
            val deadline = SystemClock.elapsedRealtime() + 10_000
            var seen = JSONObject()
            do {
                seen = json("""JSON.stringify({width:innerWidth,height:innerHeight,
                    siteGlobal:typeof globalThis.pageOwned, bridge:typeof globalThis.comicArchiveReader,
                    poisonedGlobal:typeof globalThis.comicArchive,
                    siteSeesBridge:document.getElementById('page-probe').textContent})""")
                if (seen.optInt("width") == ReaderSurface.WIDTH && seen.optInt("height") == ReaderSurface.HEIGHT) break
                SystemClock.sleep(50)
            } while (SystemClock.elapsedRealtime() < deadline)
            assertEquals(ReaderSurface.WIDTH, seen.getInt("width"))
            assertEquals(ReaderSurface.HEIGHT, seen.getInt("height"))
            assertEquals("undefined", seen.getString("siteGlobal"))
            assertEquals("undefined", seen.getString("poisonedGlobal"))
            assertEquals("object", seen.getString("bridge"))
            assertEquals("undefined", seen.getString("siteSeesBridge"))
            val retained = json("""(() => { globalThis.readerValue = 42;
                return JSON.stringify({value:globalThis.readerValue}); })()""")
            assertEquals(42, retained.getInt("value"))
            // Arming a Next click must retain the current document for its
            // visibility/position/guard reads until navigation actually begins.
            instrumentation.runOnMainSync { scripts.prepareNavigation() }
            assertEquals(42, json("JSON.stringify({value:globalThis.readerValue})").getInt("value"))
        } finally {
            instrumentation.runOnMainSync {
                try { channel?.close() } finally { surface?.close() }
            }
        }
    }

    companion object {
        private const val URL = "https://tapas.io/reader-world-fixture"
        private val HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
            <div id="page-probe"></div><script>
                globalThis.pageOwned = 'page-value';
                globalThis.comicArchive = {next: 'poisoned by site'};
                document.getElementById('page-probe').textContent = typeof globalThis.comicArchiveReader;
            </script>""".trimIndent()
    }
}
