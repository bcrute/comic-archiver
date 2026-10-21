package io.github.bcrute.comicarchiver.signin

/**
 * Own-origin Set-Cookie forwarding (docs/android-beta-contracts.md §4), frozen.
 *
 * The embedded Python Transport hands the site's own hosts' raw Set-Cookie response values
 * here, exactly as the server sent them; the implementation applies them to the platform's
 * app-private cookie store, which alone parses attributes (domain, path, expiry, rotation,
 * deletion). Implementations (WebView/session line) must:
 *  - complete before returning (bounded, synchronous): the next request reads the updated
 *    store;
 *  - refuse values for any URL outside the loaded site's own hosts — the core Transport also
 *    enforces this on its side, so CDNs and other origins never arrive;
 *  - never log the values.
 *
 * The core does not update its cookie jar when a sink is present: the platform store stays
 * the single cookie authority.
 */
interface SetCookieSink {
    fun apply(url: String, setCookieValues: List<String>)
}
