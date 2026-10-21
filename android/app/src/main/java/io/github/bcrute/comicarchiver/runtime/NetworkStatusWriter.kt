package io.github.bcrute.comicarchiver.runtime

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Maintains the workspace's net.json: the connection facts comic_archive.network_android
 * reads (its allowed words are wifi/wired/cellular with an explicit metered flag). The
 * platform's own report is rewritten whenever the default network or its capabilities
 * change; anything the file does not say — or a shape it does not accept — stays unknown
 * to the core, which holds all transfers rather than guess. Without this writer no
 * download would ever run. No address, SSID or interface name is written.
 */
object NetworkStatusWriter {
    private val executor: ExecutorService = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "net-status-writer").apply { isDaemon = true }
    }

    fun start(context: Context, statusFile: File) {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        // A registration the platform refuses (observed at first launch: the manifest
        // lacked ACCESS_NETWORK_STATE) must leave the app alive with no writer — the
        // core then holds every transfer as unknown, which is the truthful state —
        // never crash App.onCreate.
        try {
            write(statusFile, manager)
            manager.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = write(statusFile, manager)

                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) =
                    write(statusFile, manager)

                override fun onLost(network: Network) = write(statusFile, manager)
            })
        } catch (_: SecurityException) {
            // No callback, no file: downloads stay held rather than guessed.
        }
    }

    private fun write(statusFile: File, manager: ConnectivityManager) {
        executor.execute {
            // Any platform refusal inside the writer thread must stop this one write,
            // not the executor (a dead thread would silence every future callback
            // while the app still believed net.json was being maintained).
            try {
                val network = manager.activeNetwork
                val caps = network?.let { manager.getNetworkCapabilities(it) }
                val connection = when {
                    caps == null -> "none"  // Not an allowed word: the core holds, never guesses.
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "wired"
                    else -> "other"
                }
                val metered = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) != true
                val text = """{"connection":"$connection","metered":$metered}"""
                statusFile.parentFile?.mkdirs()
                val scratch = File(statusFile.parentFile, statusFile.name + ".tmp")
                scratch.writeText(text)
                if (!scratch.renameTo(statusFile)) scratch.delete()
            } catch (_: SecurityException) {
                // Skip this update; the last good file (or none) stands.
            }
        }
    }
}
