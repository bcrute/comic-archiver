package io.github.bcrute.comicarchiver.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.net.Uri
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.R
import io.github.bcrute.comicarchiver.app
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject
import io.github.bcrute.comicarchiver.words.say

/**
 * dataSync foreground service (native line). Owns the lifetime of the in-process Python
 * workers, one for each site that is saving: a progress notification whose Stop action is
 * the same checkpoint-and-stop as the app's Stop, for every site at once, and an onTimeout
 * that checkpoints through the existing drain protocol before the system stops it. Resuming is a fresh Go; nothing starts from BOOT_COMPLETED, and a killed
 * service is never sticky. The service and its notification last only while a site has
 * something running or in flight, and the processor is held awake for exactly as long.
 */
class DownloadHostService : Service() {
    private var notificationIdentity: String? = null
    /** The newest start delivered: an end the app reports must not stop a Start that is on its way. */
    @Volatile private var lastStartId = 0
    /** The phone's partial wake lock; a test gives its own. */
    internal var cpu: CpuHold = PartialWakeLock(this)
    private val handler = Handler(Looper.getMainLooper())
    // Each hold lets go by itself, so a release that was missed cannot keep the phone awake;
    // it is taken again for as long as a site is kept alive.
    private val renewal = object : Runnable {
        override fun run() {
            if (ActiveDownloads.all().isEmpty()) { cpu.release(); return }
            cpu.acquire(HOLD_MILLIS)
            handler.postDelayed(this, RENEW_MILLIS)
        }
    }
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, say("notification.channel"),
                    NotificationManager.IMPORTANCE_LOW))
        }
        live = this
    }

    override fun onDestroy() {
        if (live === this) live = null
        handler.removeCallbacksAndMessages(null)
        cpu.release()
        super.onDestroy()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Always reach startForeground first: a startForegroundService delivery of the Stop
        // action must still promote the service within its window before it stops itself.
        lastStartId = startId
        val requested = intent?.getStringExtra(EXTRA_WORKSPACE)
        if (intent?.action == ACTION_START) {
            // Only a site's workspace in the working library now in use can be kept alive.
            val target = app.siteChoices().map { app.workspaceFor(it.key) }.firstOrNull {
                it.available && it.root.absolutePath == (requested ?: app.workspace.root.absolutePath)
            }
            if (target == null) {
                startInForeground()
                if (ActiveDownloads.all().isEmpty()) { letSleep(); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(startId) }
                return START_NOT_STICKY
            }
            if (notificationIdentity != target.libraryId) ActiveDownloads.clear()
            ActiveDownloads.add(target.root.absolutePath)
            notificationIdentity = target.libraryId
        }
        startInForeground()
        if (intent?.action == ACTION_START) keepAwake()
        // A notification from another working library names one that is no longer saving here.
        val library = intent?.getStringExtra(EXTRA_LIBRARY)
        if (intent?.action == ACTION_STOP && (library == null || library == notificationIdentity))
            checkpointAndStop(startId)
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, DownloadHostService::class.java).setAction(ACTION_STOP)
                .setData(Uri.parse("comicarchiver://downloads/${notificationIdentity ?: "inactive"}"))
                .putExtra(EXTRA_LIBRARY, notificationIdentity),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        // The launcher's own intent: Android brings the open screen forward as it is. A bare
        // intent for the activity would put a second copy of the screen on top of it.
        val open = PendingIntent.getActivity(
            this, 2, Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                .setClass(this, MainActivity::class.java)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED),
            PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(this)
        }
        startForeground(NOTIFICATION_ID, builder
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(say("notification.title"))
            .setContentIntent(open)
            .setOnlyAlertOnce(true)
            .addAction(0, say("notification.stop"), stop)
            .build())
    }

    private fun keepAwake() {
        cpu.acquire(HOLD_MILLIS)
        handler.removeCallbacks(renewal)
        handler.postDelayed(renewal, RENEW_MILLIS)
    }

    private fun letSleep() {
        handler.removeCallbacks(renewal)
        cpu.release()
    }

    /** The service ends with the last site let go. A Start on its way keeps it, and adds its site when it arrives. */
    private fun letGo(root: String) {
        if (!ActiveDownloads.contains(root) || !ActiveDownloads.remove(root)) return
        if (stopSelfResult(lastStartId)) { letSleep(); stopForeground(STOP_FOREGROUND_REMOVE) }
    }

    private fun checkpointAndStop(startId: Int, waitSeconds: Int = 15) {
        val roots = ActiveDownloads.all()
        if (roots.isEmpty()) {
            letSleep()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf(startId)
            return
        }
        // Off the main thread: the drain waits until each worker's task checkpoint lands.
        CoroutineScope(Dispatchers.IO).launch {
            try {
                // Every site is asked to pause before any is waited for, so the wait is shared.
                for (root in roots) {
                    try { app.bridge.dispatch("pause", JSONObject().put("root", root).toString()) }
                    catch (_: Exception) { }
                }
                for (root in roots) {
                    val session = java.io.File(root, ".comic-session").absolutePath
                    try {
                        app.bridge.dispatch("worker_stop", JSONObject().put("session_root", session)
                            .put("wait_seconds", waitSeconds).toString())
                    } catch (_: Exception) {
                        // This scope has no handler: a bridge failure here would crash the process.
                    }
                    ActiveDownloads.remove(root)
                }
            } finally {
                // A delayed old Stop must not tear down a subsequent Start's notification.
                if (stopSelfResult(startId)) { letSleep(); stopForeground(STOP_FOREGROUND_REMOVE) }
            }
        }
    }

    /** The service's own timeout: checkpoint exactly like Stop, without the notification action. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        checkpointAndStop(startId, waitSeconds = 2)
        // Android ends the app when the service is still up a few seconds after this call, and
        // the drain waits for each site in turn: past this bound the service stops itself and
        // the drain goes on without it. A Start that arrived meanwhile keeps the service.
        handler.postDelayed({
            if (stopSelfResult(startId)) { letSleep(); stopForeground(STOP_FOREGROUND_REMOVE) }
        }, TIMEOUT_STOP_MILLIS)
    }

    companion object {
        const val ACTION_START = "io.github.bcrute.comicarchiver.worker.START"
        const val ACTION_STOP = "io.github.bcrute.comicarchiver.worker.STOP"
        const val EXTRA_WORKSPACE = "workspace_root"
        const val EXTRA_LIBRARY = "working_library"
        private const val CHANNEL_ID = DownloadNotifications.CHANNEL_ID
        private const val NOTIFICATION_ID = 1
        /** How long one hold of the processor lasts, and how often it is taken again while a site is kept alive. */
        internal const val HOLD_MILLIS = 10 * 60_000L
        internal const val RENEW_MILLIS = 5 * 60_000L
        private const val TIMEOUT_STOP_MILLIS = 4_000L

        /** The service now up; null while there is none. */
        @Volatile private var live: DownloadHostService? = null

        /**
         * A site has nothing running or in flight any more (see DownloadStatus.over): it is no
         * longer kept alive, and the service and its notification end with the last such site.
         * Go starts them again.
         */
        fun ended(root: String) { live?.letGo(root) }
    }
}
