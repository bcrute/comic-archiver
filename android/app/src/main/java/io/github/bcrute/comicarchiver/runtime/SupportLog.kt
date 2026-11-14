package io.github.bcrute.comicarchiver.runtime

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.FileProvider
import java.io.File
import java.io.IOException
import io.github.bcrute.comicarchiver.words.say

/**
 * The phone's part of the desktop's Email support. What the log holds is decided by the shared
 * core; this only names the phone, keeps the log's folders, and hands the finished log to an
 * email app, or to Downloads when there is no email app.
 */
object SupportLog {
    /** Where the app's own log is kept: private, and removed with the app. */
    fun folder(context: Context): File = File(context.filesDir, "support-log")

    /** Where a log is put together before it is handed on. The email app reads it from here, so
     *  it stays until the next log is made; it is private and removed with the app. */
    fun outbox(context: Context): File = File(context.cacheDir, "support-out")

    /** The phone's system as a support reply needs it, in letters the core accepts. */
    fun system(release: String? = Build.VERSION.RELEASE, api: Int = Build.VERSION.SDK_INT): String {
        val name = release.orEmpty().filter { it.isLetterOrDigit() && it.code < 128 || it in " ._-" }.take(20).trim()
        return if (name.isEmpty()) say("about.android_api", "api" to api) else say("about.android_api_2", "name" to name, "api" to api)
    }

    /** The installed app's version, or "unknown". */
    fun version(context: Context): String {
        val name = try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        } catch (_: Exception) { null }
        return name.orEmpty().filter { it.isLetterOrDigit() && it.code < 128 || it in ".+-" }.take(40)
            .ifEmpty { "unknown" }
    }

    /** A new email to support in the person's email app, with [log] attached when there is one;
     *  false when the phone has no email app. */
    fun email(context: Context, log: File?, version: String): Boolean {
        val body = say("support.email_body") +
            if (log != null) "\n\n\n" + say("support.email_log", "name" to log.name) else ""
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_EMAIL, arrayOf(say("support.address")))
            putExtra(Intent.EXTRA_SUBJECT, say("support.email_subject", "version" to version))
            putExtra(Intent.EXTRA_TEXT, body)
            if (log != null) {
                val uri = FileProvider.getUriForFile(context, context.packageName + ".support", log)
                putExtra(Intent.EXTRA_STREAM, uri)
                clipData = ClipData.newRawUri(log.name, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            // Only email apps are offered: the message goes to an address.
            selector = Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:"))
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try { context.startActivity(send); true } catch (_: ActivityNotFoundException) { false }
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
