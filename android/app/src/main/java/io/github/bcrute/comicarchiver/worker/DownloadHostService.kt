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
import android.net.Uri
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
    private var workspaceRoot: String? = null
    private var notificationIdentity: String? = null
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
        val requested = intent?.getStringExtra(EXTRA_WORKSPACE)
        if (intent?.action == ACTION_START) {
            val target = app.workspace
            val current = target.root.absolutePath
            if (!target.available || requested != null && requested != current) {
                startInForeground()
                if (workspaceRoot == null) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(startId) }
                return START_NOT_STICKY
            }
            workspaceRoot = current
            notificationIdentity = target.identity
        }
        startInForeground()
        if (intent?.action == ACTION_STOP && (requested == null || requested == workspaceRoot))
            checkpointAndStop(startId)
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, DownloadHostService::class.java).setAction(ACTION_STOP)
                .setData(Uri.parse("comicarchiver://downloads/${notificationIdentity ?: "inactive"}"))
                .putExtra(EXTRA_WORKSPACE, workspaceRoot),
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

    private fun checkpointAndStop(startId: Int, waitSeconds: Int = 15) {
        val root = workspaceRoot
        if (root == null) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf(startId)
            return
        }
        // Off the main thread: the drain waits until the worker's task checkpoint lands.
        CoroutineScope(Dispatchers.IO).launch {
            val session = java.io.File(root, ".comic-session").absolutePath
            try {
                app.bridge.dispatch("pause", JSONObject().put("root", root).toString())
                app.bridge.dispatch("worker_stop", JSONObject().put("session_root", session)
                    .put("wait_seconds", waitSeconds).toString())
            } catch (_: Exception) {
                // This scope has no handler: a bridge failure here would crash the process.
            } finally {
                // A delayed old Stop must not tear down a subsequent Start's notification.
                if (stopSelfResult(startId)) stopForeground(STOP_FOREGROUND_REMOVE)
            }
        }
    }

    /** The service's own timeout: checkpoint exactly like Stop, without the notification action. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        checkpointAndStop(startId, waitSeconds = 2)
    }

    companion object {
        const val ACTION_START = "io.github.bcrute.comicarchiver.worker.START"
        const val ACTION_STOP = "io.github.bcrute.comicarchiver.worker.STOP"
        const val EXTRA_WORKSPACE = "workspace_root"
        private const val CHANNEL_ID = DownloadNotifications.CHANNEL_ID
        private const val NOTIFICATION_ID = 1
    }
}
