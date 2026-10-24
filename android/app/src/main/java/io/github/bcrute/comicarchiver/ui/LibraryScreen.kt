package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import io.github.bcrute.comicarchiver.controller.CatalogState
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.foundation.layout.Box
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.library.FolderLibraryState

/**
 * Library plus the batch-export panel. The list renders what the controller reports as
 * finished; the export panel renders the SAF exporter's journal-backed state. Folder
 * picking (ACTION_OPEN_DOCUMENT_TREE and its persisted grant) is launched by app
 * assembly — this screen only asks for it through [onPickFolder] — and the export run
 * itself is the exporter wired by the native line.
 */
@Composable
fun LibraryScreen(
    series: List<LibrarySeriesUi>, exportState: ExportPanelStateUi,
    onPickFolder: () -> Unit, onStartExport: () -> Unit, onStopExport: () -> Unit,
    catalog: CatalogState = CatalogState(),
    onCatalogQuery: (CatalogQuery, Boolean) -> Unit = { _, _ -> },
    onOpenFile: (String) -> Unit = {},
    folderState: FolderLibraryState = FolderLibraryState(),
    onFolderLoad: () -> Unit = {}, onOpenFolder: () -> Unit = {}, onCloseFolder: () -> Unit = {},
    onFolderQuery: (CatalogQuery, Boolean) -> Unit = { _, _ -> }, onOpenFolderFile: (String) -> Unit = {},
    actionsEnabled: Boolean = true,
    compactEditing: Boolean = false,
    showFilesRequested: Boolean = false,
    onFilesRequestConsumed: () -> Unit = {},
    onReadFile: ((String) -> Unit)? = null,
) {
    var view by rememberSaveable { mutableStateOf("Series") }
    LaunchedEffect(showFilesRequested) {
        if (showFilesRequested) { view = "Files"; onFilesRequestConsumed() }
    }
    val viewScroll = rememberLazyListState()
    var previousView by remember { mutableStateOf(view) }
    LaunchedEffect(view) {
        val switched = previousView != view
        previousView = view
        // Returning from the reader restores this pane. Reloading an existing
        // page hides its rows during loading and clamps the saved list to the top.
        if (view == "Files" && !catalog.loading && (switched || catalog.page == null))
            onCatalogQuery(catalog.query, false)
    }
    Column(Modifier.fillMaxSize()) {
        if (!compactEditing) LazyRow(Modifier.padding(horizontal = 16.dp), state = viewScroll,
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(listOf("Series", "Files", "Folder")) { choice ->
                FilterChip(selected = view == choice, onClick = { view = choice }, label = { Text(choice) })
            }
        }
        Box(Modifier.weight(1f)) {
            if (view == "Series") SeriesLibraryScreen(series, exportState, onPickFolder, onStartExport, onStopExport, actionsEnabled)
            else if (view == "Folder") FolderLibraryScreen(folderState, onFolderLoad, onOpenFolder,
                onCloseFolder, onFolderQuery, onOpenFolderFile)
            else SavedFilesScreen(catalog, onCatalogQuery, onOpenFile, onReadFile) {
                ExportPanel(exportState, onPickFolder, onStartExport, onStopExport, actionsEnabled)
            }
        }
    }
}

@Composable
private fun SeriesLibraryScreen(
    series: List<LibrarySeriesUi>,
    exportState: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
    actionsEnabled: Boolean,
) {
    var query by rememberSaveable { mutableStateOf("") }
    var format by rememberSaveable { mutableStateOf("All") }
    var sort by rememberSaveable { mutableStateOf("Title A–Z") }
    var sortOpen by remember { mutableStateOf(false) }
    val matches = remember(series, query, format, sort) {
        val needle = query.trim()
        val filtered = series.filter {
            it.title.contains(needle, ignoreCase = true) &&
                (format == "All" || it.formatLabel == if (format == "Comics") "CBZ" else "EPUB")
        }
        val byTitle = compareBy<LibrarySeriesUi> { it.title.lowercase(java.util.Locale.ROOT) }
            .thenBy { it.seriesId }
        when (sort) {
            "Title Z–A" -> filtered.sortedWith(byTitle.reversed())
            "Most saved" -> filtered.sortedWith(
                compareByDescending<LibrarySeriesUi> { it.savedCount }.then(byTitle))
            else -> filtered.sortedWith(byTitle)
        }
    }
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(16.dp).testTag("series-library-list"),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Library", style = MaterialTheme.typography.headlineSmall)
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it.take(200) },
                    label = { Text("Find saved series") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    trailingIcon = if (query.isNotEmpty()) {
                        { TextButton(onClick = { query = "" }) { Text("Clear search") } }
                    } else null,
                )
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(listOf("All", "Comics", "Novels")) { choice ->
                        FilterChip(
                            selected = format == choice,
                            onClick = { format = choice },
                            label = { Text(choice) },
                        )
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("${matches.size} of ${series.size} series", Modifier.weight(1f))
                    Box {
                        TextButton(onClick = { sortOpen = true }) { Text("Sort: $sort") }
                        DropdownMenu(expanded = sortOpen, onDismissRequest = { sortOpen = false }) {
                            listOf("Title A–Z", "Title Z–A", "Most saved").forEach { choice ->
                                DropdownMenuItem(
                                    text = { Text(choice) },
                                    onClick = { sort = choice; sortOpen = false },
                                )
                            }
                        }
                    }
                }
            }
        }
        item { ExportPanel(exportState, onPickFolder, onStartExport, onStopExport, actionsEnabled) }
        if (series.isEmpty()) {
            item {
                Text(
                    "Nothing finished yet. Saved comics appear here as CBZ and novels as EPUB.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        } else if (matches.isEmpty()) {
            item { Text("No saved series match these filters.") }
        } else {
            items(matches, key = { it.seriesId }) { item ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp)) {
                        Text(item.title, style = MaterialTheme.typography.titleSmall)
                        Text(
                            "${item.savedCount} of ${item.episodeCount} saved · ${item.formatLabel}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
internal fun ExportPanel(
    state: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
    actionsEnabled: Boolean = true,
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
            if (state.phase == ExportPhaseUi.ALL_VERIFIED) Text("Last export verified.")
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                when (state.phase) {
                    ExportPhaseUi.NOT_PICKED, ExportPhaseUi.GRANT_LOST -> Button(
                        enabled = actionsEnabled,
                        onClick = onPickFolder,
                        modifier = Modifier.weight(1f),
                    ) { Text("Choose export folder") }

                    ExportPhaseUi.READY, ExportPhaseUi.INTERRUPTED,
                    ExportPhaseUi.FINISHED_WITH_FAILURES, ExportPhaseUi.ALL_VERIFIED,
                    -> Button(
                        enabled = actionsEnabled,
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
                }
            }
            if (state.phase in setOf(ExportPhaseUi.READY, ExportPhaseUi.INTERRUPTED,
                    ExportPhaseUi.FINISHED_WITH_FAILURES, ExportPhaseUi.ALL_VERIFIED)) {
                TextButton(onClick = onPickFolder, modifier = Modifier.fillMaxWidth()) {
                    Text("Change export folder")
                }
            }
        }
    }
}
