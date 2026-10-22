package io.github.bcrute.comicarchiver.library

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import java.io.File

internal enum class ArchiveOpenResult { REQUESTED, NO_READER, UNAVAILABLE }

internal class ArchiveViewer(
    private val context: Context,
    private val library: File,
    private val launch: (Intent) -> Unit = { context.startActivity(it) },
) {
    fun open(relative: String, kind: String): ArchiveOpenResult {
        val file = resolveArchive(library, relative) ?: return ArchiveOpenResult.UNAVAILABLE
        val mime = archiveMime(kind) ?: return ArchiveOpenResult.UNAVAILABLE
        if (archiveMime(file.extension) != mime) return ArchiveOpenResult.UNAVAILABLE
        return try {
            val uri = FileProvider.getUriForFile(context, context.packageName + ".archives", file)
            val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .apply { clipData = ClipData.newRawUri("Saved archive", uri) }
            launch(intent)
            ArchiveOpenResult.REQUESTED
        } catch (_: ActivityNotFoundException) { ArchiveOpenResult.NO_READER }
          catch (_: IllegalArgumentException) { ArchiveOpenResult.UNAVAILABLE }
          catch (_: SecurityException) { ArchiveOpenResult.UNAVAILABLE }
    }
}
