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
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

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
) {
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Saving", style = MaterialTheme.typography.headlineSmall)

        PhaseBanner(state.download)
        if (state.download is DownloadsViewUi.Running) {
            ProgressCard(state.download)
        }
        if (state.remaining > 0) {
            Text(
                "${state.remaining} more to save",
                style = MaterialTheme.typography.bodyMedium,
            )
        }
        state.notice?.let { NoticeCard(it, onDismiss = onDismissNotice) }

        Spacer(Modifier.height(4.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = onGo,
                enabled = state.controlsEnabled && state.download.goEnabled(),
                modifier = Modifier.weight(1f),
            ) { Text(if (state.download is DownloadsViewUi.Running) "Running" else "Go") }
            OutlinedButton(
                onClick = onPause,
                enabled = state.controlsEnabled && state.download.pauseEnabled(),
                modifier = Modifier.weight(1f),
            ) { Text("Pause") }
        }
    }
}

@Composable
private fun PhaseBanner(download: DownloadsViewUi) {
    val text = when (download) {
        is DownloadsViewUi.Held -> when (download.reason) {
            HoldReasonUi.NOT_STARTED -> "Nothing downloads until you press Go."
            HoldReasonUi.PAUSED_BY_USER -> "Paused. Press Go to continue."
            HoldReasonUi.METERED_OR_UNKNOWN_NETWORK ->
                "Held: this network may be metered. Saving waits for an unmetered connection."
            HoldReasonUi.CONNECTION_UNVERIFIED ->
                "Held: the connection could not be verified. Press Go to try again."
            HoldReasonUi.FAILED -> "Stopped. Press Go to try again."
        }
        is DownloadsViewUi.Running ->
            download.description ?: if (download.totalUnits == 0) "Preparing…" else "Saving."
        is DownloadsViewUi.Finished -> "Finished. ${download.summary}"
    }
    Card(Modifier.fillMaxWidth()) {
        Text(text, Modifier.padding(12.dp), style = MaterialTheme.typography.bodyMedium)
    }
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
