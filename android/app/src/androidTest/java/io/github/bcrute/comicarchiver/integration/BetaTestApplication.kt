package io.github.bcrute.comicarchiver.integration

import android.content.Context
import android.content.pm.ApplicationInfo
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import com.chaquo.python.Python
import io.github.bcrute.comicarchiver.App
import io.github.bcrute.comicarchiver.runtime.PythonRuntime
import java.io.ByteArrayInputStream

/** Production runtime and native cookie/profile authority; only network responses are fixtures. */
class BetaTestApplication : App() {
    override fun startRuntime() {
        FixtureSafety.requireEmulator()
        check((applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0)
        val marker = filesDir.resolve("integration-code/run-token")
        if (marker.exists()) {
            check(marker.readText() == FixtureSafety.runToken) { "Fixture data belongs to a different run." }
        } else {
            check(!filesDir.resolve("workspace/Library").walkTopDown().any {
                it.isFile && (it.extension in setOf("cbz", "epub") || it.name.endsWith(".tar.zst"))
            }) { "Refusing to initialize fixtures over an existing archive library." }
            marker.parentFile!!.mkdirs()
            marker.writeText(FixtureSafety.runToken)
        }
        PythonRuntime.ensureStarted(this, cookieStore)
        val directory = marker.parentFile!!
        val testContext = createPackageContext("$packageName.test", 0)
        testContext.assets.open("beta_fixture.py").use { input ->
            directory.resolve("beta_fixture.py").outputStream().use { input.copyTo(it) }
        }
        Python.getInstance().getModule("sys").get("path")!!.callAttr("insert", 0, directory.absolutePath)
        fixture().callAttr("install")
        startNetworkWriter(workspace) // Real ConnectivityManager facts; no test network-policy bypass.
    }

    override fun createSignInSurface(context: Context, key: String): WebView {
        check(key == "tapas") { "This integration fixture qualifies only Tapas." }
        return FixtureWebView(context).also { browserProfile(key).bind(it) }
    }

    fun fixture() = Python.getInstance().getModule("beta_fixture")
}

/** Browser still executes the page and owns cookies. No JS interface or native cookie injection. */
private class FixtureWebView(context: Context) : WebView(context) {
    override fun setWebViewClient(client: WebViewClient) {
        super.setWebViewClient(FixturePages(client))
    }
}

private class FixturePages(private val delegate: WebViewClient) : WebViewClient() {
    private fun response(uri: Uri): WebResourceResponse {
        val own = uri.scheme == "https" && uri.host == "m.tapas.io"
        val login = own && uri.path == "/account/signin"
        val home = own && uri.path == "/"
        val body = when {
            login -> """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
                <style>html,body{height:100%;margin:0}body{display:flex;align-items:center;justify-content:center}
                button{font-size:24px;padding:24px}</style></head><body>
                <button onclick="document.cookie='fixture_session=READY; Path=/; Secure; SameSite=Lax';
                    location.href='https://m.tapas.io/'">Sign in to fixture</button></body></html>"""
            home -> "<html><body>Fixture sign-in submitted.</body></html>"
            else -> "<html><body>External networking is disabled in this fixture.</body></html>"
        }
        return WebResourceResponse("text/html", "UTF-8", if (login || home) 200 else 403,
            if (login || home) "OK" else "Forbidden",
            mapOf("Cache-Control" to "no-store", "Content-Security-Policy" to
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"),
            ByteArrayInputStream(body.toByteArray(Charsets.UTF_8)))
    }

    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest) = response(request.url)
    @Suppress("DEPRECATION")
    override fun shouldInterceptRequest(view: WebView, url: String) = response(Uri.parse(url))
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) =
        delegate.shouldOverrideUrlLoading(view, request)
    @Suppress("DEPRECATION")
    override fun shouldOverrideUrlLoading(view: WebView, url: String) = delegate.shouldOverrideUrlLoading(view, url)
    override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) = delegate.onPageStarted(view, url, favicon)
    override fun onPageFinished(view: WebView, url: String) = delegate.onPageFinished(view, url)
    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) =
        delegate.onReceivedError(view, request, error)
    override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, error: WebResourceResponse) =
        delegate.onReceivedHttpError(view, request, error)
    override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) =
        delegate.onReceivedSslError(view, handler, error)
    override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail) =
        delegate.onRenderProcessGone(view, detail)
}
