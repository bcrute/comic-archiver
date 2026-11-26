package io.github.bcrute.comicarchiver.worker

import android.content.Context
import android.os.PowerManager

/** What keeps the processor awake while the foreground service has a site to keep alive. */
internal interface CpuHold {
    /** Take the hold, or extend one already taken; it lets go by itself after [timeoutMillis]. */
    fun acquire(timeoutMillis: Long)
    fun release()
}

/**
 * The phone's partial wake lock. A foreground service does not keep the processor awake by
 * itself, so an unplugged phone with its screen off could stall between requests. The lock is
 * not counted: one release lets go however many times the hold was extended.
 */
internal class PartialWakeLock(private val context: Context) : CpuHold {
    private val lock by lazy {
        (context.getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "comicarchiver:downloads")
            .apply { setReferenceCounted(false) }
    }

    override fun acquire(timeoutMillis: Long) = lock.acquire(timeoutMillis)

    override fun release() { if (lock.isHeld) lock.release() }
}
