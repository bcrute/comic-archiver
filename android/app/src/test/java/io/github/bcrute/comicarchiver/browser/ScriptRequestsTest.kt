package io.github.bcrute.comicarchiver.browser

import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class ScriptRequestsTest {
    private fun failure(expected: ScriptFailure, action: () -> Unit) {
        try { action(); fail("Expected the fixed browser refusal") }
        catch (error: ScriptRequestException) {
            assertEquals(expected, error.failure)
            assertEquals(expected.words, error.message)
        }
    }

    @Test fun navigationReleasesWaiterAndOldResultsCannotCompleteTheNewDocument() {
        val requests = ScriptRequests()
        assertTrue(requests.attach("first-document"))
        val old = requests.begin()
        requests.navigationStarted()
        failure(ScriptFailure.CHANGED) { requests.await(old, 100) }
        assertTrue(requests.attach("second-document"))
        val fresh = requests.begin()
        assertFalse(requests.complete(old, "private-stale-result"))
        assertTrue(requests.current(fresh))
        assertTrue(requests.complete(fresh, "\"current result\""))
        assertEquals("\"current result\"", requests.await(fresh, 100))
    }

    @Test fun cancelledAndTimedOutReadsCannotDeliverLateRendererAnswers() {
        val requests = ScriptRequests()
        requests.attach("same-document")
        val stopped = requests.begin()
        requests.cancelPending()
        failure(ScriptFailure.CANCELLED) { requests.await(stopped, 100) }
        assertFalse(requests.complete(stopped, "late"))
        val timedOut = requests.begin()
        failure(ScriptFailure.TIMEOUT) { requests.await(timedOut, 1) }
        assertFalse(requests.complete(timedOut, "late timeout"))
        val next = requests.begin()
        assertTrue(requests.complete(next, "null"))
        assertEquals("null", requests.await(next, 100))
    }

    @Test fun closeIsTerminalAndCannotBeResurrectedByReadyOrResultCallbacks() {
        val requests = ScriptRequests()
        requests.attach("document")
        val pending = requests.begin()
        requests.close()
        requests.close()
        failure(ScriptFailure.CLOSED) { requests.await(pending, 100) }
        assertFalse(requests.attach("late-ready"))
        assertFalse(requests.complete(pending, "late-result"))
        failure(ScriptFailure.CLOSED) { requests.begin() }
    }

    @Test fun capacityIsBoundedAndFailureReleasesOnlyItsOwnSlot() {
        val requests = ScriptRequests(maxPending = 2)
        failure(ScriptFailure.NOT_READY) { requests.begin() }
        assertFalse(requests.attach("a private URL/unsupported token"))
        requests.attach("document")
        val first = requests.begin()
        val second = requests.begin()
        failure(ScriptFailure.BUSY) { requests.begin() }
        assertTrue(requests.refuse(first, ScriptFailure.SCRIPT))
        failure(ScriptFailure.SCRIPT) { requests.await(first, 100) }
        val third = requests.begin()
        assertTrue(requests.current(second))
        assertTrue(requests.current(third))
        requests.close()
    }

    @Test fun duplicateReadyKeepsPendingReadWhileANewDocumentInvalidatesIt() {
        val requests = ScriptRequests()
        requests.attach("first")
        val read = requests.begin()
        requests.attach("first")
        assertTrue(requests.current(read))
        requests.attach("second")
        failure(ScriptFailure.CHANGED) { requests.await(read, 100) }
        assertFalse(requests.complete(read, "late"))
    }

    @Test fun cancellationReleasesAnActuallyWaitingWorker() {
        val requests = ScriptRequests()
        requests.attach("document")
        val ticket = requests.begin()
        val started = CountDownLatch(1)
        val finished = CountDownLatch(1)
        val result = AtomicReference<ScriptFailure>()
        val worker = Thread {
            started.countDown()
            try { requests.await(ticket, 30_000) }
            catch (error: ScriptRequestException) { result.set(error.failure) }
            finally { finished.countDown() }
        }
        worker.start()
        try {
            assertTrue(started.await(2, TimeUnit.SECONDS))
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
            while (worker.state != Thread.State.TIMED_WAITING && System.nanoTime() < deadline) Thread.yield()
            assertEquals(Thread.State.TIMED_WAITING, worker.state)
            requests.cancelPending()
            assertTrue(finished.await(2, TimeUnit.SECONDS))
            assertEquals(ScriptFailure.CANCELLED, result.get())
        } finally {
            requests.close()
            worker.interrupt()
            worker.join(2_000)
        }
    }

    @Test fun interruptedWorkerKeepsItsInterruptAndCannotPublishALateResult() {
        val requests = ScriptRequests()
        requests.attach("document")
        val ticket = requests.begin()
        val finished = CountDownLatch(1)
        val result = AtomicReference<ScriptFailure>()
        val interrupted = AtomicReference<Boolean>()
        val worker = Thread {
            try { requests.await(ticket, 30_000) }
            catch (error: ScriptRequestException) {
                result.set(error.failure)
                interrupted.set(Thread.currentThread().isInterrupted)
            } finally { finished.countDown() }
        }
        worker.start()
        try {
            worker.interrupt()
            assertTrue(finished.await(2, TimeUnit.SECONDS))
            assertEquals(ScriptFailure.CANCELLED, result.get())
            assertEquals(true, interrupted.get())
            assertFalse(requests.complete(ticket, "late"))
        } finally {
            requests.close()
            worker.interrupt()
            worker.join(2_000)
        }
    }
}
