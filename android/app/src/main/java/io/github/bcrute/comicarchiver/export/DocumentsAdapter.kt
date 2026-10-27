package io.github.bcrute.comicarchiver.export

import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/**
 * A folder inside the user's SAF tree, identified by its document URI string. Opaque;
 * no POSIX semantics (no rename, no overwrite, no atomicity) may be assumed for it.
 */
@JvmInline
value class DocumentsFolder(val uriString: String)

/** A single document inside the user's SAF tree, identified by its document URI string. */
@JvmInline
value class DocumentsFile(val uriString: String)

/** Why a SAF operation failed; each maps to one recoverable public state in the UI. */
enum class DocumentsCode { GRANT_LOST, TARGET_REFUSED, NOT_FOUND, IO }

/** Failure of a SAF operation, with a code so callers never parse messages. */
class DocumentsException(
    val code: DocumentsCode,
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause)

/**
 * The app-private original for an export entry could not be opened. The original is kept;
 * nothing about the target tree is assumed.
 */
class SourceRefusedException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * Stream access to the app-private library originals. The native line implements this on
 * top of the controller's archive stream access; this seam exists so the exporter core is
 * JVM-pure and testable without Python or storage.
 */
fun interface ExportContentSource {
    /**
     * Opens the original for [entry]. Throw [SourceRefusedException] when the archive is
     * missing or unreadable; any other exception is treated as a refusal as well.
     */
    fun openStream(entry: ExportEntry): InputStream
}

/**
 * The only seam between the exporter and the SAF world: every capability the batch
 * exporter needs, expressed as documents operations, so the JVM-pure core can be tested
 * against a fake. The real implementation is [ContentResolverDocumentsAdapter].
 *
 * Implementations must throw [DocumentsException] with [DocumentsCode.GRANT_LOST] when the
 * persisted tree grant has been revoked, so the batch can stop and ask for a fresh pick.
 */
interface DocumentsAdapter {
    /** The root folder of the target tree; may reveal a revoked grant. */
    fun rootFolder(target: SafExportTarget): DocumentsFolder

    /** Legacy records are reusable only when their URI carries this exact tree grant.
     * Unknown membership fails closed; a fresh copy is safer than writing another tree. */
    fun belongsToTree(file: DocumentsFile, target: SafExportTarget): Boolean = false

    /**
     * Returns the named subfolder of [parent], creating it when absent. When a same-named
     * non-folder document exists, implementations may pick a provider-unique alternative
     * name and MUST surface that fact through the returned folder (callers re-query
     * names via [displayNameFor] when they need the exact name).
     */
    fun ensureSubfolder(parent: DocumentsFolder, displayName: String): DocumentsFolder

    /**
     * Creates a new document in [parent]. SAF never overwrites: providers may (and do)
     * return a document with a different display name on collision. Returns the created
     * document; the caller records the actual name it ended up with.
     */
    fun createFile(
        parent: DocumentsFolder,
        displayName: String,
        mimeType: String,
    ): DocumentsFile

    /** The provider's current display name for [file]. */
    fun displayNameFor(file: DocumentsFile): String

    /** The provider-reported size in bytes, or -1 when unknown. */
    fun sizeBytes(file: DocumentsFile): Long

    fun openOutput(file: DocumentsFile): OutputStream

    fun openInput(file: DocumentsFile): InputStream

    /** Best-effort delete; false when the provider refused. Never throws. */
    fun delete(file: DocumentsFile): Boolean
}

/**
 * Streaming digests for verification. JVM-pure (java.security), so it runs identically in
 * local unit tests and on device.
 */
object StreamDigest {
    private const val CHUNK = 64 * 1024

    sealed class Result {
        /** Completed: lowercase hex SHA-256 and the number of bytes read. */
        data class Done(val sha256Hex: String, val bytes: Long) : Result()

        /** The stop flag was raised mid-stream; partial data was consumed and discarded. */
        object Stopped : Result()
    }

    /** Copies [input] to [output] while digesting, checking [stop] between chunks. */
    fun copyAndDigest(
        input: InputStream,
        output: OutputStream,
        stop: () -> Boolean,
    ): Result = pump(input, stop) { chunk, n -> output.write(chunk, 0, n) }

    /** Digests [input] without writing anywhere, checking [stop] between chunks. */
    fun digestOnly(
        input: InputStream,
        stop: () -> Boolean,
    ): Result = pump(input, stop) { _, _ -> }

    private fun pump(
        input: InputStream,
        stop: () -> Boolean,
        sink: (ByteArray, Int) -> Unit,
    ): Result {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        val chunk = ByteArray(CHUNK)
        var total = 0L
        while (true) {
            if (stop()) return Result.Stopped
            val n = input.read(chunk)
            if (n < 0) break
            if (n == 0) continue
            digest.update(chunk, 0, n)
            sink(chunk, n)
            total += n
        }
        return Result.Done(hex(digest.digest()), total)
    }

    private fun hex(bytes: ByteArray): String = buildString(64) {
        for (b in bytes) {
            append("0123456789abcdef"[(b.toInt() shr 4) and 0xf])
            append("0123456789abcdef"[b.toInt() and 0xf])
        }
    }
}

/** Shared name policy for display names sent to providers: conservative and reversible. */
object SafeName {
    private val FORBIDDEN = Regex("[/\\\\:*?\"<>|\\u0000-\\u001f]")
    private const val MAX_LEN = 96

    /**
     * Reduces an arbitrary manifest string to a display name a documents provider will
     * accept: control characters and separators removed, edge whitespace/dots trimmed,
     * length bounded. A short extension survives the bound: the core names episodes up
     * to ~105 characters, and cutting `.cbz` off would refuse the export as the wrong
     * kind. Empty results (or names that carried no information beyond separators)
     * fall back to [fallback].
     */
    fun of(raw: String, fallback: String): String {
        val cleaned = FORBIDDEN.replace(raw, "_").trim().trim('.')
        val dot = cleaned.lastIndexOf('.')
        val extension = if (dot > 0 && cleaned.length - dot in 2..9) cleaned.substring(dot) else ""
        val bounded = if (cleaned.length <= MAX_LEN) cleaned
            else cleaned.take(MAX_LEN - extension.length).trimEnd(' ', '.') + extension
        return if (bounded.isEmpty() || bounded.all { it == '_' }) fallback else bounded
    }
}
