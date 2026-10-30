package io.github.bcrute.comicarchiver.integration

import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import io.github.bcrute.comicarchiver.browser.ReaderSurface
import io.github.bcrute.comicarchiver.signin.SignInPageStyle
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import org.json.JSONObject
import org.json.JSONTokener
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Disposable-emulator-only: the real WebView hides the site's cookie notice on its sign-in
 * page, leaving the login link uncovered, and leaves other sites' pages as served. */
@RunWith(AndroidJUnit4::class)
class SignInPageStyleTest {
    @Test fun theCookieNoticeIsHiddenOnTheSitesOwnSignInPageOnly() {
        FixtureSafety.requireEmulator()
        val app = ApplicationProvider.getApplicationContext<BetaTestApplication>()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val site = WebViewSignInEngine.BETA_SITES.getValue("tapas")
        var surface: ReaderSurface? = null
        var view: WebView? = null
        val pages = mapOf(SIGNIN to CountDownLatch(1), ELSEWHERE to CountDownLatch(1))
        fun read(): JSONObject {
            val answered = CountDownLatch(1)
            var value = ""
            instrumentation.runOnMainSync {
                requireNotNull(view).evaluateJavascript(PROBE) { value = it; answered.countDown() }
            }
            assertTrue("The page did not answer", answered.await(10, TimeUnit.SECONDS))
            return JSONObject(JSONTokener(value).nextValue() as String)
        }
        try {
            instrumentation.runOnMainSync {
                // A real on-screen window, so the probe can ask what covers the link.
                val created = ReaderSurface.create(app) { context -> WebView(context) }
                surface = created
                val browser = created.view
                view = browser
                browser.settings.javaScriptEnabled = true
                browser.webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse {
                        // No fixture request may reach the real site.
                        val known = request.method == "GET" && request.url.toString() in pages
                        return WebResourceResponse("text/html", "UTF-8", if (known) 200 else 403,
                            if (known) "OK" else "Refused", mapOf("Cache-Control" to "no-store"),
                            ByteArrayInputStream(if (known) PAGE.toByteArray() else byteArrayOf()))
                    }
                    override fun onPageFinished(view: WebView, url: String) { pages[url]?.countDown() }
                }
                assertNotNull("The hide rule needs the script-world capability", SignInPageStyle.install(browser, site))
                browser.loadUrl(SIGNIN)
            }
            assertTrue("Sign-in fixture did not load", pages.getValue(SIGNIN).await(20, TimeUnit.SECONDS))
            val signIn = read()
            assertEquals("none", signIn.getString("notice"))
            assertEquals("email", signIn.getString("atLink"))

            instrumentation.runOnMainSync { requireNotNull(view).loadUrl(ELSEWHERE) }
            assertTrue("Other-site fixture did not load", pages.getValue(ELSEWHERE).await(20, TimeUnit.SECONDS))
            assertEquals("block", read().getString("notice"))
        } finally {
            instrumentation.runOnMainSync { surface?.close() }
        }
    }

    companion object {
        private const val SIGNIN = "https://m.tapas.io/account/signin"
        private const val ELSEWHERE = "https://elsewhere.example/account/signin"
        // A fixed notice over the bottom of the page, where the site's email link sits.
        private val PAGE = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width"></head>
            <body style="margin:0">
            <a id="email" href="#" style="position:absolute;bottom:40px;left:20px;display:block;height:40px">Log in with email</a>
            <div id="gdpr-cookie-message" style="position:fixed;bottom:0;left:0;right:0;height:200px;z-index:999;
                display:block;background:#eee">Tapas uses cookies. <button>Accept</button></div>
            </body></html>
        """.trimIndent()
        private val PROBE = """
            (() => {
                const link = document.getElementById('email').getBoundingClientRect();
                const top = document.elementFromPoint(link.left + 5, link.top + 5);
                return JSON.stringify({
                    notice: getComputedStyle(document.getElementById('gdpr-cookie-message')).display,
                    atLink: top ? top.id : '',
                });
            })()
        """.trimIndent()
    }
}
