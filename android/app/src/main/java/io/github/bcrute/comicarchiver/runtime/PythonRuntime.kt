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
            // PyObject's map access answers nullable; a row without every field is not a
            // site (the caller keeps the beta fallback rather than an entry that cannot
            // open a sign-in page).
            val key = row["key"]?.toString()
            val home = row["home"]?.toString()
            val signInPath = row["signInPath"]?.toString()
            val hosts = row["cookieHosts"]?.asList()?.map { it.toString() }
            // The sign-in journey's extra landing hosts (the mobile mirror); a row without
            // them still loads — its journey simply never leaves the cookie hosts.
            val landing = row["signinHosts"]?.asList()?.map { it.toString() } ?: emptyList()
            if (key == null || home == null || signInPath == null || hosts.isNullOrEmpty()) {
                continue
            }
            table[key] = SiteAddress(home, signInPath, hosts.toSet(), landing.toSet())
        }
        return table
    }
}
