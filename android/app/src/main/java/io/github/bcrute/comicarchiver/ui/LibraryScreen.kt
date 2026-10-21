package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
 * Library plus the batch-export panel. The list renders what the controller reports as
 * finished; the export panel renders the SAF exporter's journal-backed state. Folder
 * picking (ACTION_OPEN_DOCUMENT_TREE and its persisted grant) is launched by app
 * assembly — this screen only asks for it through [onPickFolder] — and the export run
 * itself is the exporter wired by the native line.
 */
@Composable
fun LibraryScreen(
    series: List<LibrarySeriesUi>,
    exportState: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Text("Library", style = MaterialTheme.typography.headlineSmall) }
        item { ExportPanel(exportState, onPickFolder, onStartExport, onStopExport) }
        item {
            Text(
                "Finished comics and novels",
                style = MaterialTheme.typography.titleMedium,
            )
        }
        if (series.isEmpty()) {
            item {
                Text(
                    "Nothing finished yet. Saved comics appear here as CBZ and novels as EPUB.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        } else {
            items(series, key = { it.seriesId }) { item ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp)) {
                        Text(item.title, style = MaterialTheme.typography.titleSmall)
                        Text(
                            "${item.episodeCount} episodes" +
                                (if (item.cbzCount > 0) " · ${item.cbzCount} CBZ" else "") +
                                (if (item.epubCount > 0) " · ${item.epubCount} EPUB" else ""),
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ExportPanel(
    state: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Export", style = MaterialTheme.typography.titleMedium)
            Text(
                "Copies finished CBZ and EPUB files into series folders you choose. " +
                    "Every copy is checked before it counts as exported.",
                style = MaterialTheme.typography.bodySmall,
            )
            state.progress?.let { p ->
                if (p.total > 0) {
                    LinearProgressIndicator(
                        progress = {
                            (p.verified + p.alreadyVerified).toFloat() / p.total
                        },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        "${p.verified + p.alreadyVerified} of ${p.total} verified" +
                            (if (p.failed > 0) " · ${p.failed} failed" else ""),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            state.lastIssue?.let { NoticeCard(NoticeUi(it)) }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                when (state.phase) {
                    ExportPhaseUi.NOT_PICKED, ExportPhaseUi.GRANT_LOST -> Button(
                        onClick = onPickFolder,
                        modifier = Modifier.weight(1f),
                    ) { Text("Choose export folder") }

                    ExportPhaseUi.READY, ExportPhaseUi.INTERRUPTED,
                    ExportPhaseUi.FINISHED_WITH_FAILURES,
                    -> Button(
                        onClick = onStartExport,
                        modifier = Modifier.weight(1f),
                    ) {
                        Text(
                            when (state.phase) {
                                ExportPhaseUi.INTERRUPTED -> "Continue export"
                                ExportPhaseUi.FINISHED_WITH_FAILURES -> "Retry failed files"
                                else -> "Export all"
                            },
                        )
                    }

                    ExportPhaseUi.RUNNING -> OutlinedButton(
                        onClick = onStopExport,
                        modifier = Modifier.weight(1f),
                    ) { Text("Stop export") }

                    ExportPhaseUi.ALL_VERIFIED -> OutlinedButton(
                        onClick = onPickFolder,
                        modifier = Modifier.weight(1f),
                    ) { Text("All files exported") }
                }
            }
        }
    }
}
