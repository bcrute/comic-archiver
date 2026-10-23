package io.github.bcrute.comicarchiver.export

import java.io.File
import java.io.IOException

/** Lifecycle of one export entry, as recorded in the journal. */
enum class JournalState { PENDING, COPYING, VERIFIED, FAILED }

/**
 * Why an entry is not verified. These are internal codes; the UI layer maps each to
 * exactly one public issue code and one recovery action (see io.github.bcrute.comicarchiver
 * .ui.NoticeText). None is ever reported as success.
 */
enum class ExportFailure {
    /** The copied bytes' size or digest did not match the manifest. */
    VERIFICATION_MISMATCH,

    /** The app-private original could not be opened or read. */
    SOURCE_REFUSED,

    /** The provider refused to create or open the target document. */
    TARGET_REFUSED,

    /** A write into the tree failed; SAF exposes no quota introspection, so any write
     *  failure (usually low space) surfaces here. */
    STORAGE_LOW,

    /** The persisted tree grant was revoked; the user must pick the folder again. */
    GRANT_LOST,

    /** The run was interrupted (user stop, service timeout, process death) mid-copy. */
    INTERRUPTED,

    /** The manifest entry itself was unusable (bad name, mismatched format). */
    MANIFEST_INVALID,
}

/**
 * Journal record for one archive. `targetUri`/`actualName` are set once the provider
 * created the document, so a resumed run reuses the same target instead of accreting
 * duplicates; `requestedName` is kept so collisions stay visible.
 */
data class JournalEntry(
    val episodeId: String,
    val seriesId: String,
    val kind: ExportKind,
    val requestedName: String,
    val actualName: String,
    val bytes: Long,
    val sha256: String,
    val state: JournalState,
    val targetUri: String?,
    val attempt: Int,
    val failure: ExportFailure?,
    val targetTreeUri: String? = null,
)

/**
 * The recoverable journal: one entry per series/episode/format/target tree, persisted after
 * every state change so interruption at any point loses at most the in-flight file, which
 * restarts from scratch on the next run. Lives only in app-private storage.
 */
class ExportJournal private constructor(private val entries: LinkedHashMap<List<String?>, JournalEntry>) {

    /** Legacy convenience for a unique episode; ambiguous identities never choose a copy. */
    fun get(episodeId: String): JournalEntry? = entries.values.filter { it.episodeId == episodeId }.singleOrNull()

    fun find(entry: ExportEntry, tree: String): JournalEntry? =
        entries[listOf(entry.seriesId, entry.episodeId, entry.kind.name, tree)] ?:
            entries[listOf(entry.seriesId, entry.episodeId, entry.kind.name, null)]

    fun all(): List<JournalEntry> = entries.values.toList()

    fun record(entry: JournalEntry) {
        if (entry.targetTreeUri != null) {
            val legacyKey = key(entry.copy(targetTreeUri = null))
            val legacy = entries[legacyKey]
            // A first export into another tree must not retire a still-usable v1
            // copy. Adopt only the same document, or a legacy line with no copy.
            if (legacy?.targetUri == null || legacy.targetUri == entry.targetUri) entries.remove(legacyKey)
        }
        entries[key(entry)] = entry
    }

    companion object {
        fun empty(): ExportJournal = ExportJournal(LinkedHashMap())

        fun of(records: List<JournalEntry>): ExportJournal =
            empty().apply { records.forEach(::record) }

        private fun key(entry: JournalEntry) =
            listOf(entry.seriesId, entry.episodeId, entry.kind.name, entry.targetTreeUri)
    }
}

/** Persistence for [ExportJournal]; app-private, never exported. */
interface JournalStore {
    fun load(): ExportJournal
    fun save(journal: ExportJournal)
}

/**
 * Line-based file journal. The format is internal and private (never a stored format
 * shared with the desktop core), versioned by its header so a future change can migrate
 * instead of guessing:
 *
 * ```
 * comicarchiver-export-journal v2
 * episodeId|seriesId|kind|requestedName|actualName|bytes|sha256|state|targetUri|attempt|failure|targetTreeUri
 * ```
 *
 * Fields are escaped (`\` -> `\\`, `|` -> `\p`, newline -> `\n`); unparseable lines are
 * skipped rather than aborting the journal, since the worst case is re-exporting a file.
 * v1 lines remain readable with an unknown target tree; the exporter verifies their
 * actual tree membership and bytes before adoption. Saves use temp file + rename;
 * a failed replacement preserves the previous journal.
 */
class FileJournalStore(private val file: File) : JournalStore {

    override fun load(): ExportJournal {
        val lines = try {
            file.readLines()
        } catch (_: IOException) {
            return ExportJournal.empty()
        }
        if (lines.isEmpty() || lines[0] !in setOf(HEADER, LEGACY_HEADER)) return ExportJournal.empty()
        val entries = ArrayList<JournalEntry>()
        for (line in lines.drop(1)) {
            parseEntry(line, lines[0] == LEGACY_HEADER)?.let(entries::add)
        }
        return ExportJournal.of(entries)
    }

    override fun save(journal: ExportJournal) {
        val dir = file.parentFile
        if (dir != null && !dir.exists()) dir.mkdirs()
        val tmp = File(dir, file.name + ".tmp")
        val body = StringBuilder()
        for (e in journal.all()) {
            body.append(escape(e.episodeId)).append(SEP)
                .append(escape(e.seriesId)).append(SEP)
                .append(e.kind.name).append(SEP)
                .append(escape(e.requestedName)).append(SEP)
                .append(escape(e.actualName)).append(SEP)
                .append(e.bytes).append(SEP)
                .append(e.sha256).append(SEP)
                .append(e.state.name).append(SEP)
                .append(e.targetUri?.let(::escape) ?: "").append(SEP)
                .append(e.attempt).append(SEP)
                .append(e.failure?.name ?: "")
                .append(SEP).append(e.targetTreeUri?.let(::escape) ?: "")
                .append('\n')
        }
        tmp.writeText(HEADER + "\n" + body)
        if (!tmp.renameTo(file)) {
            tmp.delete()
            throw IOException("export journal could not be saved")
        }
    }

    private fun parseEntry(line: String, legacy: Boolean): JournalEntry? {
        val fields = split(line) ?: return null
        if (fields.size != if (legacy) 11 else 12) return null
        return try {
            val kind = ExportKind.valueOf(fields[2])
            val state = JournalState.valueOf(fields[7])
            val failure = fields[10].takeIf { it.isNotEmpty() }?.let(ExportFailure::valueOf)
            val bytes = fields[5].toLong()
            val attempt = fields[9].toInt()
            require(bytes >= 0 && attempt >= 0 && fields[6].matches(Regex("[a-fA-F0-9]{64}")))
            JournalEntry(
                episodeId = fields[0],
                seriesId = fields[1],
                kind = kind,
                requestedName = fields[3],
                actualName = fields[4],
                bytes = bytes,
                sha256 = fields[6],
                state = state,
                targetUri = fields[8].takeIf { it.isNotEmpty() },
                attempt = attempt,
                failure = failure,
                targetTreeUri = if (legacy) null else fields[11].takeIf { it.isNotEmpty() },
            )
        } catch (_: IllegalArgumentException) {
            null
        }
    }

    private fun split(line: String): List<String>? {
        val out = ArrayList<String>(11)
        val sb = StringBuilder()
        var i = 0
        while (i < line.length) {
            val c = line[i]
            when {
                c == '\\' -> {
                    if (i + 1 >= line.length) return null
                    when (val next = line[i + 1]) {
                        '\\' -> sb.append('\\')
                        'p' -> sb.append('|')
                        'n' -> sb.append('\n')
                        else -> return null
                    }
                    i += 2
                }
                c == '|' -> {
                    out.add(sb.toString())
                    sb.setLength(0)
                    i += 1
                }
                else -> {
                    sb.append(c)
                    i += 1
                }
            }
        }
        out.add(sb.toString())
        return out
    }

    private fun escape(raw: String): String = raw
        .replace("\\", "\\\\")
        .replace("|", "\\p")
        .replace("\n", "\\n")

    companion object {
        private const val HEADER = "comicarchiver-export-journal v2"
        private const val LEGACY_HEADER = "comicarchiver-export-journal v1"
        private const val SEP = '|'
    }
}
