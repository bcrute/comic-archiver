package io.github.bcrute.comicarchiver.worker

import android.Manifest
import android.app.Activity
import android.app.NotificationManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings

enum class DownloadNotificationAccess { ENABLED, ASK_PERMISSION, SETTINGS }

/** Device-wide access only. Changing notification access never admits a download. */
internal class DownloadNotifications(private val activity: Activity) {
    private val preferences = activity.getSharedPreferences("comicarchiver_notifications", Context.MODE_PRIVATE)

    fun access(): DownloadNotificationAccess {
        if (Build.VERSION.SDK_INT >= 33 &&
            activity.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return if (!preferences.getBoolean("requested", false) ||
                activity.shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS))
                DownloadNotificationAccess.ASK_PERMISSION else DownloadNotificationAccess.SETTINGS
        }
        val manager = activity.getSystemService(NotificationManager::class.java)
        if (!manager.areNotificationsEnabled()) return DownloadNotificationAccess.SETTINGS
        if (Build.VERSION.SDK_INT >= 26 &&
            manager.getNotificationChannel(CHANNEL_ID)?.importance == NotificationManager.IMPORTANCE_NONE)
            return DownloadNotificationAccess.SETTINGS
        return DownloadNotificationAccess.ENABLED
    }

    fun recordRequest() { preferences.edit().putBoolean("requested", true).apply() }

    fun openSettings(): Boolean {
        val destinations = buildList {
            if (Build.VERSION.SDK_INT >= 26) add(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, activity.packageName))
            add(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", activity.packageName, null)))
        }
        for (intent in destinations) {
            try { activity.startActivity(intent); return true }
            catch (_: ActivityNotFoundException) { }
            catch (_: SecurityException) { }
        }
        return false
    }

    companion object { const val CHANNEL_ID = "downloads" }
}
