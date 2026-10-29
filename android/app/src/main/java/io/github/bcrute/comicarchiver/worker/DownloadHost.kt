package io.github.bcrute.comicarchiver.worker

/**
 * Frozen lifecycle seam. The native line owns the
 * implementation: a dataSync foreground service that runs the Python worker in-process with
 * an explicit workspace root (background.py's subprocess launcher is not used on Android).
 *
 * Downloads are always Held until [go]. [checkpointAndStop] is the one stop path (user Stop,
 * the notification action, or the service's onTimeout) and goes through the existing drain
 * protocol. Nothing starts from BOOT_COMPLETED; resuming after any stop is a fresh [go].
 */
interface DownloadHost {
    /** Start the service and worker if absent; state begins Held. Idempotent. */
    fun start()

    /** The only transition into downloads running. */
    fun go(onResult: (String?) -> Unit = {})

    fun pause()

    /** Cooperative checkpoint-and-stop through the existing drain protocol; safe at any point. */
    fun checkpointAndStop(reason: String)

    /** True while the service and its worker are up. */
    fun isRunning(): Boolean
}
