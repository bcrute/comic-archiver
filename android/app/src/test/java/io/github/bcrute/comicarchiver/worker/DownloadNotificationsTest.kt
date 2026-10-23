package io.github.bcrute.comicarchiver.worker

import android.Manifest
import android.app.Activity
import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.provider.Settings
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [24, 34])
class DownloadNotificationsTest {
    @Test fun appPermissionAndChannelBlocksOfferTheRightRecovery() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val access = DownloadNotifications(activity)
        val manager = activity.getSystemService(NotificationManager::class.java)
        shadowOf(manager).setNotificationsEnabled(true)
        if (Build.VERSION.SDK_INT >= 33) {
            shadowOf(activity.application).denyPermissions(Manifest.permission.POST_NOTIFICATIONS)
            assertEquals(DownloadNotificationAccess.ASK_PERMISSION, access.access())
            shadowOf(activity.application).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        }
        assertEquals(DownloadNotificationAccess.ENABLED, access.access())
        shadowOf(manager).setNotificationsEnabled(false)
        assertEquals(DownloadNotificationAccess.SETTINGS, access.access())
        shadowOf(manager).setNotificationsEnabled(true)
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(NotificationChannel(DownloadNotifications.CHANNEL_ID,
                "Synthetic downloads", NotificationManager.IMPORTANCE_NONE))
            assertEquals(DownloadNotificationAccess.SETTINGS, access.access())
        } else assertEquals(DownloadNotificationAccess.ENABLED, access.access())
    }

    @Test fun aNewAccessReaderKeepsRequestHistoryAndOnlyOneDevicePreference() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val access = DownloadNotifications(activity)
        shadowOf(activity.getSystemService(NotificationManager::class.java)).setNotificationsEnabled(true)
        if (Build.VERSION.SDK_INT >= 33)
            shadowOf(activity.application).denyPermissions(Manifest.permission.POST_NOTIFICATIONS)
        access.recordRequest()
        assertEquals(mapOf("requested" to true), activity.getSharedPreferences(
            "comicarchiver_notifications", Context.MODE_PRIVATE).all)
        val recreated = DownloadNotifications(activity)
        if (Build.VERSION.SDK_INT >= 33) {
            assertEquals(DownloadNotificationAccess.SETTINGS, recreated.access())
            shadowOf(activity.packageManager).setShouldShowRequestPermissionRationale(
                Manifest.permission.POST_NOTIFICATIONS, true)
            assertEquals(DownloadNotificationAccess.ASK_PERMISSION, recreated.access())
        } else assertEquals(DownloadNotificationAccess.ENABLED, recreated.access())
    }

    @Test fun settingsOpenOnlyThisAppWithAnOldAndroidFallback() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        assertTrue(DownloadNotifications(activity).openSettings())
        val intent = shadowOf(activity).nextStartedActivity
        if (Build.VERSION.SDK_INT >= 26) {
            assertEquals(Settings.ACTION_APP_NOTIFICATION_SETTINGS, intent.action)
            assertEquals(activity.packageName, intent.getStringExtra(Settings.EXTRA_APP_PACKAGE))
        } else {
            assertEquals(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, intent.action)
            assertEquals("package:${activity.packageName}", intent.dataString)
        }
    }
}
