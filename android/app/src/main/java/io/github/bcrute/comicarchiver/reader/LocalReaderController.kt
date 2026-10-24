package io.github.bcrute.comicarchiver.reader

import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.library.PreparedPortableReading
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors

/** Process-owned, serial reader I/O. It cannot sign in, admit Go or open arbitrary paths. */
internal class LocalReaderController(private val bridge: PythonBridge, private val scope: CoroutineScope,
    private val currentRoot: () -> File?) {
    private data class Ticket(val id: String, val root: File, val exportId: String,
        val selected: PreparedPortableReading? = null)
    private val executor = Executors.newSingleThreadExecutor { work -> Thread(work, "local-reader").apply { isDaemon = true } }
    private val io = executor.asCoroutineDispatcher()
    private val _state = MutableStateFlow(LocalReaderState())
    val state: StateFlow<LocalReaderState> = _state
    private var ticket: Ticket? = null
    private var version = 0L
    private var edits = 0L
    private var dirty = false
    private var debounce: Job? = null

    private fun request(target: Ticket, op: String, extra: JSONObject = JSONObject()): JSONObject {
        extra.put("root", target.root.absolutePath).put("reader_id", target.id)
        return ReaderProtocol.result(bridge.dispatch(op, extra.toString()))
    }
    private fun current(target: Ticket, captured: Long) = ticket == target && version == captured &&
        currentRoot()?.absolutePath == target.root.absolutePath
    private fun error(error: Exception) = (error as? ReaderRefusal)?.message
        ?: "This page could not be displayed here. Close the reader and use Open, or refresh the library."
    private fun loaded(target: Ticket, value: JSONObject): Pair<ReaderBook, ReaderDocument> {
        val book = ReaderProtocol.book(value, target.id)
        val page = request(target, "reader_page", JSONObject().put("unit", book.record.position.unit))
        return book to LocalReaderMedia.decode(page, book, book.record.position.unit)
    }
    private fun apply(book: ReaderBook, document: ReaderDocument, changedProgress: Boolean = false) {
        dirty = changedProgress && book.warning != null
        _state.value = _state.value.copy(book = book, document = document, position = book.record.position,
            preferences = book.record.preferences, generation = _state.value.generation + 1,
            loading = false, saving = false, error = null, feedback = null)
    }

    fun open(id: String) {
        if (_state.value.open || _state.value.closing) return
        val root = currentRoot()
        if (root == null || !id.matches(Regex("[a-f0-9]{64}"))) {
            _state.value = LocalReaderState(feedback = "The saved file is unavailable. Refresh files and choose Read here again.")
            return
        }
        val target = Ticket(UUID.randomUUID().toString().replace("-", ""), root, id)
        openTarget(target)
    }

    fun openPrepared(source: PreparedPortableReading): Boolean {
        if (_state.value.open || _state.value.closing || currentRoot()?.absolutePath != source.workspace.absolutePath) return false
        openTarget(Ticket(UUID.randomUUID().toString().replace("-", ""), source.workspace, source.relative, source))
        return true
    }

    private fun openTarget(target: Ticket) {
        ticket = target; dirty = false; edits++
        val captured = ++version
        _state.value = LocalReaderState(id = target.id, loading = true)
        scope.launch {
            try {
                val result = withContext(io) {
                    val choice = if (target.selected == null) "id" else "selected"
                    loaded(target, request(target, "reader_open", JSONObject().put(choice, target.exportId)))
                }
                if (current(target, captured)) apply(result.first, result.second)
                else if (ticket == target && version == captured) contextChanged(currentRoot())
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (failure: Exception) {
                if (ticket == target && version == captured) _state.value = _state.value.copy(loading = false, error = error(failure))
            }
        }
    }

    fun scrolled(offset: Int) {
        val prior = _state.value
        if (!prior.open || prior.loading || prior.closing || prior.document == null || offset !in 0..10_000 || offset == prior.position.offset) return
        edits++; dirty = true
        _state.value = prior.copy(position = prior.position.copy(offset = offset))
        scheduleSave()
    }
    private fun scheduleSave() {
        debounce?.cancel()
        debounce = scope.launch { delay(500); if (!_state.value.busy) save() }
    }
    private fun saveRequest(target: Ticket, snapshot: LocalReaderState, bookmark: Boolean = false): ReaderBook =
        ReaderProtocol.book(request(target, "reader_save", JSONObject().put("position", snapshot.position.json())
            .put("preferences", snapshot.preferences.json()).put("bookmark", bookmark)), target.id)

    fun bookmark() = save(bookmark = true)
    private fun save(bookmark: Boolean = false) {
        val target = ticket ?: return
        val snapshot = _state.value
        if (snapshot.busy || snapshot.book == null || snapshot.document == null || (!dirty && !bookmark)) return
        debounce?.cancel()
        val captured = version; val savedEdit = edits
        _state.value = snapshot.copy(saving = true)
        scope.launch {
            try {
                val book = withContext(io) { saveRequest(target, snapshot, bookmark) }
                if (current(target, captured)) {
                    val changed = edits != savedEdit
                    dirty = changed || book.warning != null
                    _state.value = _state.value.copy(book = book, saving = false)
                    if (changed && book.warning == null) scheduleSave()
                } else if (ticket == target && version == captured) contextChanged(currentRoot())
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) {
                if (ticket == target && version == captured) {
                    dirty = true
                    _state.value = _state.value.copy(saving = false, book = snapshot.book.copy(
                        warning = "Reading progress could not be saved. Reading still works; try Reload saved progress."))
                }
            }
        }
    }

    fun preferences(value: ReadingPreferences) {
        if (_state.value.busy || _state.value.document == null || value.fit !in setOf("width", "page") ||
            !value.textScale.isFinite() || value.textScale !in .75..2.0) return
        edits++; dirty = true
        _state.value = _state.value.copy(preferences = value, generation = _state.value.generation + 1)
        save()
    }

    fun navigate(position: ReadingPosition) {
        val target = ticket ?: return
        val snapshot = _state.value
        val book = snapshot.book ?: return
        if (snapshot.busy || position.unit !in 0 until book.record.units || position.offset !in 0..10_000) return
        debounce?.cancel()
        val captured = version; val savePrior = dirty
        _state.value = snapshot.copy(loading = true)
        scope.launch {
            try {
                val result = withContext(io) {
                    if (savePrior) saveRequest(target, snapshot)
                    val page = request(target, "reader_page", JSONObject().put("unit", position.unit))
                    val document = LocalReaderMedia.decode(page, book, position.unit)
                    val saved = saveRequest(target, snapshot.copy(position = position))
                    saved to document
                }
                if (current(target, captured)) apply(result.first, result.second, changedProgress = true)
                else if (ticket == target && version == captured) contextChanged(currentRoot())
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (failure: Exception) {
                if (ticket == target && version == captured) _state.value = snapshot.copy(loading = false, error = error(failure))
            }
        }
    }
    fun next() = navigate(_state.value.position.let { ReadingPosition(it.unit + 1) })
    fun previous() = navigate(_state.value.position.let { ReadingPosition(it.unit - 1) })

    fun reload() {
        val target = ticket ?: return
        if (_state.value.busy) return
        if (_state.value.book == null) {
            val id = target.exportId
            close(save = false, reopen = id)
            return
        }
        debounce?.cancel()
        val captured = version
        dirty = false // Explicit Reload discards unsaved local position/preferences.
        _state.value = _state.value.copy(loading = true)
        scope.launch {
            try {
                val result = withContext(io) { loaded(target, request(target, "reader_reload")) }
                if (current(target, captured)) apply(result.first, result.second)
                else if (ticket == target && version == captured) contextChanged(currentRoot())
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (failure: Exception) {
                if (ticket == target && version == captured) _state.value = _state.value.copy(loading = false, error = error(failure))
            }
        }
    }

    fun close(save: Boolean = true, reason: String? = null, reopen: String? = null) {
        val target = ticket ?: return
        if (_state.value.closing) return
        debounce?.cancel()
        val snapshot = _state.value
        val shouldSave = save && dirty && snapshot.book != null && snapshot.document != null
        val captured = ++version
        // Return navigation immediately. Retirement and a final save can wait on
        // archive I/O; that wait must never hide the downloader's Pause control.
        _state.value = LocalReaderState(closing = true, feedback = "Closing reader…")
        scope.launch {
            var warning = reason ?: snapshot.book?.warning
            try {
                withContext(io) {
                    try { if (shouldSave) warning = saveRequest(target, snapshot).warning ?: reason }
                    finally {
                        request(target, "reader_close")
                        if (reopen == null) target.selected?.discard()
                    }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) { warning = reason ?: "Reading progress could not be saved. The original file is unchanged." }
            if (ticket == target && version == captured) {
                ticket = null; dirty = false
                _state.value = LocalReaderState(feedback = warning)
                if (reopen != null) {
                    val selected = target.selected
                    if (selected == null) open(reopen)
                    else if (!openPrepared(selected)) selected.discard()
                }
            }
        }
    }
    fun contextChanged(root: File?) {
        if (ticket?.root?.absolutePath != root?.absolutePath && ticket != null)
            close(save = false, reason = "The working library changed. Choose Read here again.")
    }
    fun dismissFeedback() { if (!_state.value.open && !_state.value.closing) _state.value = LocalReaderState() }
    fun dispose() {
        debounce?.cancel()
        val target = ticket
        version++; ticket = null
        _state.value = LocalReaderState()
        if (target != null) executor.execute {
            try { request(target, "reader_close"); target.selected?.discard() }
            catch (_: Exception) { /* Keep actual copy when native close is unknown. */ }
        }
        executor.shutdown()
    }
}
