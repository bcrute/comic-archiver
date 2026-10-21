package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPython
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore

/**
 * Process-wide Python bootstrap (native line). Starts the embedded runtime once and
 * registers the WebView cookie store with app_bridge's seams exactly once, so the store is
 * the single cookie authority for both sign-in and the worker's Transport.
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
                    Python.start(AndroidPython(context.applicationContext))
                }
            }
        }
        if (!seamsRegistered) {
            synchronized(this) {
                if (!seamsRegistered) {
                    Python.getInstance().getModule("app_bridge")
                        .callAttr("set_cookie_seams", cookieStore, cookieStore)
                    seamsRegistered = true
                }
            }
        }
    }
}
