package io.github.bcrute.comicarchiver.ui

import android.os.SystemClock
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

/** Time passing only ages observations. It never advances progress or changes admission. */
internal fun dashboardAge(nowMillis: Long, recordedMillis: Long): Long =
    ((nowMillis - recordedMillis).coerceAtLeast(0) / 1000)

internal fun dashboardDuration(seconds: Long): String = when {
    seconds >= 3600 -> "${seconds / 3600}h ${seconds % 3600 / 60}m ${seconds % 60}s"
    seconds >= 60 -> "${seconds / 60}m ${seconds % 60}s"
    else -> "${seconds.coerceAtLeast(0)}s"
}

internal fun dashboardDelayed(readFailed: Boolean, nowMillis: Long, recordedMillis: Long?): Boolean =
    readFailed || recordedMillis != null && dashboardAge(nowMillis, recordedMillis) >= 30

@Composable
internal fun dashboardNowMillis(): Long {
    var now by remember { mutableStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(Unit) {
        while (isActive) {
            delay(1000)
            now = SystemClock.elapsedRealtime()
        }
    }
    return now
}

@Composable
internal fun DashboardCards(state: DownloadsScreenUi, now: Long, onDownloadSettings: () -> Unit,
    onStorageSettings: () -> Unit, onOpenCatalog: (String) -> Unit, details: Boolean = true) {
    val running = state.download is DownloadsViewUi.Running
    val observation = state.observations
    val delayed = dashboardDelayed(state.statusReadFailed, now, observation?.receivedAtMillis)
    if (details) Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Download status", style = MaterialTheme.typography.titleMedium)
            Text(if (state.signedIn) "Account: Signed in to ${state.siteName}" else "Account: Sign-in needed")
            Text("${if (delayed) "Last reported downloader" else "Downloader"}: ${observation?.worker?.label ?: "Not checked yet"}")
            if (running) state.admittedAtMillis?.let {
                Text("Elapsed since starting: ${dashboardDuration(dashboardAge(now, it))}")
            }
            observation?.let { observed ->
                val age = dashboardAge(now, observed.receivedAtMillis)
                Text("Status checked ${dashboardDuration(age)} ago")
                observed.savedEpisodes?.let { Text("$it saved episodes recorded") }
                observed.observedSeries?.let { Text("$it known series") }
                if (running) {
                    observed.activityAgeSeconds?.let {
                        val activity = if (Long.MAX_VALUE - age < it) Long.MAX_VALUE else it + age
                        Text("Last recorded activity: ${dashboardDuration(activity)} ago")
                        if (!delayed && activity >= 60 && observed.retrySeconds == null)
                            Text("No new progress for over a minute. The site may be slow; you can wait or press Pause.")
                    }
                    if (!delayed && observed.retrySeconds != null) {
                        val left = (observed.retrySeconds - age).coerceAtLeast(0)
                        Text(if (left == 0L) "A retry is due; waiting for the next status update." else {
                            val why = when (observed.retryReason) {
                                "source" -> "The site asked the app to wait."
                                "images" -> "Some images did not arrive."
                                "connection" -> "The connection was interrupted."
                                else -> "The download is waiting before retrying."
                            }
                            "$why Next retry in ${dashboardDuration(left)}."
                        })
                    }
                    observed.currentList?.let { Text("${if (delayed) "Last reported list" else "Current list"}: $it") }
                    if (observed.listsChecked != null && observed.listsTotal != null) {
                        LinearProgressIndicator(progress = { observed.listsChecked.toFloat() / observed.listsTotal },
                            modifier = Modifier.fillMaxWidth())
                        Text("${observed.listsChecked} of ${observed.listsTotal} account lists checked")
                    }
                }
            }
            OutlinedButton(onClick = onDownloadSettings, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text("Open download settings") }
        }
    }
    val cloud = state.cloud
    val overview = cloud?.overview
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Your saved library", style = MaterialTheme.typography.titleMedium)
            Text(when (cloud?.destination) {
                "device" -> "New episodes save to this device's working library."
                "b2" -> "New episodes save to your Backblaze B2 storage."
                else -> "The saving destination has not been checked yet."
            })
            if (dashboardDelayed(state.cloudReadFailed, now, state.cloudCheckedAtMillis))
                Text("Storage updates are delayed. These are the last recorded counts and destination.")
            if (details) state.cloudCheckedAtMillis?.let { Text("Storage checked ${dashboardDuration(dashboardAge(now, it))} ago") }
            Text(if (!details && overview?.available == true) "Find your finished CBZ and EPUB files."
                else overview?.summary ?: "Saved file counts have not been read. Open View saved files to check them.")
            if (details && overview?.available == true) {
                overview.metrics.forEach { Text("${it.label}: ${it.value}") }
                overview.locations.forEach { place ->
                    OutlinedButton(onClick = { onOpenCatalog(place.id) }, enabled = state.controlsEnabled,
                        modifier = Modifier.fillMaxWidth().semantics {
                            contentDescription = "View stored episodes on ${place.label}. ${place.counts}"
                        }) {
                        Column { Text(place.label); Text(place.counts) }
                    }
                }
            }
            if (details) cloud?.timeEstimate?.takeIf { it.isNotBlank() }?.let { Text(it) }
            if (details) cloud?.spaceEstimate?.takeIf { it.isNotBlank() }?.let { Text(it) }
            OutlinedButton(onClick = { onOpenCatalog("all") }, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text("View saved files") }
            OutlinedButton(onClick = onStorageSettings, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text("Change saving destination") }
            if (details) Text("Finished exports are separate copies. Comics use CBZ; novels use EPUB.")
        }
    }
}
