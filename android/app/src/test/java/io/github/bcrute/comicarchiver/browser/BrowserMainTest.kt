package io.github.bcrute.comicarchiver.browser

import android.app.Application
import android.os.Looper
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
@LooperMode(LooperMode.Mode.PAUSED)
class BrowserMainTest {
    private fun waiting(worker: Thread) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
        while (worker.state != Thread.State.TIMED_WAITING && System.nanoTime() < deadline) Thread.yield()
        assertEquals(Thread.State.TIMED_WAITING, worker.state)
    }

    @Test fun aTimedOutQueuedActionCannotRunLater() {
        val calls = AtomicInteger()
        val failure = AtomicReference<ScriptFailure>()
        val worker = Thread {
            try { onBrowserMain(timeoutMs = 5) { calls.incrementAndGet() } }
            catch (error: ScriptRequestException) { failure.set(error.failure) }
        }
        worker.start()
        try {
            worker.join(2_000)
            assertFalse(worker.isAlive)
            assertEquals(ScriptFailure.TIMEOUT, failure.get())
            Shadows.shadowOf(Looper.getMainLooper()).idle()
            assertEquals(0, calls.get())
        } finally { worker.interrupt(); worker.join(2_000) }
    }

    @Test fun aResourceCompletedAfterItsCallerWasInterruptedIsDiscardedExactlyOnce() {
        val discards = AtomicInteger()
        val failure = AtomicReference<ScriptFailure>()
        val workerRef = AtomicReference<Thread>()
        val resource = Any()
        val worker = Thread {
            try {
                onBrowserMain(discard = { value: Any -> assertSame(resource, value); discards.incrementAndGet() }) {
                    workerRef.get().interrupt()
                    resource
                }
            } catch (error: ScriptRequestException) { failure.set(error.failure) }
        }
        workerRef.set(worker)
        worker.start()
        try {
            waiting(worker)
            Shadows.shadowOf(Looper.getMainLooper()).idle()
            worker.join(2_000)
            assertFalse(worker.isAlive)
            Shadows.shadowOf(Looper.getMainLooper()).idle()
            assertEquals(ScriptFailure.CANCELLED, failure.get())
            assertEquals(1, discards.get())
        } finally { worker.interrupt(); worker.join(2_000) }
    }

    @Test fun aCompletedResourceReturnsToItsOwnerWithoutBeingDiscarded() {
        val resource = Any()
        val answer = AtomicReference<Any>()
        val discards = AtomicInteger()
        val worker = Thread {
            answer.set(onBrowserMain(discard = { _: Any -> discards.incrementAndGet() }) { resource })
        }
        worker.start()
        try {
            waiting(worker)
            Shadows.shadowOf(Looper.getMainLooper()).idle()
            worker.join(2_000)
            assertFalse(worker.isAlive)
            assertSame(resource, answer.get())
            assertEquals(0, discards.get())
        } finally { worker.interrupt(); worker.join(2_000) }
    }
}
