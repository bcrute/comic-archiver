package io.github.bcrute.comicarchiver.export

import java.io.IOException
import java.io.InputStream

/**
 * Result of one batch run. `interrupted` and `grantLost` are recoverable: the journal
 * holds enough to resume, and the UI asks the user to re-pick the folder or re-run.
 * A partial batch is never reported as complete: `complete` is true only when every
 * entry is VERIFIED or was skipped as already verified, with no failures.
 */
data class ExportSummary(
    val verified: Int,
    val failed: Int,
    val alreadyVerified: Int,
    val failures: List<JournalEntry>,
    val interrupted: Boolean,
    val grantLost: Boolean,
) {
    val complete: Boolean get() = !interrupted && !grantLost && failed == 0
}

/**
 * SAF batch exporter: copies finished CBZ/EPUB archives from the app-private library into
 * per-series folders under one ACTION_OPEN_DOCUMENT_TREE tree, verifying size and SHA-256
 * of every copy before recording it VERIFIED.
 *
 * Invariants (see the beta contract, section 5, and the roadmap's Block 5):
 * - The app-private original is never modified or deleted; export is copy-only.
 * - Nothing but the archive files named in the manifest is ever written to the tree —
 *   no credentials, provenance, queue data or private metadata.
 * - A file is VERIFIED only after its copy's size AND digest match the manifest; a
 *   partial copy is left unverified (and best-effort deleted), never called complete.
 * - Interruption at any point is recoverable: the journal is saved after every entry
 *   state change, and an interrupted in-flight file restarts from scratch on resume,
 *   reusing its recorded target document so retries do not accrete duplicates.
 * - A revoked tree grant stops the batch with a recoverable GRANT_LOST summary; the
 *   next run with a fresh pick continues from the journal.
 * - No POSIX assumptions: targets are document URIs, never paths; name collisions are
 *   handled by recording the provider's actual display name, never by overwriting.
 *
 * This class is JVM-pure and synchronous; the caller (native line's app assembly) owns
 * threading, lifecycle and progress notification. Cooperative stop: call [requestStop]
 * from any thread; the run finishes the current chunk check and returns with
 * `interrupted = true`.
 */
class SafBatchExporter(
    private val source: ExportContentSource,
    private val documents: DocumentsAdapter,
    private val store: JournalStore,
) {
    @Volatile
    private var stopRequested = false

    fun requestStop() {
        stopRequested = true
    }

    private var activeTarget = ""

    @Synchronized
    fun exportBatch(target: SafExportTarget, entries: List<ExportEntry>,
                    shouldStop: () -> Boolean = { false },
                    onProgress: (ExportSummary) -> Unit = {}): ExportSummary {
        activeTarget = target.treeUriString
        val stopped = { stopRequested || shouldStop() }
        stopRequested = false
        val journal = store.load()

        val root = try {
            documents.rootFolder(target)
        } catch (e: DocumentsException) {
            if (e.code == DocumentsCode.GRANT_LOST) {
                return failAllGrantLost(journal, entries)
            }
            return failAll(journal, entries, ExportFailure.TARGET_REFUSED)
        }

        var verified = 0
        var already = 0
        var interrupted = false
        val failures = ArrayList<JournalEntry>()
        val seriesFolders = HashMap<String, DocumentsFolder>()

        for (entry in entries) {
            if (stopped()) {
                interrupted = true
                break
            }
            val previous = journal.find(entry, activeTarget)?.takeIf { record ->
                record.targetUri?.let { documents.belongsToTree(DocumentsFile(it), target) }
                    ?: (record.targetTreeUri == activeTarget)
            }
            if (previous != null &&
                previous.state == JournalState.VERIFIED &&
                previous.bytes == entry.bytes &&
                previous.sha256.equals(entry.sha256, ignoreCase = true) &&
                recordedCopyMatches(previous, entry, stopped)
            ) {
                // Only skip when the verified copy is of the archive the manifest now
                // describes AND that copy is still in the tree; a re-saved episode (new
                // bytes/digest) or a user-deleted document is exported again.
                if (previous.targetTreeUri == null) {
                    journal.record(previous.copy(targetTreeUri = activeTarget))
                    store.save(journal)
                }
                already++
                onProgress(ExportSummary(verified, failures.size, already, failures.toList(), false, false))
                continue
            }
            if (stopped()) { interrupted = true; break }
            when (val outcome = exportOne(root, entry, previous, journal, seriesFolders, stopped)) {
                OneResult.Verified -> verified++
                OneResult.GrantLost -> {
                    store.save(journal)
                    return ExportSummary(
                        verified = verified,
                        failed = failures.size,
                        alreadyVerified = already,
                        failures = failures,
                        interrupted = true,
                        grantLost = true,
                    )
                }
                OneResult.Stopped -> {
                    interrupted = true
                    break
                }
                is OneResult.FailedResult -> {
                    failures.add(outcome.entry)
                    if (outcome.entry.failure == ExportFailure.INTERRUPTED) {
                        interrupted = true
                        break
                    }
                }
            }
            onProgress(ExportSummary(verified, failures.size, already, failures.toList(), interrupted, false))
        }
        store.save(journal)
        return ExportSummary(
            verified = verified,
            failed = failures.size,
            alreadyVerified = already,
            failures = failures,
            interrupted = interrupted,
            grantLost = false,
        )
    }

    private sealed class OneResult {
        object Verified : OneResult()
        object GrantLost : OneResult()
        object Stopped : OneResult()
        data class FailedResult(val entry: JournalEntry) : OneResult()
    }

    /**
     * A VERIFIED journal line is only trustworthy while the actual selected-tree bytes
     * still match. A changed document is rewritten, and a missing/stale document follows
     * the normal create path. Metadata alone never certifies an old copy.
     */
    private fun recordedCopyMatches(previous: JournalEntry, entry: ExportEntry,
                                    stopped: () -> Boolean): Boolean {
        val uri = previous.targetUri ?: return false
        return try {
            val digest = documents.openInput(DocumentsFile(uri)).use { StreamDigest.digestOnly(it, stopped) }
            digest is StreamDigest.Result.Done && digest.bytes == entry.bytes &&
                digest.sha256Hex.equals(entry.sha256, ignoreCase = true)
        } catch (_: DocumentsException) { false }
          catch (_: IOException) { false }
    }

    private fun exportOne(
        root: DocumentsFolder,
        entry: ExportEntry,
        previous: JournalEntry?,
        journal: ExportJournal,
        seriesFolders: MutableMap<String, DocumentsFolder>,
        stopped: () -> Boolean,
    ): OneResult {
        val requestedName = SafeName.of(entry.filename, fallbackName(entry))
        if (ExportKind.fromFilename(requestedName) != entry.kind) {
            return failed(journal, entry, previous, requestedName, targetUri = previous?.targetUri,
                failure = ExportFailure.MANIFEST_INVALID)
        }

        val target: DocumentsFile
        val actualName: String
        when (val acquired = acquireTarget(root, entry, previous, requestedName, seriesFolders)) {
            is TargetResult.Granted -> {
                target = acquired.target
                actualName = acquired.actualName
            }
            TargetResult.GrantLost -> return OneResult.GrantLost
            is TargetResult.Refused ->
                return failed(journal, entry, previous, requestedName, acquired.targetUri,
                    ExportFailure.TARGET_REFUSED)
        }

        // Journal the COPYING state (with the target) before any byte moves, so a crash
        // mid-copy resumes onto the same document.
        journal.record(
            journalEntry(entry, requestedName, actualName, JournalState.COPYING,
                target.uriString, (previous?.attempt ?: 0) + 1, failure = null)
        )
        store.save(journal)

        val copy = try {
            copyVerified(entry, target, stopped)
        } catch (e: DocumentsException) {
            if (e.code == DocumentsCode.GRANT_LOST) return OneResult.GrantLost
            return failed(journal, entry, previous, requestedName, target.uriString,
                if (e.code == DocumentsCode.TARGET_REFUSED) ExportFailure.TARGET_REFUSED
                else ExportFailure.STORAGE_LOW)
        } catch (e: SourceRefusedException) {
            return failed(journal, entry, previous, requestedName, target.uriString,
                ExportFailure.SOURCE_REFUSED)
        } catch (e: IOException) {
            // SAF gives no quota introspection: any stream failure during the copy is
            // surfaced as storage_low, never as success and never as verified.
            return failed(journal, entry, previous, requestedName, target.uriString,
                ExportFailure.STORAGE_LOW)
        }

        return when (copy) {
            CopyOutcome.STOPPED ->
                failed(journal, entry, previous, requestedName, target.uriString,
                    ExportFailure.INTERRUPTED)
            is CopyOutcome.MISMATCH ->
                failed(journal, entry, previous, requestedName, target.uriString,
                    ExportFailure.VERIFICATION_MISMATCH, deleteTarget = target)
            CopyOutcome.DONE -> {
                journal.record(
                    journalEntry(entry, requestedName, actualName, JournalState.VERIFIED,
                        target.uriString, (previous?.attempt ?: 0) + 1, failure = null)
                )
                store.save(journal)
                OneResult.Verified
            }
        }
    }

    private sealed class TargetResult {
        data class Granted(val target: DocumentsFile, val actualName: String) : TargetResult()
        object GrantLost : TargetResult()
        data class Refused(val targetUri: String?) : TargetResult()
    }

    /**
     * Picks the document to write: a recorded target on resume (so retries never accrete
     * duplicates), otherwise a freshly created file in the series folder. When the
     * recorded document has disappeared (user deleted it), falls back to creating a
     * fresh one — a refusal of that shape is recoverable, not fatal.
     *
     * A recorded target can also live under a PREVIOUS tree whose grant was revoked
     * after the user re-picked a different folder: probing it raises GRANT_LOST even
     * though the tree being exported to now is fine. Such a probe failure therefore
     * falls through to a fresh create instead of aborting; a genuinely dead current
     * grant re-surfaces at ensureSubfolder/createFile below and aborts the batch there.
     *
     * Known, accepted window: a crash between createFile and the COPYING journal save
     * leaves an orphan document. The next run cannot know its URI, so it creates a
     * sibling; the provider's unique rename (recorded in the journal) keeps both files
     * intact rather than risking an overwrite of a document we cannot identify.
     */
    private fun acquireTarget(
        root: DocumentsFolder,
        entry: ExportEntry,
        previous: JournalEntry?,
        requestedName: String,
        seriesFolders: MutableMap<String, DocumentsFolder>,
    ): TargetResult {
        if (previous?.targetUri != null) {
            val recorded = DocumentsFile(previous.targetUri)
            try {
                return TargetResult.Granted(recorded, documents.displayNameFor(recorded))
            } catch (e: DocumentsException) {
                when (e.code) {
                    // Stale target (deleted, or under a revoked previous tree): the
                    // current grant is checked by the create path below.
                    DocumentsCode.NOT_FOUND, DocumentsCode.GRANT_LOST -> Unit
                    else -> return TargetResult.Refused(previous.targetUri)
                }
            }
        }
        val folder = try {
            seriesFolders.getOrPut(entry.seriesId) {
                documents.ensureSubfolder(root, SafeName.of(entry.seriesId, "series"))
            }
        } catch (e: DocumentsException) {
            if (e.code == DocumentsCode.GRANT_LOST) return TargetResult.GrantLost
            return TargetResult.Refused(null)
        }
        val created = try {
            documents.createFile(folder, requestedName, entry.kind.mimeType)
        } catch (e: DocumentsException) {
            if (e.code == DocumentsCode.GRANT_LOST) return TargetResult.GrantLost
            return TargetResult.Refused(null)
        }
        val actualName = try {
            documents.displayNameFor(created)
        } catch (_: DocumentsException) {
            requestedName
        }
        return TargetResult.Granted(created, actualName)
    }

    private sealed class CopyOutcome {
        object DONE : CopyOutcome()
        object STOPPED : CopyOutcome()
        object MISMATCH : CopyOutcome()
    }

    /**
     * Copies the original to [target] and verifies the copy by re-reading it through the
     * provider: size (provider-reported when available, else bytes read) and SHA-256 must
     * both match the manifest. The original is only read, never written or deleted.
     */
    private fun copyVerified(entry: ExportEntry, target: DocumentsFile, stopped: () -> Boolean): CopyOutcome {
        source.openStream(entry).use { input ->
            documents.openOutput(target).use { output ->
                when (StreamDigest.copyAndDigest(input, output) { stopped() }) {
                    StreamDigest.Result.Stopped -> return CopyOutcome.STOPPED
                    is StreamDigest.Result.Done -> Unit
                }
            }
        }
        val written = when (val verify = documents.openInput(target).use { verifyStream ->
            StreamDigest.digestOnly(verifyStream) { stopped() }
        }) {
            StreamDigest.Result.Stopped -> return CopyOutcome.STOPPED
            is StreamDigest.Result.Done -> verify
        }
        val providerSize = try {
            documents.sizeBytes(target)
        } catch (_: DocumentsException) {
            -1L
        }
        val sizeOk = written.bytes == entry.bytes && (providerSize < 0 || providerSize == entry.bytes)
        val digestOk = written.sha256Hex.equals(entry.sha256, ignoreCase = true)
        return if (sizeOk && digestOk) CopyOutcome.DONE else CopyOutcome.MISMATCH
    }

    private fun failed(
        journal: ExportJournal,
        entry: ExportEntry,
        previous: JournalEntry?,
        requestedName: String,
        targetUri: String?,
        failure: ExportFailure,
        deleteTarget: DocumentsFile? = null,
        actualName: String = previous?.actualName ?: requestedName,
    ): OneResult.FailedResult {
        if (deleteTarget != null) documents.delete(deleteTarget)
        val recorded = journalEntry(entry, requestedName, actualName, JournalState.FAILED,
            targetUri, (previous?.attempt ?: 0) + 1, failure)
        journal.record(recorded)
        return OneResult.FailedResult(recorded)
    }

    private fun journalEntry(
        entry: ExportEntry,
        requestedName: String,
        actualName: String,
        state: JournalState,
        targetUri: String?,
        attempt: Int,
        failure: ExportFailure?,
    ) = JournalEntry(
        episodeId = entry.episodeId,
        seriesId = entry.seriesId,
        kind = entry.kind,
        requestedName = requestedName,
        actualName = actualName,
        bytes = entry.bytes,
        sha256 = entry.sha256,
        state = state,
        targetUri = targetUri,
        attempt = attempt,
        failure = failure,
        targetTreeUri = activeTarget,
    )

    private fun fallbackName(entry: ExportEntry): String =
        "${entry.episodeId}.${entry.kind.extension}"

    private fun failAllGrantLost(
        journal: ExportJournal,
        entries: List<ExportEntry>,
    ): ExportSummary = failAll(journal, entries, ExportFailure.GRANT_LOST)

    private fun failAll(
        journal: ExportJournal,
        entries: List<ExportEntry>,
        failure: ExportFailure,
    ): ExportSummary {
        val failures = ArrayList<JournalEntry>()
        for (entry in entries) {
            // Root access failed, so no legacy URI's membership was verified.
            val previous = journal.find(entry, activeTarget)?.takeIf { it.targetTreeUri == activeTarget }
            val recorded = journalEntry(
                entry,
                SafeName.of(entry.filename, fallbackName(entry)),
                previous?.actualName ?: SafeName.of(entry.filename, fallbackName(entry)),
                JournalState.FAILED,
                previous?.targetUri,
                (previous?.attempt ?: 0) + 1,
                failure,
            )
            journal.record(recorded)
            failures.add(recorded)
        }
        store.save(journal)
        return ExportSummary(
            verified = 0,
            failed = failures.size,
            alreadyVerified = 0,
            failures = failures,
            interrupted = false,
            grantLost = failure == ExportFailure.GRANT_LOST,
        )
    }
}
