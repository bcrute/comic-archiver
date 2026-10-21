package io.github.bcrute.comicarchiver.worker

import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * dataSync foreground service (native line; unimplemented scaffold until the first fleet
 * build). Declared in the manifest with foregroundServiceType="dataSync"; it owns the
 * in-process Python worker's lifetime, the progress notification with Stop, and the
 * onTimeout checkpoint. Never started from BOOT_COMPLETED.
 */
class DownloadHostService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null
}
