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
        Text("Downloads", style = MaterialTheme.typography.headlineMedium)
        Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(when {
                    running && statusDelayed -> "Status unknown"
                    running -> "Saving your episodes"
                    state.download is DownloadsViewUi.Finished -> "Saving finished"
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.PAUSED_BY_USER -> "Saving paused"
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.METERED_OR_UNKNOWN_NETWORK -> "Waiting for your connection"
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.CONNECTION_UNVERIFIED -> "Check your connection"
                    (state.download as? DownloadsViewUi.Held)?.reason == HoldReasonUi.FAILED -> "Saving stopped"
                    else -> "Ready to save"
                }, style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
                PhaseBanner(if (statusDelayed && state.download is DownloadsViewUi.Running)
                    state.download.copy(description = "Saving status is unavailable.") else state.download)
                if (statusDelayed) Text("Status updates are delayed. The downloader's current activity is unknown. You can still Pause if saving was running.")
                if (state.download is DownloadsViewUi.Running) {
                    if (statusDelayed) Text(if (state.observations != null) "At the last successful status check:"
                        else "Waiting for the first status update.")
                    ProgressCard(state.download)
                }
                state.observations?.savedEpisodes?.let {
                    Text("$it episodes saved", style = MaterialTheme.typography.titleMedium)
                }
                if (state.remaining > 0) Text("${state.remaining} more to save")
                Button(onClick = if (running) onPause else onGo,
                    enabled = state.controlsEnabled && (if (running) state.download.pauseEnabled() else state.download.goEnabled()),
                    modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp).testTag("saving-action")) {
                    Text(if (running) "Pause" else state.download.startLabel())
                }
            }
        }
        state.notice?.let { NoticeCard(it, onDismiss = onDismissNotice) }
        DashboardCards(state, now, onDownloadSettings, onStorageSettings, onOpenCatalog, details)
        TextButton(onClick = { details = !details }, modifier = Modifier.fillMaxWidth()) {
            Text(if (details) "Hide download details" else "Download details")
        }
        if (notificationAccess != DownloadNotificationAccess.ENABLED) {
            Text(notificationError ?: "Notifications are off. Enable them to control saving while the app is in the background.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            TextButton(onClick = onNotificationAccess) {
                Text(if (notificationAccess == DownloadNotificationAccess.ASK_PERMISSION)
                    "Allow download notifications" else "Notification settings")
            }
        }
    }
}

@Composable
private fun PhaseBanner(download: DownloadsViewUi) {
    val text = when (download) {
        is DownloadsViewUi.Held -> when (download.reason) {
            HoldReasonUi.NOT_STARTED -> "Save the episodes you can access as CBZ and EPUB files. Nothing starts until you choose Start saving."
            HoldReasonUi.PAUSED_BY_USER -> "Your progress is kept. Resume when you’re ready."
            HoldReasonUi.METERED_OR_UNKNOWN_NETWORK ->
                "Saving was held by your network setting. Check your connection and metered-data choice in Settings, then choose Resume."
            HoldReasonUi.CONNECTION_UNVERIFIED ->
                "Your connection could not be verified. Check it and try again."
            HoldReasonUi.FAILED -> "Saving stopped. Your saved files are kept. Try again when ready."
        }
        is DownloadsViewUi.Running ->
            download.description ?: if (download.totalUnits == 0) "Preparing…" else "Saving."
        is DownloadsViewUi.Finished -> "Finished. ${download.summary}"
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
                    "${running.doneUnits} of ${running.totalUnits} saved",
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
        HoldReasonUi.NOT_STARTED -> "Start saving"
        HoldReasonUi.FAILED, HoldReasonUi.CONNECTION_UNVERIFIED -> "Try again"
        else -> "Resume"
    }
    else -> "Start saving"
}
