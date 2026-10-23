package io.github.bcrute.comicarchiver.signin

import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebResourceError
import android.webkit.WebResourceResponse
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.net.http.SslError
import android.graphics.Bitmap
import android.net.Uri
import android.view.ViewGroup

/**
 * Reports the sign-in pages' progress to [WebViewSignInEngine].
 *
 * The site's own pages and the sign-in providers it sends the person to load in the WebView;
 * only other schemes (mailto:, tel:, intent:) are refused. This client installs no
 * JavaScript interface and evaluates no script: nothing in the page can call the app, the
 * downloader or Python. Nothing is logged.
 */
class SignInWebViewClient(private val engine: WebViewSignInEngine,
                         private val session: Long = engine.currentSession()) : WebViewClient() {

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val refused = !request.url.scheme.equals("https", ignoreCase = true)
        if (refused && request.isForMainFrame) fail()
        return refused
    }

    @Suppress("DEPRECATION")
    override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
        val refused = !Uri.parse(url).scheme.equals("https", ignoreCase = true)
        if (refused) fail()
        return refused
    }

    override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
        if (engine.currentSession() != session) return
        if (!Uri.parse(url).scheme.equals("https", ignoreCase = true)) {
            view.stopLoading()
            fail()
        } else engine.pageStarted(url, session)
    }

    override fun onPageFinished(view: WebView, url: String) {
        if (engine.currentSession() == session) engine.onPageFinished(url)
    }

    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
        if (request.isForMainFrame) fail()
    }

    override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
        if (request.isForMainFrame && errorResponse.statusCode >= 400) fail()
    }

    override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
        handler.cancel()
        fail()
    }

    override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
        fail()
        (view.parent as? ViewGroup)?.removeView(view)
        try { view.destroy() } catch (_: Exception) {}
        return true
    }

    private fun fail() { engine.verificationFailed("signin_unavailable", session) }
}
