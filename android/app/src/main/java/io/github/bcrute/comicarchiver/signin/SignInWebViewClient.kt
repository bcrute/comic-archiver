package io.github.bcrute.comicarchiver.signin

import android.view.View
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

    /** The page is ready to draw without a white flash: show the surface the sign-in hid. */
    override fun onPageCommitVisible(view: WebView, url: String) {
        view.visibility = View.VISIBLE
    }

    override fun onPageFinished(view: WebView, url: String) {
        view.visibility = View.VISIBLE
        if (engine.currentSession() == session) engine.onPageFinished(url)
    }

    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
        if (request.isForMainFrame) fail()
    }

    /** An HTTP error page is still the site talking to the person: a rejected password
     * (401/403), a bot challenge that clears itself in script (403/503) or a stray 404 all
     * render their own page and leave the sign-in usable. Ending the attempt here showed
     * a generic failure instead. The session's bound and Back still end it; transport
     * and TLS failures above still fail it. */
    override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) = Unit

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
