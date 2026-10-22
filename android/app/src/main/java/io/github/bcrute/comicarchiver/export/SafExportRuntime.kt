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
 * (the activity's lifecycle scope at assembly); [stopExport] is safe from any thread.
 */
class SafExportRuntime(
    context: Context,
    private val bridge: PythonBridge,
    workspaceRoot: File,
    private val scope: CoroutineScope,
) {
    private val appContext = context.applicationContext
    private val libraryRoot = File(workspaceRoot, LIBRARY_DIR)
    private val exporter = SafBatchExporter(
        source = ::openOriginal,
        documents = ContentResolverDocumentsAdapter(appContext),
        store = FileJournalStore(File(appContext.filesDir, "export/journal-v1.txt")),
    )
    private val preferences = appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // initialPanel restores this value; keep the initialization order explicit so a
    // later initializer change cannot overwrite that restored folder.
    private var treeUri: Uri? = null

    private val _panel = kotlinx.coroutines.flow.MutableStateFlow(initialPanel())
    val panel: kotlinx.coroutines.flow.StateFlow<ExportPanelStateUi> = _panel

    /**
     * The folder-picker result: the one persistable grant the beta uses. A grant the
     * system refuses to persist is reported as the re-pick state, not silently dropped.
     */
    fun onFolderGranted(uri: Uri) {
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
        _panel.value = ExportPanelStateUi(ExportPhaseUi.RUNNING)
        scope.launch(Dispatchers.IO) {
            val parsed = try {
                ExportManifestParser.parse(
                    bridge.dispatch(OP_MANIFEST, JSONObject().put("root", workspaceRootArg).toString()),
                )
            } catch (error: Exception) {
                _panel.value = refusedPanel(PublicIssue.JOB_FAILED)
                return@launch
            }
            if (parsed.refused) {
                _panel.value = refusedPanel(exportIssueFromWireCode(parsed.refusedCode!!))
                return@launch
            }
            if (parsed.invalid > 0) {
                // An incomplete manifest cannot certify that every archive exported.
                // Keep originals and require a fresh, usable manifest before copying.
                _panel.value = refusedPanel(PublicIssue.JOB_FAILED)
                return@launch
            }
            if (parsed.entries.isEmpty() && parsed.invalid == 0) {
                // Nothing finished yet: the honest state is a ready panel, not success.
                _panel.value = ExportPanelStateUi(ExportPhaseUi.READY)
                return@launch
            }
            val summary = exporter.exportBatch(SafExportTarget(uri.toString()), parsed.entries)
            _panel.value = ExportPanelStateUi.fromSummary(summary, parsed.entries.size)
        }
    }

    /** Cooperative stop; the running batch then reports itself interrupted, never complete. */
    fun stopExport() {
        exporter.requestStop()
    }

    /** Manifest entries point under the app-private Library only; anything else is refused. */
    private fun openOriginal(entry: ExportEntry): InputStream {
        val resolved = resolveOriginal(libraryRoot, entry.relpath)
            ?: throw SourceRefusedException("original refused")
        return FileInputStream(resolved)
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
            if (relpath.isBlank() || relpath.startsWith("/")) return null
            val root = libraryRoot.canonicalFile
            val target = File(root, relpath).canonicalFile
            if (target == root || !target.path.startsWith(root.path + File.separator)) return null
            return target.takeIf { it.isFile }
        }
    }
}
