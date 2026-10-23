package io.github.bcrute.comicarchiver.browser

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Fixed public failures; browser exception messages and script bodies never leave this boundary. */
internal enum class ScriptFailure(val words: String) {
    NOT_READY("The reading page is not ready yet."),
    CHANGED("The reading page changed. Try the read again."),
    CLOSED("The reading browser has closed."),
    CANCELLED("The browser read was stopped."),
    TIMEOUT("The reading page did not answer in time."),
    BUSY("The reading browser is busy. Try the read again."),
    TOO_LARGE("The browser read exceeded its size limit."),
    SCRIPT("The reading page could not answer this read."),
}

internal class ScriptRequestException(val failure: ScriptFailure) : RuntimeException(failure.words)

/**
 * Tracks native-owned reads, not commands from page JavaScript. A callback keeps its
 * original ticket, so navigation, cancellation and close cannot deliver an old result
 * to a later document even when a renderer callback arrives out of order.
 */
internal class ScriptRequests(private val maxPending: Int = 8) {
    init { require(maxPending in 1..32) }

    internal class Ticket internal constructor(
        val id: Long,
        val document: String,
        val generation: Long,
    ) {
        internal val latch = CountDownLatch(1)
        @Volatile internal var result: String? = null
        @Volatile internal var failure: ScriptFailure? = null

        internal fun finish(value: String? = null, problem: ScriptFailure? = null) {
            result = value
            failure = problem
            latch.countDown()
        }
    }

    private val gate = Any()
    private val pending = mutableMapOf<Long, Ticket>()
    private var nextId = 0L
    private var generation = 0L
    private var document: String? = null
    private var closed = false

    fun attach(token: String): Boolean = synchronized(gate) {
        if (closed || !token.matches(Regex("[A-Za-z0-9_.:-]{1,160}"))) return@synchronized false
        if (document == token) return@synchronized true
        invalidate(ScriptFailure.CHANGED)
        document = token
        true
    }

    fun begin(): Ticket = synchronized(gate) {
        if (closed) throw ScriptRequestException(ScriptFailure.CLOSED)
        val token = document ?: throw ScriptRequestException(ScriptFailure.NOT_READY)
        if (pending.size >= maxPending) throw ScriptRequestException(ScriptFailure.BUSY)
        Ticket(++nextId, token, generation).also { pending[it.id] = it }
    }

    fun current(ticket: Ticket): Boolean = synchronized(gate) { owns(ticket) }

    fun complete(ticket: Ticket, encodedValue: String): Boolean = synchronized(gate) {
        if (!owns(ticket)) return@synchronized false
        pending.remove(ticket.id)
        ticket.finish(value = encodedValue)
        true
    }

    fun refuse(ticket: Ticket, problem: ScriptFailure): Boolean = synchronized(gate) {
        if (!owns(ticket)) return@synchronized false
        pending.remove(ticket.id)
        ticket.finish(problem = problem)
        true
    }

    fun await(ticket: Ticket, timeoutMs: Long): String {
        require(timeoutMs in 1..45_000)
        try {
            if (!ticket.latch.await(timeoutMs, TimeUnit.MILLISECONDS)) refuse(ticket, ScriptFailure.TIMEOUT)
        } catch (_: InterruptedException) {
            refuse(ticket, ScriptFailure.CANCELLED)
            Thread.currentThread().interrupt()
            throw ScriptRequestException(ScriptFailure.CANCELLED)
        }
        ticket.failure?.let { throw ScriptRequestException(it) }
        return ticket.result ?: throw ScriptRequestException(ScriptFailure.SCRIPT)
    }

    fun navigationStarted() = synchronized(gate) {
        invalidate(ScriptFailure.CHANGED)
        document = null
    }

    fun cancelPending() = synchronized(gate) { invalidate(ScriptFailure.CANCELLED) }

    fun close() = synchronized(gate) {
        closed = true
        invalidate(ScriptFailure.CLOSED)
        document = null
    }

    private fun owns(ticket: Ticket): Boolean =
        !closed && generation == ticket.generation && document == ticket.document && pending[ticket.id] === ticket

    private fun invalidate(problem: ScriptFailure) {
        generation++
        pending.values.forEach { it.finish(problem = problem) }
        pending.clear()
    }
}
