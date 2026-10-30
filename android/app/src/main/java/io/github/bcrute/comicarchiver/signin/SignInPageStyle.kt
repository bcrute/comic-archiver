package io.github.bcrute.comicarchiver.signin

import android.webkit.WebView
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject

/**
 * Hides the parts of a site's sign-in page named in [SiteAddress.signInHidden], such as a
 * cookie notice that covers the login links on a phone. It adds one style rule and nothing
 * else: the script runs in its own world, only on the site's own HTTPS pages, and never
 * clicks, answers or reads anything, so the page has nothing to call and no choice is made
 * for the person.
 */
internal object SignInPageStyle {
    private const val WORLD = "comic-archiver-signin-style"
    // One id or class name each; anything else could end the rule and add another.
    private val SELECTOR = Regex("[#.][A-Za-z][A-Za-z0-9_-]*")

    /** The site's own sign-in pages, as exact HTTPS origins. */
    fun origins(site: SiteAddress): Set<String> =
        (site.cookieHosts + site.signInHosts).map { "https://$it" }.toSet() + site.home

    /** The script, or null when the site names nothing to hide. */
    fun script(selectors: List<String>): String? {
        if (selectors.isEmpty()) return null
        require(selectors.all { SELECTOR.matches(it) }) { "Name page parts by one id or class." }
        val rule = selectors.joinToString(",") + "{display:none!important}"
        return """
            (() => {
                const style = document.createElement('style');
                style.textContent = ${JSONObject.quote(rule)};
                const add = () => (document.head || document.documentElement).appendChild(style);
                if (document.documentElement) add();
                else document.addEventListener('readystatechange', add, {once: true});
            })();
        """.trimIndent()
    }

    /** Installs the rule for every page the view loads from now; null when there is none. */
    fun install(view: WebView, site: SiteAddress): ScriptHandler? {
        val source = script(site.signInHidden) ?: return null
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.JS_INJECTION_IN_FRAME_AND_WORLD)) return null
        val world = WebViewCompat.getExecutionWorld(view, WORLD)
        return WebViewCompat.addJavaScriptOnEvent(view, source, WebViewCompat.INJECTION_EVENT_DOCUMENT_START,
            origins(site), world)
    }
}
