package io.github.bcrute.comicarchiver.signin

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** The cookie store failed or is not ready; the message carries no URL, value or account detail. */
class CookieSourceException(message: String) : Exception(message)

/**
 * The app's authoritative WebView cookie store behind the frozen CookieHeaderSource and
 * SetCookieSink seams (baseline 2a829ea).
 *
 * Every CookieManager call is marshalled to the main thread and waited for with a bound, so
 * worker threads (the embedded Python Transport) can never block forever and never touch
 * WebView off the main thread. [headerFor] answers only the site's own HTTPS cookie hosts —
 * every other origin gets null, so credentials cannot cross origins through this seam — and
 * is asked again for every request, never cached. [apply] hands raw Set-Cookie strings to
 * the store's own parser, unchanged, and completes before returning, so the next request
 * reads the updated store. Cookies persist in WebView's own app-private store; [flush]
 * after a sign-in settles makes that persistence immediate.
 *
 * Nothing here logs a cookie value, URL or account detail: fixed, class-free messages only.
 *
 * @param cookieHosts the exact hosts whose cookies the downloader may carry (the site's
 * `cookie_hosts`, e.g. `setOf("tapas.io")`).
 * @param timeoutMs how long a worker thread waits for the main thread before giving up.
 */
class WebViewCookieStore(
    private val cookieHosts: Set<String>,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
) : CookieHeaderSource, SetCookieSink {

    private val main = Handler(Looper.getMainLooper())

    /** The live Cookie header for exactly this URL, or null when the store would send none. */
    override fun headerFor(url: String): String? = onMain { manager ->
        if (!isSiteUrl(url)) null else manager.getCookie(url)
    }

    /** Raw Set-Cookie values for a site URL to the store's own parser; complete before returning. */
    override fun apply(url: String, setCookieValues: List<String>) {
        if (setCookieValues.isEmpty() || !isSiteUrl(url)) return
        onMain { manager ->
            // The store's own parser reads each string's attributes; this side never edits
            // or rebuilds them, and keeps no copy.
            for (raw in setCookieValues) {
                manager.setCookie(url, raw)
            }
            manager.flush()
        }
    }

    /**
     * Remove every cookie the store holds for this app, bounded in time. The WebView store
     * keeps every site's cookies together, so a sign-out or a site switch clears all of it:
     * OAuth and other-site state never survives. Must be called off the main thread; the
     * main thread uses [clearAllAsync] so it never waits on its own callback.
     */
    fun clearAll(): Boolean {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            // The main thread would be waiting for a callback it must deliver itself.
            throw CookieSourceException("The cookie store is cleared asynchronously on the main thread.")
        }
        val done = CountDownLatch(1)
        val cleared = AtomicReference(false)
        if (!main.post {
                try {
                    val manager = store()
                    manager.removeAllCookies { any -> cleared.set(any) }
                    manager.flush()
                } catch (_: Exception) {
                    // Counted down below either way; the fixed outcome message stays safe.
                } finally {
                    done.countDown()
                }
            }
        ) {
            return false
        }
        return done.await(timeoutMs, TimeUnit.MILLISECONDS) && cleared.get()
    }

    /** Ask the store asynchronously and report on the main thread; the main thread never waits on itself. */
    fun clearAllAsync(onCleared: (Boolean) -> Unit) {
        main.post {
            try {
                store().removeAllCookies { any -> store().flush(); onCleared(any) }
            } catch (_: Exception) {
                onCleared(false)
            }
        }
    }

    /** Whether the store holds any cookie for this URL; used for settle detection. No auth claim. */
    fun hasCookies(url: String): Boolean = onMain { manager ->
        !isSiteUrl(url) || manager.hasCookies(url)
    }

    /** Make the store's already-private persistence immediate; called when a sign-in settles. */
    fun flush() {
        main.post {
            try {
                store().flush()
            } catch (_: Exception) {
                // Persistence remains the store's own eventual duty; nothing to report here.
            }
        }
    }

    /** Run [block] with the store on the main thread, waiting no longer than [timeoutMs]. */
    private fun <T> onMain(block: (CookieManager) -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            return try {
                block(store())
            } catch (error: CookieSourceException) {
                throw error
            } catch (_: Exception) {
                throw CookieSourceException(UNREADY)
            }
        }
        val answer = AtomicReference<Result<T>>()
        val done = CountDownLatch(1)
        if (!main.post {
                try {
                    answer.set(Result.success(block(store())))
                } catch (error: Exception) {
                    answer.set(Result.failure(error))
                } finally {
                    done.countDown()
                }
            }
        ) {
            throw CookieSourceException(UNREADY)
        }
        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            throw CookieSourceException(TIMED_OUT)
        }
        return answer.get().getOrElse { error ->
            if (error is CookieSourceException) throw error
            throw CookieSourceException(FAILED)
        }
    }

    private fun store(): CookieManager = try {
        CookieManager.getInstance()
    } catch (_: Exception) {
        // The WebView provider is not loaded yet; the app initializes it at start-up.
        throw CookieSourceException(UNREADY)
    }

    /** The site's own HTTPS cookie hosts only; every other origin gets no cookies from this seam. */
    private fun isSiteUrl(url: String): Boolean {
        val address = try {
            Uri.parse(url)
        } catch (_: Exception) {
            return false
        } ?: return false
        if (!address.scheme.equals("https", ignoreCase = true)) return false
        val host = address.host?.lowercase() ?: return false
        return host in cookieHosts
    }

    companion object {
        const val DEFAULT_TIMEOUT_MS = 3_000L
        private const val UNREADY = "The app sign-in store is not ready; sign in again."
        private const val TIMED_OUT = "The app sign-in store did not answer in time; sign in again."
        private const val FAILED = "The app sign-in store is unavailable; sign in again."
    }
}
