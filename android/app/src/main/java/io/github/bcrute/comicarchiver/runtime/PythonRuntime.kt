package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore

/**
 * Process-wide Python bootstrap (native line). Starts the embedded runtime once and
 * registers the WebView cookie store with app_bridge's seams exactly once, so the store is
 * the single cookie authority for both sign-in and the worker's Transport. The store is
 * registered as itself: the bridge recognises it by its frozen headerFor/apply methods
 * and hands it to the core's own adapter at use time.
 *
 * Must first be called from the main thread (WebView's CookieManager is set up there);
 * later callers from any thread get the already-started runtime.
 */
object PythonRuntime {
    @Volatile
    private var seamsRegistered = false

    fun ensureStarted(context: Context, cookieStore: WebViewCookieStore) {
        if (!Python.isStarted()) {
            synchronized(this) {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(context.applicationContext))
                }
            }
        }
        if (!seamsRegistered) {
            synchronized(this) {
                if (!seamsRegistered) {
                    Python.getInstance().getModule("app_bridge")
                        .callAttr("set_cookie_seams", cookieStore)
                    seamsRegistered = true
                }
            }
        }
    }

    /**
     * The sign-in engine's site table, from the core's own sites module (its
     * android_adapter.site_addresses): plain public-safe addresses only, so no second
     * site list exists anywhere in the app.
     */
    fun siteAddresses(): Map<String, SiteAddress> {
        val rows = Python.getInstance().getModule("comic_archive.android_adapter")
            .callAttr("site_addresses").asList()
        val table = mutableMapOf<String, SiteAddress>()
        for (row in rows) {
            table[row["key"].toString()] = SiteAddress(
                home = row["home"].toString(),
                signInPath = row["signInPath"].toString(),
                cookieHosts = row["cookieHosts"].asList().map { it.toString() }.toSet(),
            )
        }
        return table
    }
}
