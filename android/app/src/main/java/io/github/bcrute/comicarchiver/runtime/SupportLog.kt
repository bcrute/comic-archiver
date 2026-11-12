package io.github.bcrute.comicarchiver.runtime

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.provider.MediaStore
import java.io.File
import java.io.IOException

/**
 * The phone's part of the desktop's Copy details for support and Save log for support. What
 * the details and the log hold is decided by the shared core; this only names the phone,
 * keeps the log's folders and hands the finished text to the clipboard or to Downloads.
 */
object SupportLog {
    /** Where the app's own log is kept: private, and removed with the app. */
    fun folder(context: Context): File = File(context.filesDir, "support-log")

    /** Where a log is put together before it is handed to Downloads. */
    fun outbox(context: Context): File = File(context.cacheDir, "support-out")

    /** The phone's system as a support reply needs it, in letters the core accepts. */
    fun system(release: String? = Build.VERSION.RELEASE, api: Int = Build.VERSION.SDK_INT): String {
        val name = release.orEmpty().filter { it.isLetterOrDigit() && it.code < 128 || it in " ._-" }.take(20).trim()
        return if (name.isEmpty()) "Android (API $api)" else "Android $name (API $api)"
    }

    /** The installed app's version, or "unknown". */
    fun version(context: Context): String {
        val name = try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        } catch (_: Exception) { null }
        return name.orEmpty().filter { it.isLetterOrDigit() && it.code < 128 || it in ".+-" }.take(40)
            .ifEmpty { "unknown" }
    }

    fun copy(context: Context, text: String) {
        val clipboard = context.getSystemService(ClipboardManager::class.java) ?: throw IOException()
        clipboard.setPrimaryClip(ClipData.newPlainText("Comic Archiver details for support", text))
    }

    /** Put [file] in the phone's Downloads as a text file of the same name; the name it has
     *  there, or null when it could not be written. Needs no permission from Android 10 on. */
    fun toDownloads(context: Context, file: File): String? {
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
            put(MediaStore.MediaColumns.MIME_TYPE, "text/plain")
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val place = try {
            resolver.insert(MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY), values)
        } catch (_: Exception) { null } ?: return null
        return try {
            (resolver.openOutputStream(place) ?: throw IOException()).use { out ->
                file.inputStream().use { it.copyTo(out) }
            }
            resolver.update(place, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
            resolver.query(place, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)?.use { row ->
                if (row.moveToFirst()) row.getString(0) else null
            } ?: file.name
        } catch (_: Exception) {
            try { resolver.delete(place, null, null) } catch (_: Exception) {}
            null
        }
    }
}
