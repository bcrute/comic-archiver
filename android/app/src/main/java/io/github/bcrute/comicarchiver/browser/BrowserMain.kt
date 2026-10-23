package io.github.bcrute.comicarchiver.browser

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** A timed-out queued task never starts; a late resource result is closed on Main. */
internal fun <T> onBrowserMain(
    timeoutMs: Long = 10_000,
    discard: (T) -> Unit = {},
    action: () -> T,
): T {
    if (Looper.myLooper() == Looper.getMainLooper()) return action()
    val gate = Any()
    val done = CountDownLatch(1)
    var abandoned = false
    var answer: Result<T>? = null
    val posted = Handler(Looper.getMainLooper()).post {
        if (synchronized(gate) { abandoned }) return@post
        val made = runCatching(action)
        val stale = synchronized(gate) {
            if (abandoned) true else { answer = made; done.countDown(); false }
        }
        if (stale) made.onSuccess { try { discard(it) } catch (_: Exception) { } }
    }
    if (!posted) throw ScriptRequestException(ScriptFailure.CLOSED)
    try {
        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) synchronized(gate) {
            if (answer == null) abandoned = true
        }
    } catch (_: InterruptedException) {
        val late = synchronized(gate) { abandoned = true; answer }
        late?.onSuccess { value -> Handler(Looper.getMainLooper()).post {
            try { discard(value) } catch (_: Exception) { }
        } }
        Thread.currentThread().interrupt()
        throw ScriptRequestException(ScriptFailure.CANCELLED)
    }
    val result = synchronized(gate) { answer }
        ?: throw ScriptRequestException(ScriptFailure.TIMEOUT)
    return result.getOrThrow()
}
