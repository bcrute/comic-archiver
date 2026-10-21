package io.github.bcrute.comicarchiver.export

/**
 * Which finished-archive format an entry is. Mirrors the two formats the existing core
 * already writes (comic episodes as CBZ, novel chapters as EPUB); the beta adds no new
 * format and never rewrites these files.
 */
enum class ExportKind(val extension: String, val mimeType: String) {
    CBZ("cbz", "application/vnd.comicbook+zip"),
    EPUB("epub", "application/epub+zip");

    companion object {
        /** Derives the kind from a manifest filename; null when the name is neither format. */
        fun fromFilename(name: String): ExportKind? =
            entries.firstOrNull { name.endsWith(".${it.extension}", ignoreCase = true) }
    }
}

/**
 * One finished archive from the controller's export manifest. Field-for-field the frozen
 * bridge manifest entry `{seriesId, episodeId, kind, filename, bytes, sha256, relpath}`
 * (docs/android-beta-contracts.md section 5); the native line maps the bridge JSON onto
 * this class.
 *
 * `relpath` is the app-private location of the original. It is never written under the
 * user's tree, never shown in UI copy, and never persisted anywhere public.
 */
data class ExportEntry(
    val seriesId: String,
    val episodeId: String,
    val kind: ExportKind,
    val filename: String,
    val bytes: Long,
    val sha256: String,
    val relpath: String,
) {
    init {
        require(seriesId.isNotEmpty()) { "seriesId must not be empty" }
        require(episodeId.isNotEmpty()) { "episodeId must not be empty" }
        require(bytes >= 0L) { "bytes must not be negative" }
        require(SHA256_HEX.matches(sha256)) { "sha256 must be 64 hex characters" }
    }

    companion object {
        private val SHA256_HEX = Regex("^[0-9a-fA-F]{64}$")
    }
}

/**
 * The user's chosen export tree: the document URI returned by one
 * ACTION_OPEN_DOCUMENT_TREE selection, whose persistable grant the caller (app assembly,
 * native line) has already taken. Held as a string: a content URI is opaque and must
 * never be treated as a POSIX path.
 */
class SafExportTarget(val treeUriString: String) {
    init {
        require(treeUriString.isNotEmpty()) { "treeUriString must not be empty" }
    }
}
