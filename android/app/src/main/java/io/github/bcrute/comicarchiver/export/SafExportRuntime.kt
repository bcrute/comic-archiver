package io.github.bcrute.comicarchiver.export

import android.content.Context
import android.net.Uri
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.ui.ExportPanelStateUi
import io.github.bcrute.comicarchiver.ui.ExportPhaseUi
import io.github.bcrute.comicarchiver.ui.PublicIssue
import io.github.bcrute.comicarchiver.ui.exportIssueFromWireCode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.InputStream

/**
 * The export runtime coordinator (UI/export line): owns the one SAF target tree, the
 * manifest fetch and the [SafBatchExporter] run, and exposes the export panel's state as
 * a [kotlinx.coroutines.flow.StateFlow] for [io.github.bcrute.comicarchiver.ui.ComicArchiverAppRoot].
 *
 * Truthfulness rules it keeps: the folder is the user's single ACTION_OPEN_DOCUMENT_TREE
 * pick with a persisted grant (and the pick itself is persisted so a restart keeps the
 * folder); a refused, unimplemented or unreadable manifest surfaces as a public-safe
 * failure, never as an empty success; a run's outcome is exactly the exporter's summary —
 * partial and interrupted runs stay partial, and nothing is reported exported that the
 * journal does not say is verified. App-private originals are only ever read, through a
 * relpath confined to the workspace's Library folder.
 *
 * The bridge call and the exporter run execute on Dispatchers.IO through [scope]
 * (the application's process scope at assembly); [stopExport] is safe from any thread.
 */
class SafExportRuntime(
    context: Context,
    private val bridge: PythonBridge,
    workspaceRoot: File,
    private val scope: CoroutineScope,
    documents: DocumentsAdapter = ContentResolverDocumentsAdapter(context.applicationContext),
    siteKey: String = "tapas",
) {
    private val appContext = context.applicationContext
    private val siteIdentity = siteKey.also { require(it.matches(Regex("[a-z][a-z0-9_-]{0,31}"))) }
    private val libraryRoot = File(workspaceRoot, LIBRARY_DIR)
    private val exporter = SafBatchExporter(
        source = ::openOriginal,
        documents = documents,
        store = FileJournalStore(File(appContext.filesDir,
            if (siteIdentity == "tapas") "export/journal-v1.txt" else "export/$siteIdentity/journal-v1.txt")),
    )
    private val active = AtomicReference<AtomicBoolean?>(null)
    private val preferences = appContext.getSharedPreferences(
        if (siteIdentity == "tapas") PREFS else "${PREFS}_$siteIdentity", Context.MODE_PRIVATE)

    // initialPanel restores this value; keep the initialization order explicit so a
    // later initializer change cannot overwrite that restored folder.
    private var treeUri: Uri? = null

    private val _panel = kotlinx.coroutines.flow.MutableStateFlow(initialPanel())
    val panel: kotlinx.coroutines.flow.StateFlow<ExportPanelStateUi> = _panel
    val isActive: Boolean get() = active.get() != null

    /**
     * The folder-picker result: the one persistable grant the beta uses. A grant the
     * system refuses to persist is reported as the re-pick state, not silently dropped.
     */
    fun onFolderGranted(uri: Uri) {
        if (active.get() != null) return
        try {
            appContext.contentResolver.takePersistableUriPermission(
                uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (_: SecurityException) {
            _panel.value = ExportPanelStateUi(ExportPhaseUi.GRANT_LOST, lastIssue = PublicIssue.EXPORT_INTERRUPTED)
            return
        }
        treeUri = uri
        preferences.edit().putString(PREFS_TREE, uri.toString()).apply()
        _panel.value = ExportPanelStateUi(ExportPhaseUi.READY)
    }

    /** Starts (or resumes) one batch run; a run without a picked folder is a no-op pick hint. */
    fun startExport() {
        val uri = treeUri ?: run {
            _panel.value = ExportPanelStateUi(ExportPhaseUi.NOT_PICKED)
            return
        }
        val stop = AtomicBoolean(false)
        if (!active.compareAndSet(null, stop)) return
        _panel.value = ExportPanelStateUi(ExportPhaseUi.RUNNING)
        scope.launch(Dispatchers.IO) {
            try {
                val parsed = ExportManifestParser.parse(
                    bridge.dispatch(OP_MANIFEST, JSONObject().put("root", workspaceRootArg).toString()),
                )
                if (stop.get()) {
                    _panel.value = ExportPanelStateUi(ExportPhaseUi.INTERRUPTED,
                        lastIssue = PublicIssue.EXPORT_INTERRUPTED)
                    return@launch
                }
                if (parsed.refused || parsed.invalid > 0) {
                    _panel.value = refusedPanel(if (parsed.refused)
                        exportIssueFromWireCode(parsed.refusedCode!!) else PublicIssue.JOB_FAILED)
                    return@launch
                }
                if (parsed.entries.isEmpty()) {
                    _panel.value = ExportPanelStateUi(ExportPhaseUi.READY)
                    return@launch
                }
                val summary = exporter.exportBatch(SafExportTarget(uri.toString()), parsed.entries,
                    shouldStop = { stop.get() }, onProgress = { partial ->
                        _panel.value = ExportPanelStateUi.fromSummary(partial, parsed.entries.size)
                            .copy(phase = ExportPhaseUi.RUNNING)
                    })
                _panel.value = ExportPanelStateUi.fromSummary(summary, parsed.entries.size)
            } catch (cancelled: CancellationException) {
                _panel.value = ExportPanelStateUi(ExportPhaseUi.INTERRUPTED,
                    lastIssue = PublicIssue.EXPORT_INTERRUPTED)
                throw cancelled
            } catch (_: SecurityException) {
                _panel.value = ExportPanelStateUi(ExportPhaseUi.GRANT_LOST,
                    lastIssue = PublicIssue.EXPORT_INTERRUPTED)
            } catch (_: Exception) {
                _panel.value = refusedPanel(PublicIssue.JOB_FAILED)
            } finally {
                active.compareAndSet(stop, null)
            }
        }
    }

    /** Cooperative stop; the running batch then reports itself interrupted, never complete. */
    fun stopExport() {
        active.get()?.set(true)
        exporter.requestStop()
    }

    /** Manifest entries point under the app-private Library only; anything else is refused. */
    private fun openOriginal(entry: ExportEntry): InputStream {
        val resolved = resolveOriginal(libraryRoot, entry.relpath)
            ?: throw SourceRefusedException("original refused")
        return try { FileInputStream(resolved) }
        catch (error: java.io.IOException) { throw SourceRefusedException("original refused", error) }
    }

    private fun initialPanel(): ExportPanelStateUi {
        val saved = preferences.getString(PREFS_TREE, null)
        return if (saved != null) {
            treeUri = Uri.parse(saved)
            ExportPanelStateUi(ExportPhaseUi.READY)
        } else {
            ExportPanelStateUi(ExportPhaseUi.NOT_PICKED)
        }
    }

    private fun refusedPanel(issue: PublicIssue): ExportPanelStateUi =
        ExportPanelStateUi(ExportPhaseUi.FINISHED_WITH_FAILURES, lastIssue = issue)

    private val workspaceRootArg: String get() = libraryRoot.parentFile?.absolutePath ?: libraryRoot.absolutePath

    companion object {
        private const val OP_MANIFEST = "export_manifest"
        private const val PREFS = "comicarchiver_export"
        private const val PREFS_TREE = "tree_uri"
        private const val LIBRARY_DIR = "Library"

        /**
         * Resolves a manifest relpath under [libraryRoot], refusing any path that would
         * escape it (absolute paths, `..`, symlinked targets). Public-safe: returns null
         * on refusal; the caller reports a refused original, never the path itself.
         */
        internal fun resolveOriginal(libraryRoot: File, relpath: String): File? {
            return io.github.bcrute.comicarchiver.library.resolveArchive(libraryRoot, relpath)
        }
    }
}
