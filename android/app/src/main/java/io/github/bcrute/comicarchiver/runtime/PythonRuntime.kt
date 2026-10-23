package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.PyObject
import com.chaquo.python.android.AndroidPlatform
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.browser.NativeBrowserFactory

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

    fun ensureStarted(context: Context, cookieStore: WebViewCookieStore, browser: NativeBrowserFactory? = null) {
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
                    Python.getInstance().getModule("app_bridge")
                        .callAttr("enable_android_cloud")
                    Python.getInstance().getModule("app_bridge")
                        .callAttr("set_browser_seam", browser)
                    seamsRegistered = true
                }
            }
        }
    }

    /** Called only after the former worker is stopped and before the next native attempt. */
    fun selectCookieStore(store: WebViewCookieStore, siteKey: String) {
        Python.getInstance().getModule("app_bridge").callAttr("set_cookie_seams", store, null, siteKey)
    }

    fun deactivateCookieStore() {
        Python.getInstance().getModule("app_bridge").callAttr("deactivate_cookie_seams")
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
            // PyObject.get accesses Python attributes, even though PyObject implements
            // Map. Site rows are dictionaries: use the container view and Python keys.
            val fields = row.asMap()
            fun field(name: String): PyObject? = fields[PyObject.fromJava(name)]
            val key = field("key")?.toString()
            val home = field("home")?.toString()
            val signInPath = field("signInPath")?.toString()
            val hosts = field("cookieHosts")?.asList()?.map { it.toString() }
            // The sign-in journey's extra landing hosts (the mobile mirror); a row without
            // them still loads — its journey simply never leaves the cookie hosts.
            val landing = field("signinHosts")?.asList()?.map { it.toString() } ?: emptyList()
            if (key == null || home == null || signInPath == null || hosts.isNullOrEmpty()) {
                continue
            }
            val name = field("name")?.toString() ?: key
            val folder = field("folder")?.toString() ?: ""
            table[key] = SiteAddress(home, signInPath, hosts.toSet(), landing.toSet(), name, folder)
        }
        return table
    }
}
