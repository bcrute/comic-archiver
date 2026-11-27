package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import io.github.bcrute.comicarchiver.worker.DownloadHostService
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** One platform callback and one serialized writer for every opened site's workspace:
 * each site may be saving at once, and each reads the report beside its own library.
 * Opening a site invalidates its old facts before new facts can permit work.
 * No network name, address or device identity enters the private status file. */
object NetworkStatusWriter {
    private val executor = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "net-status-writer").apply { isDaemon = true }
    }
    private val gate = Any()
    private var manager: ConnectivityManager? = null
    private var callback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var targets: Set<File> = emptySet()
    /** The kind of connection and whether it is metered, as last told; other details change too often to matter. */
    @Volatile private var told: String? = null

    init {
        executor.scheduleWithFixedDelay({
            val current = synchronized(gate) { manager }
            if (current != null) write(current)
        }, 5, 5, TimeUnit.SECONDS)
    }

    fun start(context: Context, statusFile: File) {
        synchronized(gate) {
            if (statusFile !in targets) {
                targets = targets + statusFile
                invalidate(statusFile)
            }
            val current = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            if (current == null) { invalidate(statusFile); return }
            if (manager === current && callback != null) { write(current); return }
            callback?.let { old -> try { manager?.unregisterNetworkCallback(old) } catch (_: Exception) {} }
            manager = current
            val next = object : ConnectivityManager.NetworkCallback() {
                // A download waiting for the connection is told of each change at once, on this thread:
                // the phone may have woken only for it.
                override fun onAvailable(network: Network) { told = null; DownloadHostService.connectionChanged(); write(current) }
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                    val facts = "${kind(caps)}/${isMetered(caps)}"
                    if (facts != told) { told = facts; DownloadHostService.connectionChanged() }
                    write(current)
                }
                override fun onLost(network: Network) { told = null; DownloadHostService.connectionChanged(); write(current, network) }
            }
            try {
                current.registerDefaultNetworkCallback(next)
                callback = next
                write(current)
            } catch (_: SecurityException) {
                callback = null
                invalidate(statusFile)
            }
        }
    }

    /** Another working library is in use: its sites' reports are no longer kept fresh. */
    fun forgetAll() {
        synchronized(gate) { targets.forEach(::invalidate); targets = emptySet() }
    }

    private fun invalidate(file: File) { try { file.delete() } catch (_: Exception) {} }

    private fun kind(caps: NetworkCapabilities?) = when {
        caps == null -> "none"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "wired"
        else -> "other"
    }

    private fun isMetered(caps: NetworkCapabilities?) = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) != true

    private fun write(current: ConnectivityManager, lost: Network? = null) {
        if (targets.isEmpty()) return
        executor.execute {
            try {
                val network = current.activeNetwork
                val caps = network?.takeUnless { it == lost }?.let { current.getNetworkCapabilities(it) }
                val connection = kind(caps)
                val metered = isMetered(caps)
                val text = """{"schema_version":1,"observed_at_ms":${System.currentTimeMillis()},"connection":"$connection","metered":$metered}"""
                synchronized(gate) {
                    if (manager !== current) return@execute
                    for (selected in targets) {
                        try {
                            selected.parentFile?.mkdirs()
                            val scratch = File(selected.parentFile, selected.name + ".tmp")
                            scratch.writeText(text)
                            if (!scratch.renameTo(selected)) { scratch.delete(); invalidate(selected) }
                        } catch (_: Exception) { invalidate(selected) }
                    }
                }
            } catch (_: Exception) {
                synchronized(gate) { targets.forEach(::invalidate) }
            }
        }
    }
}
