package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
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
 * Go/Pause and progress. Renders [DownloadsStateUi]; owns no download policy — the Go
 * button asks the controller to go, the Pause button asks it to pause, and every hold,
 * admission and metering decision stays in the controller. Held (for any reason) is the
 * only shape in which Go is offered, so nothing here can admit a download by itself.
 */
@Composable
fun DownloadsScreen(
    state: DownloadsStateUi,
    onGo: () -> Unit,
    onPause: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Saving", style = MaterialTheme.typography.headlineSmall)

        HoldBanner(state)
        state.active?.let { active ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(active.label, style = MaterialTheme.typography.titleSmall)
                    if (active.totalUnits > 0) {
                        LinearProgressIndicator(
                            progress = { active.doneUnits.toFloat() / active.totalUnits },
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                    Text(
                        "${active.doneUnits} of ${active.totalUnits} saved",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
        if (state.queueDepth > 0) {
            Text(
                "${state.queueDepth} more in the queue",
                style = MaterialTheme.typography.bodyMedium,
            )
        }
        state.notice?.let { NoticeCard(it) }

        Spacer(Modifier.height(4.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = onGo,
                enabled = !state.running,
                modifier = Modifier.weight(1f),
            ) { Text("Go") }
            OutlinedButton(
                onClick = onPause,
                enabled = state.running,
                modifier = Modifier.weight(1f),
            ) { Text("Pause") }
        }
    }
}

@Composable
private fun HoldBanner(state: DownloadsStateUi) {
    val text = when (state.holdReason) {
        HoldReasonUi.NOT_STARTED ->
            "Nothing downloads until you press Go."
        HoldReasonUi.PAUSED_BY_USER ->
            "Paused. Press Go to continue."
        HoldReasonUi.METERED_OR_UNKNOWN_NETWORK ->
            "Held: this network may be metered. Saving waits for an unmetered connection."
        HoldReasonUi.CONNECTION_UNVERIFIED ->
            "Held: the connection could not be verified. Press Go to try again."
        HoldReasonUi.RESTARTED_HELD_FOR_GO ->
            "Held after a restart. Nothing continues until you press Go."
        null ->
            "Saving."
    }
    Card(Modifier.fillMaxWidth()) {
        Text(text, Modifier.padding(12.dp), style = MaterialTheme.typography.bodyMedium)
    }
}
