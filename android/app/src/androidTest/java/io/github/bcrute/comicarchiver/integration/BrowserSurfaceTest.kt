package io.github.bcrute.comicarchiver.integration

import android.os.SystemClock
import android.webkit.ServiceWorkerClient
import android.webkit.ServiceWorkerController
import android.webkit.WebSettings
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewCompat
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
        var workers: ServiceWorkerController? = null
        var previousWorkerBlock = true
        var previousWorkerCache = WebSettings.LOAD_DEFAULT
        try {
            instrumentation.runOnMainSync {
                assertTrue("Browser execution world capability is required", IsolatedScriptChannel.available())
                val created = ReaderSurface.create(app) { context ->
                    WebView(context).also { app.browserProfile("tapas").bind(it) }
                }
                surface = created
                val browser = created.view
                browser.settings.javaScriptEnabled = true
                browser.settings.domStorageEnabled = true
                browser.settings.allowFileAccess = false
                browser.settings.allowContentAccess = false
                val controller = WebViewCompat.getProfile(browser).serviceWorkerController
                workers = controller
                previousWorkerBlock = controller.serviceWorkerWebSettings.blockNetworkLoads
                previousWorkerCache = controller.serviceWorkerWebSettings.cacheMode
                controller.serviceWorkerWebSettings.blockNetworkLoads = false
                controller.serviceWorkerWebSettings.cacheMode = WebSettings.LOAD_NO_CACHE
                controller.setServiceWorkerClient(object : ServiceWorkerClient() {
                    override fun shouldInterceptRequest(request: WebResourceRequest): WebResourceResponse {
                        val allowed = request.method == "GET" && request.url.toString() == WORKER_URL
                        return WebResourceResponse("text/javascript", "UTF-8", if (allowed) 200 else 403,
                            if (allowed) "OK" else "Refused", mapOf("Cache-Control" to "no-store"),
                            ByteArrayInputStream(if (allowed) WORKER.toByteArray() else byteArrayOf()))
                    }
                })
                val scripts = IsolatedScriptChannel(browser, setOf("https://tapas.io"))
                channel = scripts
                browser.webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse {
                        // No fixture request may escape to the real provider, including subresources.
                        val worker = request.method == "GET" && request.url.toString() == WORKER_URL
                        val page = request.method == "GET" && request.url.toString() == URL
                        val admitted = worker || page
                        return WebResourceResponse(if (worker) "text/javascript" else "text/html", "UTF-8", if (admitted) 200 else 403,
                            if (admitted) "OK" else "Refused", mapOf("Cache-Control" to "no-store"),
                            ByteArrayInputStream(when { worker -> WORKER.toByteArray(); page -> HTML.toByteArray(); else -> byteArrayOf() }))
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
            // Seed a real active worker in the actual sign-in profile. The next
            // Go must unregister it and discard the controlled reading window;
            // otherwise who-am-i/discovery receive this worker's stale content.
            json("""(() => {
                globalThis.workerRegistered = null;
                navigator.serviceWorker.register('$WORKER_URL', {scope:'/'})
                  .then(() => navigator.serviceWorker.ready)
                  .then(() => {globalThis.workerRegistered = true;}, () => {globalThis.workerRegistered = false;});
                return JSON.stringify({started:true});
            })()""")
            val workerDeadline = SystemClock.elapsedRealtime() + 20_000
            do {
                seen = json("""JSON.stringify({registered:globalThis.workerRegistered,
                    controlled:!!navigator.serviceWorker.controller})""")
                if (seen.optBoolean("registered") && seen.optBoolean("controlled")) break
                SystemClock.sleep(50)
            } while (SystemClock.elapsedRealtime() < workerDeadline)
            assertTrue("Fixture worker did not activate", seen.optBoolean("registered"))
            assertTrue("Fixture document is not controlled", seen.optBoolean("controlled"))
            json("""(() => {
                globalThis.workerCached = null;
                fetch('https://tapas.io/reader-cached-fixture').then(answer => answer.text())
                  .then(text => {globalThis.workerCached = text === '$STALE';},
                    () => {globalThis.workerCached = false;});
                return JSON.stringify({started:true});
            })()""")
            val cacheDeadline = SystemClock.elapsedRealtime() + 10_000
            do {
                seen = json("JSON.stringify({cached:globalThis.workerCached})")
                if (!seen.isNull("cached")) break
                SystemClock.sleep(50)
            } while (SystemClock.elapsedRealtime() < cacheDeadline)
            assertTrue("Fixture worker did not supply cached content", seen.optBoolean("cached"))
        } finally {
            instrumentation.runOnMainSync {
                try { channel?.close() } finally {
                    try { surface?.close() } finally {
                        workers?.setServiceWorkerClient(null)
                        workers?.serviceWorkerWebSettings?.cacheMode = previousWorkerCache
                        workers?.serviceWorkerWebSettings?.blockNetworkLoads = previousWorkerBlock
                    }
                }
            }
        }
    }

    companion object {
        private const val URL = "https://tapas.io/reader-world-fixture"
        private const val WORKER_URL = "https://tapas.io/reader-worker-fixture.js"
        private const val STALE = "CACHED_FIXTURE_CONTENT_MUST_NOT_BE_USED"
        private val WORKER = """self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
            self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
            self.addEventListener('fetch', event => event.respondWith(new Response('$STALE',
                {status:200, headers:{'Content-Type':'text/html'}})));""".trimIndent()
        private val HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
            <div id="page-probe"></div><script>
                globalThis.pageOwned = 'page-value';
                globalThis.comicArchive = {next: 'poisoned by site'};
                document.getElementById('page-probe').textContent = typeof globalThis.comicArchiveReader;
            </script>""".trimIndent()
    }
}
