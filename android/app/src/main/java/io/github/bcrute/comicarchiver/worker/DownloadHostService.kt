package io.github.bcrute.comicarchiver.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.R
import io.github.bcrute.comicarchiver.app
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * dataSync foreground service (native line). Owns the in-process Python worker's lifetime:
 * a progress notification whose Stop action is the same checkpoint-and-stop as the app's
 * Stop, and an onTimeout that checkpoints through the existing drain protocol before the
 * system stops it. Resuming is a fresh Go; nothing starts from BOOT_COMPLETED, and a killed
 * service is never sticky.
 */
class DownloadHostService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.download_channel_name),
                    NotificationManager.IMPORTANCE_LOW))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Always reach startForeground first: a startForegroundService delivery of the Stop
        // action must still promote the service within its window before it stops itself.
        startInForeground()
        if (intent?.action == ACTION_STOP) checkpointAndStop()
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, DownloadHostService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val open = PendingIntent.getActivity(
            this, 2, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(this)
        }
        startForeground(NOTIFICATION_ID, builder
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(getString(R.string.download_notification_title))
            .setContentIntent(open)
            .setOnlyAlertOnce(true)
            .addAction(0, getString(R.string.download_notification_stop), stop)
            .build())
    }

    private fun checkpointAndStop() {
        // Off the main thread: the drain waits until the worker's task checkpoint lands.
        CoroutineScope(Dispatchers.IO).launch {
            val root = filesDir.resolve("workspace").absolutePath
            val session = filesDir.resolve("workspace/.comic-session").absolutePath
            app.bridge.dispatch("pause", JSONObject().put("root", root).toString())
            app.bridge.dispatch(
                "worker_stop",
                JSONObject().put("session_root", session).put("wait_seconds", 15).toString())
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    /** The service's own timeout: checkpoint exactly like Stop, without the notification action. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        checkpointAndStop()
    }

    companion object {
        const val ACTION_START = "io.github.bcrute.comicarchiver.worker.START"
        const val ACTION_STOP = "io.github.bcrute.comicarchiver.worker.STOP"
        private const val CHANNEL_ID = "downloads"
        private const val NOTIFICATION_ID = 1
    }
}
