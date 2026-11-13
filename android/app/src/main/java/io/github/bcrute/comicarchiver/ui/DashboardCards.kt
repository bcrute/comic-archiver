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
import io.github.bcrute.comicarchiver.words.say

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
            Text(say("downloads.download_status"), style = MaterialTheme.typography.titleMedium)
            Text(if (state.signedIn) say("downloads.account_signed_in_to", "siteName" to state.siteName) else say("downloads.account_sign_in_needed"))
            Text((if (delayed) say("downloads.last_reported_downloader", "value" to (observation?.worker?.label ?: say("downloads.not_checked_yet"))) else say("downloads.downloader", "value" to (observation?.worker?.label ?: say("downloads.not_checked_yet")))))
            if (running) state.admittedAtMillis?.let {
                Text(say("downloads.elapsed_since_starting", "value" to (dashboardDuration(dashboardAge(now, it)))))
            }
            observation?.let { observed ->
                val age = dashboardAge(now, observed.receivedAtMillis)
                Text(say("downloads.status_checked_ago", "value" to (dashboardDuration(age))))
                observed.savedEpisodes?.let { Text(say("downloads.saved_episodes_recorded", "it" to it)) }
                observed.observedSeries?.let { Text(say("downloads.known_series", "it" to it)) }
                if (running) {
                    observed.activityAgeSeconds?.let {
                        val activity = if (Long.MAX_VALUE - age < it) Long.MAX_VALUE else it + age
                        Text(say("downloads.last_recorded_activity_ago", "value" to (dashboardDuration(activity))))
                        if (!delayed && activity >= 60 && observed.retrySeconds == null)
                            Text(say("downloads.no_new_progress_for_over_a"))
                    }
                    if (!delayed && observed.retrySeconds != null) {
                        val left = (observed.retrySeconds - age).coerceAtLeast(0)
                        Text(if (left == 0L) say("downloads.a_retry_is_due_waiting_for") else {
                            val why = when (observed.retryReason) {
                                "source" -> say("downloads.the_site_asked_the_app_to")
                                "images" -> say("downloads.some_images_did_not_arrive")
                                "connection" -> say("downloads.the_connection_was_interrupted")
                                "idle" -> say("downloads.the_last_step_saved_nothing_new")
                                else -> say("downloads.the_download_is_waiting_before_retrying")
                            }
                            say("downloads.next_retry_in", "why" to why, "value" to (dashboardDuration(left)))
                        })
                    }
                    observed.currentList?.let { Text(if (delayed) say("downloads.last_reported_list", "it" to it) else say("downloads.current_list", "it" to it)) }
                    if (observed.listsChecked != null && observed.listsTotal != null) {
                        LinearProgressIndicator(progress = { observed.listsChecked.toFloat() / observed.listsTotal },
                            modifier = Modifier.fillMaxWidth())
                        Text(say("downloads.of_account_lists_checked", "listsChecked" to observed.listsChecked, "listsTotal" to observed.listsTotal))
                    }
                }
            }
            OutlinedButton(onClick = onDownloadSettings, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text(say("downloads.open_download_settings")) }
        }
    }
    val cloud = state.cloud
    val overview = cloud?.overview
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(say("downloads.your_saved_library"), style = MaterialTheme.typography.titleMedium)
            Text(when (cloud?.destination) {
                "device" -> say("downloads.new_episodes_save_to_this_devices")
                "b2" -> say("downloads.new_episodes_save_to_your_backblaze")
                else -> say("downloads.the_saving_destination_has_not_been")
            })
            if (dashboardDelayed(state.cloudReadFailed, now, state.cloudCheckedAtMillis))
                Text(say("downloads.storage_updates_are_delayed_these_are"))
            if (details) state.cloudCheckedAtMillis?.let { Text(say("downloads.storage_checked_ago", "value" to (dashboardDuration(dashboardAge(now, it))))) }
            Text(if (!details && overview?.available == true) say("downloads.find_your_finished_cbz_and_epub")
                else overview?.summary ?: say("downloads.saved_file_counts_have_not_been"))
            if (details && overview?.available == true) {
                overview.metrics.forEach { Text("${it.label}: ${it.value}") }
                overview.locations.forEach { place ->
                    OutlinedButton(onClick = { onOpenCatalog(place.id) }, enabled = state.controlsEnabled,
                        modifier = Modifier.fillMaxWidth().semantics {
                            contentDescription = say("downloads.view_stored_episodes_on", "label" to place.label, "counts" to place.counts)
                        }) {
                        Column { Text(place.label); Text(place.counts) }
                    }
                }
            }
            if (details) cloud?.timeEstimate?.takeIf { it.isNotBlank() }?.let { Text(it) }
            if (details) cloud?.spaceEstimate?.takeIf { it.isNotBlank() }?.let { Text(it) }
            OutlinedButton(onClick = { onOpenCatalog("all") }, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text(say("downloads.view_saved_files")) }
            OutlinedButton(onClick = onStorageSettings, enabled = state.controlsEnabled,
                modifier = Modifier.fillMaxWidth()) { Text(say("downloads.change_saving_destination")) }
            if (details) Text(say("downloads.finished_exports_are_separate_copies_comics"))
        }
    }
}
