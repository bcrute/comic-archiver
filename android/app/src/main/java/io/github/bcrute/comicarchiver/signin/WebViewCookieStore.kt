package io.github.bcrute.comicarchiver.signin

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * The app's authoritative WebView cookie store behind [CookieHeaderProvider].
 *
 * Every CookieManager call is marshalled to the main thread and waited for with a bound, so
 * worker threads (the embedded Python downloader) can never block forever and never touch
 * WebView off the main thread. Only the site's own HTTPS cookie hosts are ever asked: a URL
 * for a CDN or any other origin gets an empty header, so credentials cannot cross origins
 * through this seam. Cookies persist in WebView's own app-private store; [flush] after a
 * sign-in settles or cookies are taken makes that persistence immediate.
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
) : CookieHeaderProvider {

    private val main = Handler(Looper.getMainLooper())

    override fun cookieHeader(url: String): String = onMain { manager ->
        if (!isSiteUrl(url)) "" else manager.getCookie(url) ?: ""
    }

    override fun setSessionCookies(url: String, setCookieValues: List<String>): Boolean =
        onMain { manager ->
            if (!isSiteUrl(url) || setCookieValues.isEmpty()) {
                return@onMain false
            }
            // The store's own parser reads each string's attributes; this side never edits
            // or rebuilds them, and keeps no copy.
            // CookieManager.setCookie returns nothing; every raw string is delivered as-is.
            for (raw in setCookieValues) {
                manager.setCookie(url, raw)
            }
            manager.flush()
            true
        }

    override fun clearAll(): Boolean {
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

    /** Whether the store holds any cookie for this URL; used for sign-in settle detection. */
    fun hasCookies(url: String): Boolean = onMain { manager ->
        !isSiteUrl(url) || !manager.getCookie(url).isNullOrEmpty()
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
