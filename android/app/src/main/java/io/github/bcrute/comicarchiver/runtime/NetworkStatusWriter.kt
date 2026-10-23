package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** One platform callback and one serialized writer for the selected workspace.
 * Selecting a site invalidates its old facts before new facts can permit work.
 * No network name, address or device identity enters the private status file. */
object NetworkStatusWriter {
    private val executor = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "net-status-writer").apply { isDaemon = true }
    }
    private val gate = Any()
    private var manager: ConnectivityManager? = null
    private var callback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var target: File? = null

    init {
        executor.scheduleWithFixedDelay({
            val current = synchronized(gate) { manager }
            if (current != null) write(current)
        }, 5, 5, TimeUnit.SECONDS)
    }

    fun start(context: Context, statusFile: File) {
        synchronized(gate) {
            if (target != statusFile) {
                target = statusFile
                invalidate(statusFile)
            }
            val current = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            if (current == null) { invalidate(statusFile); return }
            if (manager === current && callback != null) { write(current); return }
            callback?.let { old -> try { manager?.unregisterNetworkCallback(old) } catch (_: Exception) {} }
            manager = current
            val next = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = write(current)
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = write(current)
                override fun onLost(network: Network) = write(current, network)
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

    private fun invalidate(file: File) { try { file.delete() } catch (_: Exception) {} }

    private fun write(current: ConnectivityManager, lost: Network? = null) {
        val selected = target ?: return
        executor.execute {
            try {
                val network = current.activeNetwork
                val caps = network?.takeUnless { it == lost }?.let { current.getNetworkCapabilities(it) }
                val connection = when {
                    caps == null -> "none"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "wired"
                    else -> "other"
                }
                val metered = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) != true
                val text = """{"schema_version":1,"observed_at_ms":${System.currentTimeMillis()},"connection":"$connection","metered":$metered}"""
                synchronized(gate) {
                    if (target != selected || manager !== current) return@execute
                    selected.parentFile?.mkdirs()
                    val scratch = File(selected.parentFile, selected.name + ".tmp")
                    scratch.writeText(text)
                    if (!scratch.renameTo(selected)) { scratch.delete(); invalidate(selected) }
                }
            } catch (_: Exception) {
                synchronized(gate) { if (target == selected) invalidate(selected) }
            }
        }
    }
}
