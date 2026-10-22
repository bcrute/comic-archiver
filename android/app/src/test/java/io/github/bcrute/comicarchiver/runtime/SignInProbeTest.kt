package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.signin.SignInVerification
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

/**
 * The settle-time probe's lifecycle, pinned off-device against a faked bridge and a
 * faked engine (the real one needs WebView): the desktop window's retry shape — a "not
 * yet signed in" answer keeps asking within the session; the loop stops when its
 * session is gone or the engine left the sign-in screens; a bridge that cannot ask
 * fails terminally once; and a replacement loop never puts a second network probe in
 * flight. What these tests cannot cover — the engine's own rejection of a stale answer
 * — is its main-thread session check, reviewed in source and exercised on the phone.
 */
class SignInProbeTest {

    private val notYet = """{"ok":true,"result":{"signed_in":false,"verified":false,"reason":"not_signed_in"}}"""
    private val unavailable = """{"ok":true,"result":{"signed_in":false,"verified":false,"reason":"unavailable"}}"""
    private val verified = """{"ok":true,"result":{"signed_in":true,"verified":true,"reason":null}}"""
    private val broken = """{"ok":false,"error":{"code":"job_failed"}}"""

    /** Answers pop in order; an exhausted queue repeats its last element; a null answer
     *  stands for a bridge that throws. Only one dispatch is ever in flight (the probe
     *  serialises them), so the deque needs no more than its own lock. */
    private class FakeBridge(
        vararg scripted: String?,
        private val blockMs: Long = 0,
        private val onDispatch: (Int) -> Unit = {},
    ) : PythonBridge {
        private val answers = ArrayDeque(listOf(*scripted))
        private val last = scripted.lastOrNull()
        val dispatches = AtomicInteger(0)
        val inFlight = AtomicInteger(0)
        val maxInFlight = AtomicInteger(0)

        override fun dispatch(op: String, argsJson: String): String {
            if (inFlight.incrementAndGet() > maxInFlight.get()) {
                maxInFlight.set(inFlight.get())
            }
            try {
                dispatches.incrementAndGet()
                onDispatch(dispatches.get())
                if (blockMs > 0) Thread.sleep(blockMs)
                val answer = synchronized(answers) { answers.removeFirstOrNull() } ?: last
                    ?: error("no scripted answer")
                if (answer == null) throw IllegalStateException("bridge unavailable")
                return answer
            } finally {
                inFlight.decrementAndGet()
            }
        }
    }

    /** The engine surface the probe talks to, mutable from the tests. */
    private class FakeEngine(
        var session: Long = 1L,
        var state: SignInState = SignInState.Settling,
    ) : SignInVerification {
        val verifiedSessions = mutableListOf<Long>()
        val failures = mutableListOf<Pair<String, Long>>()

        override fun currentSession(): Long = session
        override fun signInState(): SignInState = state
        override fun verified(session: Long) { verifiedSessions.add(session) }
        override fun verificationFailed(code: String, session: Long) {
            failures.add(code to session)
        }
    }

    private fun probe(bridge: PythonBridge) =
        SignInProbe(bridge, "/session", retryMs = 5, engineContext = Dispatchers.Unconfined)

    private fun await(condition: () -> Boolean, what: String) {
        val deadline = System.nanoTime() + 5_000_000_000L
        while (!condition()) {
            if (System.nanoTime() > deadline) fail("timed out waiting for: $what")
            Thread.sleep(10)
        }
    }

    @Test
    fun a_not_yet_signed_in_answer_keeps_asking_until_verified() {
        val bridge = FakeBridge(notYet, notYet, verified)
        val engine = FakeEngine()
        probe(bridge).settled(engine)
        await({ engine.verifiedSessions == listOf(1L) }, "verified")
        assertEquals(3, bridge.dispatches.get())
        assertTrue(engine.failures.isEmpty())
    }

    @Test
    fun the_sites_own_unavailable_answer_keeps_the_session_open() {
        val bridge = FakeBridge(unavailable, notYet, verified)
        val engine = FakeEngine()
        probe(bridge).settled(engine)
        await({ engine.verifiedSessions == listOf(1L) }, "verified after unavailable")
        assertTrue(engine.failures.isEmpty())
    }

    @Test
    fun a_bridge_that_cannot_ask_fails_terminally_once() {
        val bridge = FakeBridge(broken)
        val engine = FakeEngine()
        probe(bridge).settled(engine)
        await({ engine.failures == listOf("signin_unavailable" to 1L) }, "terminal failure")
        assertEquals(1, bridge.dispatches.get())
        assertTrue(engine.verifiedSessions.isEmpty())
    }

    @Test
    fun a_throwing_bridge_fails_terminally_once() {
        val bridge = FakeBridge(null as String?)
        val engine = FakeEngine()
        probe(bridge).settled(engine)
        await({ engine.failures == listOf("signin_unavailable" to 1L) }, "terminal failure")
        assertTrue(engine.verifiedSessions.isEmpty())
    }

    @Test
    fun the_loop_stops_when_its_session_is_replaced() {
        // The first dispatch replaces the session, as a new Sign in would.
        val engine = FakeEngine()
        val replacing = FakeBridge(notYet, onDispatch = { engine.session = 2L })
        probe(replacing).settled(engine)
        await({ replacing.dispatches.get() >= 1 }, "first dispatch")
        Thread.sleep(100)
        val stopped = replacing.dispatches.get()
        Thread.sleep(100)
        assertEquals(stopped, replacing.dispatches.get())
        // The old session's loop (if any) must never verify or fail the new one.
        assertTrue(engine.verifiedSessions.isEmpty())
        assertTrue(engine.failures.isEmpty())
    }

    @Test
    fun the_loop_stops_when_the_engine_left_the_sign_in_screens() {
        val bridge = FakeBridge(notYet)
        val engine = FakeEngine()
        val bounded = FakeBridge(notYet, onDispatch = { engine.state = SignInState.Failed("signin_incomplete") })
        probe(bounded).settled(engine)
        await({ bounded.dispatches.get() >= 1 }, "first dispatch")
        Thread.sleep(100)
        val stopped = bounded.dispatches.get()
        Thread.sleep(100)
        assertEquals(stopped, bounded.dispatches.get())
        assertTrue(engine.verifiedSessions.isEmpty())
    }

    @Test
    fun a_replacement_loop_never_puts_a_second_probe_in_flight() {
        // A slow bridge holds each dispatch open; the second settle arrives while the
        // first question is still completing.
        val bridge = FakeBridge(notYet, verified, blockMs = 60)
        val engine = FakeEngine()
        val probe = probe(bridge)
        probe.settled(engine)
        Thread.sleep(20)
        engine.session = 2L
        probe.settled(engine)
        await({ engine.verifiedSessions == listOf(2L) }, "second loop verified")
        Thread.sleep(150)
        assertTrue("at most one probe in flight", bridge.maxInFlight.get() <= 1)
    }
}
