package io.github.bcrute.comicarchiver.library

import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import androidx.core.content.FileProvider
import java.io.File
import java.io.FileNotFoundException

/** Only finished portable archives can be shared. Recheck at open time as a file may
 * have disappeared or become a link since the catalog/intent was prepared. */
class ArchiveFileProvider : FileProvider() {
    private fun selected(uri: Uri): File? {
        val context = context ?: return null
        if (uri.scheme != "content" || uri.authority != context.packageName + ".archives") return null
        val parts = uri.pathSegments
        // Persisted URI identities match archive_paths.xml. The original Tapas
        // `library` root remains unchanged across updates and external read grants.
        val folder = when (parts.firstOrNull()) {
            "library" -> File(context.filesDir, "workspace/Library")
            "webtoon-library" -> File(context.filesDir, "workspace/Webtoon/Library")
            "tappytoon-library" -> File(context.filesDir, "workspace/Tappytoon/Library")
            "opened-cache" -> File(context.cacheDir, "opened-archives")
            else -> return null
        }
        return resolveArchive(folder, parts.drop(1).joinToString("/"))
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw FileNotFoundException("Saved archives are read-only.")
        val file = selected(uri) ?: throw FileNotFoundException("Saved archive is unavailable.")
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun getType(uri: Uri): String? = selected(uri)?.let { archiveMime(it.extension) }

    override fun query(uri: Uri, projection: Array<out String>?, selection: String?,
                       selectionArgs: Array<out String>?, sortOrder: String?): Cursor {
        val file = selected(uri) ?: throw FileNotFoundException("Saved archive is unavailable.")
        val columns = (projection?.toList() ?: listOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE))
            .filter { it == OpenableColumns.DISPLAY_NAME || it == OpenableColumns.SIZE }
        return MatrixCursor(columns.toTypedArray()).apply {
            addRow(columns.map { if (it == OpenableColumns.DISPLAY_NAME) file.name else file.length() })
        }
    }

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("Saved archives are read-only.")
}

internal fun archiveMime(kind: String): String? = when (kind.lowercase(java.util.Locale.ROOT)) {
    "cbz" -> "application/vnd.comicbook+zip"
    "epub" -> "application/epub+zip"
    else -> null
}

internal fun resolveArchive(library: File, relative: String): File? = try {
    val parts = relative.split('/')
    if (parts.any { it.isBlank() || it.startsWith('.') || it.contains('\\') || it.contains('\u0000') }) null
    else {
        val root = library.canonicalFile
        val target = File(root, relative).absoluteFile
        // A canonical root permits Android's own /data/user aliases; every segment
        // below it must stay unchanged, excluding both internal and escaping symlinks.
        target.takeIf { it.canonicalFile == it && it.isFile && archiveMime(it.extension) != null &&
            it.path.startsWith(root.path + File.separator) }
    }
} catch (_: java.io.IOException) { null }
