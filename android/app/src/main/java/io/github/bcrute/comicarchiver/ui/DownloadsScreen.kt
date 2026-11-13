package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.foundation.layout.heightIn
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.worker.DownloadNotificationAccess
import io.github.bcrute.comicarchiver.words.say

/**
 * Go/Pause and progress. Renders [DownloadsScreenUi], derived from the controller's
 * `AppState`; it owns no download policy — the Go button asks the controller to go, the
 * Pause button asks it to pause, and every hold, admission and metering decision stays in
 * the controller. Go is offered only while held or finished, so nothing here can admit a
 * download by itself.
 */
@Composable
fun DownloadsScreen(
    state: DownloadsScreenUi,
    onGo: () -> Unit,
    onPause: () -> Unit,
    onDismissNotice: (() -> Unit)? = null,
    notificationAccess: DownloadNotificationAccess = DownloadNotificationAccess.ENABLED,
    notificationError: String? = null,
    onNotificationAccess: () -> Unit = {},
    onDownloadSettings: () -> Unit = {},
    onStorageSettings: () -> Unit = {},
    onOpenCatalog: (String) -> Unit = {},
    onGoAll: () -> Unit = {},
    onPauseAll: () -> Unit = {},
) {
    val now = dashboardNowMillis()
    val statusDelayed = dashboardDelayed(state.statusReadFailed, now,
        state.observations?.receivedAtMillis ?: state.admittedAtMillis.takeIf { state.download is DownloadsViewUi.Running })
    var details by rememberSaveable { mutableStateOf(false) }
    val running = state.download is DownloadsViewUi.Running
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp).testTag("downloads-scroll"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(say("app.downloads"), style = MaterialTheme.typography.headlineMedium)
        Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(when {
                    running && statusDelayed -> say("downloads.status_unknown")
                    state.headline != null -> state.headline
                    running -> say("downloads.saving_your_episodes")
                    state.download is DownloadsViewUi.Finished -> say("downloads.saving_finished")
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.PAUSED_BY_USER -> say("downloads.saving_paused")
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.METERED_OR_UNKNOWN_NETWORK -> say("downloads.waiting_for_your_connection")
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.CONNECTION_UNVERIFIED -> say("downloads.check_your_connection")
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.FAILED -> say("downloads.saving_stopped")
                    else -> say("downloads.ready_to_save")
                }, style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
                PhaseBanner(if (statusDelayed && state.download is DownloadsViewUi.Running)
                    state.download.copy(description = say("downloads.saving_status_is_unavailable")) else state.download)
                if (statusDelayed) Text(say("downloads.status_updates_are_delayed_the_downloaders"))
                if (state.download is DownloadsViewUi.Running) {
                    if (statusDelayed) Text(if (state.observations != null) say("downloads.at_the_last_successful_status_check")
                        else say("downloads.waiting_for_the_first_status_update"))
                    ProgressCard(state.download)
                }
                state.observations?.savedEpisodes?.let {
                    Text(if (it == 1L) say("downloads.1_episode_saved") else say("downloads.episodes_saved", "it" to it), style = MaterialTheme.typography.titleMedium)
                }
                if (state.remaining > 0) Text(say("downloads.more_to_save", "remaining" to state.remaining))
                Button(onClick = if (running) onPause else onGo,
                    enabled = state.controlsEnabled && (if (running) state.download.pauseEnabled() else state.download.goEnabled()),
                    modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp).testTag("saving-action")) {
                    Text(if (running) say("downloads.pause") else state.download.startLabel())
                }
            }
        }
        state.notice?.let { NoticeCard(it, onDismiss = onDismissNotice) }
        state.allSites?.let { all ->
            Card(Modifier.fillMaxWidth().testTag("all-sites")) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(say("downloads.all_sites"), style = MaterialTheme.typography.titleMedium)
                    all.counts?.let { Text(it) }
                    all.lines.forEach { Text(it) }
                    if (all.starting) Text(say("downloads.starting_each_signed_in_site_in"),
                        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        OutlinedButton(onClick = onGoAll, enabled = state.controlsEnabled,
                            modifier = Modifier.weight(1f)) { Text(say("downloads.start_all")) }
                        if (all.anySaving || all.starting) OutlinedButton(onClick = onPauseAll,
                            modifier = Modifier.weight(1f)) { Text(say("downloads.pause_all")) }
                    }
                }
            }
        }
        state.siteCards.forEach { card ->
            Card(Modifier.fillMaxWidth().testTag("site-card-${card.site}")) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("${card.name} · ${card.state}", style = MaterialTheme.typography.titleMedium)
                    if (card.message.isNotBlank()) Text(card.message, color = MaterialTheme.colorScheme.error)
                    card.rows.forEach { row ->
                        Text("${row.name}: ${row.value}",
                            color = if (row.warning) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
                    }
                }
            }
        }
        DashboardCards(state, now, onDownloadSettings, onStorageSettings, onOpenCatalog, details)
        TextButton(onClick = { details = !details }, modifier = Modifier.fillMaxWidth()) {
            Text(if (details) say("downloads.hide_download_details") else say("downloads.download_details"))
        }
        if (notificationAccess != DownloadNotificationAccess.ENABLED) {
            Text(notificationError ?: say("downloads.notifications_are_off_enable_them_to"),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            TextButton(onClick = onNotificationAccess) {
                Text(if (notificationAccess == DownloadNotificationAccess.ASK_PERMISSION)
                    say("downloads.allow_download_notifications") else say("downloads.notification_settings"))
            }
        }
    }
}

@Composable
private fun PhaseBanner(download: DownloadsViewUi) {
    val text = when (download) {
        is DownloadsViewUi.Held -> when (download.reason) {
            HoldReasonUi.NOT_STARTED -> say("downloads.save_the_episodes_you_can_access")
            HoldReasonUi.PAUSED_BY_USER -> say("downloads.your_progress_is_kept_resume_when")
            HoldReasonUi.METERED_OR_UNKNOWN_NETWORK ->
                say("downloads.saving_was_held_by_your_network")
            HoldReasonUi.CONNECTION_UNVERIFIED ->
                say("downloads.your_connection_could_not_be_verified")
            HoldReasonUi.FAILED -> say("downloads.saving_stopped_your_saved_files_are")
        }
        is DownloadsViewUi.Running ->
            download.description ?: if (download.totalUnits == 0) say("downloads.preparing") else say("downloads.saving")
        is DownloadsViewUi.Finished -> say("downloads.finished", "summary" to download.summary)
    }
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun ProgressCard(running: DownloadsViewUi.Running) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            if (running.totalUnits > 0) {
                LinearProgressIndicator(
                    progress = { running.doneUnits.toFloat() / running.totalUnits },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    say("downloads.of_saved", "doneUnits" to running.doneUnits, "totalUnits" to running.totalUnits),
                    style = MaterialTheme.typography.bodySmall,
                )
            } else {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
            running.detail?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

/** Labels describe the next action; the controller still owns admission. */
internal fun DownloadsViewUi.startLabel(): String = when (this) {
    is DownloadsViewUi.Held -> when (reason) {
        HoldReasonUi.NOT_STARTED -> say("downloads.start_saving")
        HoldReasonUi.FAILED, HoldReasonUi.CONNECTION_UNVERIFIED -> say("downloads.try_again")
        else -> say("downloads.resume")
    }
    else -> say("downloads.start_saving")
}
