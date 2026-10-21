package io.github.bcrute.comicarchiver.signin

import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Reports the sign-in pages' progress to [SignInSessionController].
 *
 * The site's own pages and the sign-in providers it sends the person to load in the WebView;
 * only other schemes (mailto:, tel:, intent:) are refused. This client installs no
 * JavaScript interface and evaluates no script: nothing in the page can call the app, the
 * downloader or Python. Nothing is logged.
 */
class SignInWebViewClient(private val controller: SignInSessionController) : WebViewClient() {

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        // Load web pages in the WebView; leave every other scheme to the system, not to us.
        val scheme = request.url.scheme ?: return true
        return !scheme.equals("http", ignoreCase = true) && !scheme.equals("https", ignoreCase = true)
    }

    override fun onPageFinished(view: WebView, url: String) {
        controller.onPageFinished(url)
    }
}
