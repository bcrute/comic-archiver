package io.github.bcrute.comicarchiver.signin

/**
 * Live per-URL Cookie header provider (docs/android-beta-contracts.md §4), consumed by the
 * embedded Python Transport through its cookie_source seam.
 *
 * Implementations (WebView/session line) must:
 *  - return the header the platform's own app-private cookie store would send for exactly
 *    this URL at call time, or null when it would send none;
 *  - never cache: every request and every redirect hop asks again;
 *  - never invent attributes the platform does not provide, and never scrape cookie
 *    databases;
 *  - never return a credential-bearing header for a URL outside the loaded site's own hosts.
 *
 * The core Transport additionally consults this source only for the site's cookie hosts, so
 * CDN and cross-origin targets receive no credentials by construction, on both sides of the
 * seam. There is deliberately no snapshot, jar or cookie-converter counterpart.
 */
interface CookieHeaderSource {
    fun headerFor(url: String): String?
}
