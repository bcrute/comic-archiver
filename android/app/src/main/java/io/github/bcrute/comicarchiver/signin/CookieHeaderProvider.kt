package io.github.bcrute.comicarchiver.signin

/**
 * The smallest native/HTTP seam: the app's WebView cookie store, read live and URL-scoped.
 *
 * The embedded Python downloader holds one implementation of this interface (see
 * [WebViewCookieStore]) and asks it for exactly the Cookie header a browser would send to
 * one URL, each time it makes a request or follows a redirect. The store stays the only
 * authority on cookie attributes; nothing on either side of the seam reconstructs domain,
 * path, expiry or security attributes, and no cookie value, URL or account detail is ever
 * logged. Safe to call from any thread.
 */
interface CookieHeaderProvider {

    /**
     * The Cookie request-header pairs for this exact URL, or "" when the store would send
     * none. Only the site's own HTTPS cookie hosts are answered; every other origin gets "".
     *
     * @throws CookieSourceException the store could not answer within its bound, or is not
     * ready. The exception's message never carries a URL, value or account detail.
     */
    @Throws(CookieSourceException::class)
    fun cookieHeader(url: String): String

    /**
     * Hand raw Set-Cookie response strings observed for this URL to the store's own parser,
     * unchanged. Returns true when the store took them, false when it could not. Called by
     * the downloader only for the site's own cookie hosts.
     */
    fun setSessionCookies(url: String, setCookieValues: List<String>): Boolean

    /**
     * Remove every cookie the store holds for this app, bounded in time. The WebView store
     * keeps every site's cookies together, so a sign-out or a site switch clears all of it:
     * OAuth and other-site state never survives. Must be called off the main thread; the
     * main thread uses [WebViewCookieStore.clearAllAsync] so it never waits on its own
     * callback.
     */
    @Throws(CookieSourceException::class)
    fun clearAll(): Boolean
}

/** The cookie store failed or is not ready; the message carries no URL, value or account detail. */
class CookieSourceException(message: String) : Exception(message)
