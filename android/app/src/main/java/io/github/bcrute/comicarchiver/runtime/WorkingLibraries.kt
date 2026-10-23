package io.github.bcrute.comicarchiver.runtime

import android.net.Uri
import android.os.SystemClock
import io.github.bcrute.comicarchiver.App
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

enum class WorkingLibraryPhase { IDLE, CHOOSING, SCANNING, PREVIEW, CHECKING_COPY, COPYING,
    CHECKING_SOURCE, VERIFYING, SELECTING, STOPPING, HELD, FAILED }

data class WorkingLibraryChoice(val id: String, val label: String, val current: Boolean,
    val available: Boolean, val hasSource: Boolean)
data class WorkingLibraryPreview(val label: String, val files: Int, val knownBytes: Long,
    val unknownSizes: Int, val empty: Boolean, val existingCopy: Boolean, val refreshedCopy: Boolean)
data class WorkingLibrariesState(val enabled: Boolean = false, val loading: Boolean = false,
    val phase: WorkingLibraryPhase = WorkingLibraryPhase.IDLE, val choices: List<WorkingLibraryChoice> = emptyList(),
    val preview: WorkingLibraryPreview? = null, val pending: Boolean = false, val pendingLabel: String? = null,
    val filesDone: Int = 0, val filesTotal: Int = 0, val bytesRead: Long = 0, val knownBytes: Long = 0,
    val unknownSizes: Int = 0, val message: String? = null) {
    val busy: Boolean get() = phase !in setOf(WorkingLibraryPhase.IDLE, WorkingLibraryPhase.HELD, WorkingLibraryPhase.FAILED)
}

/** Process-owned location transaction. Private authorities never enter its renderable state. */
internal class WorkingLibraries(private val app: App, private val scope: CoroutineScope,
    private val documents: WorkingLibraryDocuments = app.createWorkingLibraryDocuments(),
    private val preparation: WorkingLibraryPreparation = app.createWorkingLibraryPreparation()) {
    private val locations get() = app.libraryLocations
    private val capture by lazy { WorkingLibraryCapture(documents, app.signInSites.mapValues { it.value.workspaceFolder }) }
    private val _state = MutableStateFlow(WorkingLibrariesState(enabled = true))
    val state: StateFlow<WorkingLibrariesState> = _state
    private var running: Job? = null
    private var generation = 0L
    private var picker: String? = null
    private var expectedScope: String? = null
    private var leased = false
    private var pending: PendingWorkingLibrary? = null
    private var preview: WorkingCapturePlan? = null
    private var refreshing = false
    private var cancellation: LibraryCaptureCancellation? = null
    @Volatile private var discardRequested = false

    private data class Choices(val entries: List<WorkingLibraryChoice>, val pending: PendingWorkingLibrary?)
    private fun readChoices(): Choices {
        val selected = locations.activeId()
        return Choices(locations.libraries().map { choice -> WorkingLibraryChoice(choice.id, choice.label,
            choice.id == selected, runCatching { locations.root(choice.id) }.isSuccess, choice.source != null) }, locations.pending())
    }
    private fun scopeIdentity() = app.controller.state.value.let { "${it.libraryId}/${it.site}" }
    private fun lease(): Boolean {
        if (leased) return true
        if (!app.controller.reserveLibraryChange()) return false
        leased = true; expectedScope = scopeIdentity()
        return true
    }
    private fun release() {
        if (leased) app.controller.releaseLibraryChange()
        leased = false; expectedScope = null
    }
    private fun current(operation: Long) = operation == generation && expectedScope == scopeIdentity()
    private fun words(error: Exception): String = when (error) {
        is LibraryCaptureException -> error.failure.words
        is WorkingLibraryException -> error.failure.words
        else -> "The working library could not be prepared. The previous library is retained. Retry or discard this copy."
    }

    fun load() {
        if (leased || running?.isActive == true) return
        val operation = ++generation
        _state.value = _state.value.copy(loading = true)
        running = scope.launch(Dispatchers.IO) {
            try {
                val choices = readChoices()
                withContext(Dispatchers.Main) {
                    if (operation != generation) return@withContext
                    running = null
                    pending = choices.pending
                    _state.value = WorkingLibrariesState(enabled = true, choices = choices.entries,
                        phase = if (pending == null) WorkingLibraryPhase.IDLE else WorkingLibraryPhase.HELD,
                        pending = pending != null, pendingLabel = pending?.label,
                        message = if (pending == null) null else "Library copy held. Continue or discard it; nothing starts by itself.")
                }
            } catch (error: Exception) {
                withContext(Dispatchers.Main) { if (operation == generation) _state.value = _state.value.copy(
                    loading = false, phase = WorkingLibraryPhase.FAILED, message = words(error)) }
            }
        }
    }

    /** The activity retains this opaque ticket through recreation; a cold process rejects an old result. */
    fun requestPicker(): String? {
        if (_state.value.loading || running?.isActive == true || _state.value.busy || !lease()) return null
        generation++; picker = UUID.randomUUID().toString(); preview = null; refreshing = false
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.CHOOSING, message = null, preview = null)
        return picker
    }

    fun picked(uri: Uri?, ticket: String) {
        if (ticket != picker || !leased || expectedScope != scopeIdentity()) return
        picker = null
        if (uri == null) { discardPreview(); return }
        scan(uri, false)
    }

    fun refreshSource() {
        if (running?.isActive == true || _state.value.busy || pending != null || !lease()) return
        val source = try { locations.libraries().singleOrNull { it.id == app.currentLibraryId }?.source }
            catch (_: Exception) { null }
        if (source == null) {
            release(); _state.value = _state.value.copy(message = "This library has no captured source. Choose a project or Library folder.")
            return
        }
        scan(Uri.parse(source.tree), true)
    }

    private fun scan(uri: Uri, refresh: Boolean) {
        val operation = ++generation
        val control = LibraryCaptureCancellation(); cancellation = control
        val site = app.controller.state.value.site
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.SCANNING, message = null, preview = null)
        running = scope.launch(Dispatchers.IO) {
            try {
                documents.retain(uri)
                val plan = capture.scan(uri, site, control)
                val source = WorkingLibraryCapture.source(plan.tree)
                if (pending != null && pending?.source?.key != source.key) throw WorkingLibraryException(WorkingLibraryFailure.STALE)
                val existing = locations.libraries().any { it.source?.key == source.key }
                withContext(Dispatchers.Main) {
                    if (!current(operation)) return@withContext
                    running = null
                    preview = plan; refreshing = refresh
                    _state.value = _state.value.copy(phase = WorkingLibraryPhase.PREVIEW,
                        preview = WorkingLibraryPreview(plan.label, plan.files.size, plan.knownBytes, plan.unknownSizes,
                            plan.files.isEmpty(), pending == null && existing, refresh))
                }
            } catch (error: Exception) {
                withContext(Dispatchers.Main) {
                    if (operation != generation) return@withContext
                    _state.value = _state.value.copy(phase = if (pending == null) WorkingLibraryPhase.FAILED else WorkingLibraryPhase.HELD,
                        message = words(error), preview = null)
                    release()
                }
            } finally { if (cancellation === control) cancellation = null }
        }
    }

    fun confirm(refreshedCopy: Boolean = refreshing) {
        if (_state.value.phase != WorkingLibraryPhase.PREVIEW || running?.isActive == true || !leased) return
        val plan = preview ?: return
        start(plan, refreshedCopy, false)
    }

    fun retry() {
        if (_state.value.busy || running?.isActive == true || pending == null || !lease()) return
        start(null, false, _state.value.phase == WorkingLibraryPhase.FAILED)
    }

    private fun start(plan: WorkingCapturePlan?, refresh: Boolean, rebuild: Boolean) {
        val operation = ++generation
        val control = LibraryCaptureCancellation(); cancellation = control; discardRequested = false
        preview = null
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.CHECKING_COPY, preview = null, message = null)
        running = scope.launch(Dispatchers.IO) {
            var resultMessage: String? = null
            var failed = false
            try {
                var ticket = locations.pending()
                var existing: WorkingLibraryLocation? = null
                if (ticket == null) {
                    val chosen = plan ?: throw WorkingLibraryException(WorkingLibraryFailure.STALE)
                    when (val started = locations.begin(chosen.label, WorkingLibraryCapture.source(chosen.tree), refresh)) {
                        is WorkingLibraryStart.Existing -> existing = started.library
                        is WorkingLibraryStart.Copy -> ticket = started.pending
                    }
                }
                val existingCopy = existing
                if (existingCopy != null) {
                    withContext(Dispatchers.Main) {
                        _state.value = _state.value.copy(phase = WorkingLibraryPhase.SELECTING)
                        if (!app.controller.selectWorkingLibrary(existingCopy.id)) resultMessage = "The saved copy could not be opened. The previous library is retained."
                    }
                } else {
                    val pendingCopy = ticket ?: throw WorkingLibraryException(WorkingLibraryFailure.STALE)
                    withContext(Dispatchers.Main) {
                        pending = pendingCopy
                        _state.value = _state.value.copy(pending = true, pendingLabel = pendingCopy.label)
                    }
                    val capturedRoot = locations.captureRoot(pendingCopy.id)
                    var payload = if (pendingCopy.digest == null) locations.copyRoot(pendingCopy.id) else locations.preparedRoot(pendingCopy.id)
                    if (rebuild && pendingCopy.digest == null) payload = locations.resetCopy(pendingCopy.id)
                    var proof = preparation.prepare(capturedRoot.resolve("workspace"), payload, pendingCopy, control, recover = true)
                    if (proof == null) {
                        if (pendingCopy.digest != null) throw WorkingLibraryException(WorkingLibraryFailure.NOT_READY)
                        val retained = if (capturedRoot.resolve("capture-v1.json").exists()) capture.recover(capturedRoot, pendingCopy.source, control) else null
                        val snapshot = retained ?: run {
                            val sourcePlan = plan ?: capture.scan(Uri.parse(pendingCopy.source.tree), app.controller.state.value.site, control)
                            var last = 0L
                            var phase: WorkingCapturePhase? = null
                            capture.capture(sourcePlan, capturedRoot, control) { progress ->
                                val now = SystemClock.elapsedRealtime()
                                if (progress.phase != phase || now - last >= 100) {
                                    last = now; phase = progress.phase
                                    scope.launch(Dispatchers.Main) {
                                        if (current(operation) && _state.value.phase in CAPTURE_PHASES) {
                                            _state.value = _state.value.copy(phase = when (progress.phase) {
                                                WorkingCapturePhase.CHECKING_COPY -> WorkingLibraryPhase.CHECKING_COPY
                                                WorkingCapturePhase.COPYING -> WorkingLibraryPhase.COPYING
                                                WorkingCapturePhase.CHECKING_SOURCE -> WorkingLibraryPhase.CHECKING_SOURCE
                                                WorkingCapturePhase.CAPTURED -> WorkingLibraryPhase.VERIFYING },
                                                filesDone = progress.filesDone, filesTotal = progress.filesTotal,
                                                bytesRead = progress.bytesRead, knownBytes = progress.knownBytes, unknownSizes = progress.unknownSizes)
                                        }
                                    }
                                }
                            }
                        }
                        withContext(Dispatchers.Main) { _state.value = _state.value.copy(phase = WorkingLibraryPhase.VERIFYING) }
                        payload = locations.resetCopy(pendingCopy.id)
                        proof = preparation.prepare(snapshot.workspace, payload, pendingCopy, control)
                            ?: throw WorkingLibraryException(WorkingLibraryFailure.NOT_READY)
                    }
                    val verifiedProof = proof ?: throw WorkingLibraryException(WorkingLibraryFailure.NOT_READY)
                    control.check()
                    if (pendingCopy.digest != null && pendingCopy.digest != verifiedProof.digest) throw WorkingLibraryException(WorkingLibraryFailure.STALE)
                    if (pendingCopy.digest == null) locations.verified(pendingCopy.id, verifiedProof.digest)
                    control.check()
                    val completed = locations.complete(pendingCopy.id)
                    control.check()
                    val currentSite = app.controller.state.value.site
                    val site = verifiedProof.sites.firstOrNull { it.key == currentSite }?.key ?: verifiedProof.sites.first().key
                    withContext(Dispatchers.Main) {
                        _state.value = _state.value.copy(phase = WorkingLibraryPhase.SELECTING)
                        if (!app.controller.selectWorkingLibrary(completed.id, site)) resultMessage =
                            "The verified copy is ready. The previous library remains selected; open the new copy when activity stops."
                    }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (error: Exception) {
                failed = error !is LibraryCaptureException || error.failure != LibraryCaptureFailure.CANCELLED
                resultMessage = if (!failed) "Library copy held. Continue or discard it; the previous library is retained." else words(error)
            } finally {
                if (discardRequested) {
                    try { locations.pending()?.let { locations.cancel(it.id) }; resultMessage = "Copy discarded. The previous library is retained." }
                    catch (error: Exception) { failed = true; resultMessage = words(error) }
                }
                val choices = runCatching { readChoices() }.getOrNull()
                withContext(NonCancellable + Dispatchers.Main) {
                    if (operation == generation) {
                        pending = choices?.pending
                        _state.value = _state.value.copy(phase = if (pending != null) {
                            if (failed) WorkingLibraryPhase.FAILED else WorkingLibraryPhase.HELD
                        } else if (failed) WorkingLibraryPhase.FAILED else WorkingLibraryPhase.IDLE,
                            choices = choices?.entries ?: _state.value.choices, pending = pending != null,
                            pendingLabel = pending?.label, preview = null, message = resultMessage)
                        release(); cancellation = null
                    }
                }
            }
        }
    }

    fun select(id: String) {
        if (_state.value.busy || _state.value.loading || pending != null || running?.isActive == true || !lease()) return
        val operation = ++generation
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.SELECTING, message = null)
        running = scope.launch {
            val opened = try { app.controller.selectWorkingLibrary(id) } catch (_: Exception) { false }
            val choices = withContext(Dispatchers.IO) { runCatching { readChoices() }.getOrNull() }
            if (operation == generation) {
                _state.value = _state.value.copy(phase = if (opened) WorkingLibraryPhase.IDLE else WorkingLibraryPhase.FAILED,
                    choices = choices?.entries ?: _state.value.choices,
                    message = if (opened) null else "The working library could not be opened. The previous library is retained.")
                release()
            }
        }
    }

    fun stop() {
        if (_state.value.phase == WorkingLibraryPhase.PREVIEW || _state.value.phase == WorkingLibraryPhase.CHOOSING) { discardPreview(); return }
        if (_state.value.phase == WorkingLibraryPhase.SELECTING) return
        val control = cancellation ?: return
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.STOPPING)
        scope.launch(Dispatchers.IO) { control.cancel() }
    }

    private fun discardPreview() {
        picker = null; preview = null; generation++
        _state.value = _state.value.copy(phase = if (pending == null) WorkingLibraryPhase.IDLE else WorkingLibraryPhase.HELD,
            preview = null, message = null)
        release()
    }

    fun discard() {
        if (_state.value.phase == WorkingLibraryPhase.SELECTING) return
        if (running?.isActive == true) { discardRequested = true; stop(); return }
        if (pending == null) { discardPreview(); return }
        if (!lease()) return
        val operation = ++generation
        _state.value = _state.value.copy(phase = WorkingLibraryPhase.STOPPING)
        running = scope.launch(Dispatchers.IO) {
            var message = "Copy discarded. The previous library is retained."
            try { locations.pending()?.let { locations.cancel(it.id) } } catch (error: Exception) { message = words(error) }
            val choices = runCatching { readChoices() }.getOrNull()
            withContext(Dispatchers.Main) {
                if (operation == generation) {
                    pending = choices?.pending
                    _state.value = _state.value.copy(phase = if (pending == null) WorkingLibraryPhase.IDLE else WorkingLibraryPhase.FAILED,
                        choices = choices?.entries ?: _state.value.choices, pending = pending != null,
                        pendingLabel = pending?.label, message = message)
                    release()
                }
            }
        }
    }

    companion object {
        private val CAPTURE_PHASES = setOf(WorkingLibraryPhase.CHECKING_COPY, WorkingLibraryPhase.COPYING,
            WorkingLibraryPhase.CHECKING_SOURCE, WorkingLibraryPhase.VERIFYING)
    }
}
