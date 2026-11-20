package io.github.bcrute.comicarchiver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
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
import io.github.bcrute.comicarchiver.controller.EpisodeList
import io.github.bcrute.comicarchiver.controller.SiteChoice
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
import io.github.bcrute.comicarchiver.words.say

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
    onWorkingLibrary: (() -> Unit)? = null,
    showFilesRequested: Boolean = false,
    onFilesRequestConsumed: () -> Unit = {},
    showComicsRequested: Boolean = false,
    onComicsRequestConsumed: () -> Unit = {},
    onChooseComics: (Boolean, String?, String?) -> Unit = { _, _, _ -> },
    comicChoicesBusy: Boolean = false,
    comicChoicesNotice: String? = null,
    sites: List<SiteChoice> = emptyList(),
    episodes: Map<String, EpisodeList> = emptyMap(),
    onLoadEpisodes: (String, String?) -> Unit = { _, _ -> },
    onRefreshEpisodes: () -> Unit = {},
) {
    var view by rememberSaveable { mutableStateOf("Files") }
    var options by remember { mutableStateOf(false) }
    BackHandler(enabled = view == "Export") { view = "Files" }
    LaunchedEffect(showFilesRequested) {
        if (showFilesRequested) { view = "Files"; onFilesRequestConsumed() }
    }
    LaunchedEffect(showComicsRequested) {
        if (showComicsRequested) { view = "Episodes"; onComicsRequestConsumed() }
    }
    val viewScroll = rememberLazyListState()
    var previousView by remember { mutableStateOf(view) }
    LaunchedEffect(view) {
        val switched = previousView != view
        previousView = view
        // Returning from another app restores this pane. Reloading an existing
        // page hides its rows during loading and clamps the saved list to the top.
        if (view == "Files" && !catalog.loading && (switched || catalog.page == null))
            onCatalogQuery(catalog.query, false)
    }
    Column(Modifier.fillMaxSize()) {
        if (!compactEditing) Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
            LazyRow(Modifier.weight(1f).testTag("library-views"), state = viewScroll,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(listOf("Files", "Series", "Folder", "Episodes")) { choice ->
                    FilterChip(selected = view == choice, onClick = { view = choice }, label = { Text(libraryLabel(choice)) })
                }
            }
            Box {
                IconButton(onClick = { options = true }) {
                    Icon(Icons.Default.MoreVert, contentDescription = say("library.library_options"))
                }
                DropdownMenu(options, onDismissRequest = { options = false }) {
                    DropdownMenuItem(text = { Text(say("library.export_files")) }, onClick = { options = false; view = say("library.export") })
                    onWorkingLibrary?.let { action ->
                        DropdownMenuItem(text = { Text(say("library.change_working_library")) }, onClick = { options = false; action() })
                    }
                }
            }
        }
        if (view != "Export" && exportState.phase == ExportPhaseUi.RUNNING)
            TextButton(onClick = { view = say("library.export") }) { Text(say("library.export_running_view_progress")) }

        Box(Modifier.weight(1f)) {
            if (view == "Export") Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(say("library.export_files"), style = MaterialTheme.typography.headlineMedium)
                Text(say("library.copy_finished_archives_to_a_folder"))
                ExportPanel(exportState, onPickFolder, onStartExport, onStopExport, actionsEnabled)
            }
            else if (view == "Series") SeriesLibraryScreen(series)
            else if (view == "Episodes") EpisodeLibraryScreen(sites, episodes, onLoadEpisodes, onRefreshEpisodes,
                onChooseComics, comicChoicesBusy || !actionsEnabled, comicChoicesNotice)
            else if (view == "Folder") FolderLibraryScreen(folderState, onFolderLoad, onOpenFolder,
                onCloseFolder, onFolderQuery, onOpenFolderFile)
            else SavedFilesScreen(catalog, onCatalogQuery, onOpenFile) {}
        }
    }
}

@Composable
private fun SeriesLibraryScreen(series: List<LibrarySeriesUi>) {
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
                Text(say("app.library"), style = MaterialTheme.typography.headlineSmall)
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it.take(200) },
                    label = { Text(say("library.find_saved_series")) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    trailingIcon = if (query.isNotEmpty()) {
                        { TextButton(onClick = { query = "" }) { Text(say("library.clear_search")) } }
                    } else null,
                )
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(listOf("All", "Comics", "Novels")) { choice ->
                        FilterChip(
                            selected = format == choice,
                            onClick = { format = choice },
                            label = { Text(libraryLabel(choice)) },
                        )
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(say("library.of_series", "size" to matches.size, "size2" to series.size), Modifier.weight(1f))
                    Box {
                        TextButton(onClick = { sortOpen = true }) { Text(say("library.sort", "sort" to libraryLabel(sort))) }
                        DropdownMenu(expanded = sortOpen, onDismissRequest = { sortOpen = false }) {
                            listOf("Title A–Z", "Title Z–A", "Most saved").forEach { choice ->
                                DropdownMenuItem(
                                    text = { Text(libraryLabel(choice)) },
                                    onClick = { sort = choice; sortOpen = false },
                                )
                            }
                        }
                    }
                }
            }
        }
        if (series.isEmpty()) {
            item {
                Text(
                    say("library.nothing_finished_yet_saved_comics_appear"),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        } else if (matches.isEmpty()) {
            item { Text(say("library.no_saved_series_match_these_filters")) }
        } else {
            items(matches, key = { it.seriesId }) { item ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp)) {
                        Text(item.title, style = MaterialTheme.typography.titleSmall)
                        Text(
                            say("library.of_saved", "savedCount" to item.savedCount, "episodeCount" to item.episodeCount, "formatLabel" to item.formatLabel),
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
            Text(say("library.export"), style = MaterialTheme.typography.titleMedium)
            Text(
                say("library.copies_finished_cbz_and_epub_files"),
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
                        say("library.of_verified", "value" to (p.verified + p.alreadyVerified), "total" to p.total) +
                            (if (p.failed > 0) say("library.failed", "failed" to p.failed) else ""),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            state.lastIssue?.let { NoticeCard(NoticeUi(it)) }
            if (state.phase == ExportPhaseUi.ALL_VERIFIED) Text(say("library.last_export_verified"))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                when (state.phase) {
                    ExportPhaseUi.NOT_PICKED, ExportPhaseUi.GRANT_LOST -> Button(
                        enabled = actionsEnabled,
                        onClick = onPickFolder,
                        modifier = Modifier.weight(1f),
                    ) { Text(say("library.choose_export_folder")) }

                    ExportPhaseUi.READY, ExportPhaseUi.INTERRUPTED,
                    ExportPhaseUi.FINISHED_WITH_FAILURES, ExportPhaseUi.ALL_VERIFIED,
                    -> Button(
                        enabled = actionsEnabled,
                        onClick = onStartExport,
                        modifier = Modifier.weight(1f),
                    ) {
                        Text(
                            when (state.phase) {
                                ExportPhaseUi.INTERRUPTED -> say("library.continue_export")
                                ExportPhaseUi.FINISHED_WITH_FAILURES -> say("library.retry_failed_files")
                                else -> say("library.export_all")
                            },
                        )
                    }

                    ExportPhaseUi.RUNNING -> OutlinedButton(
                        onClick = onStopExport,
                        modifier = Modifier.weight(1f),
                    ) { Text(say("library.stop_export")) }
                }
            }
            if (state.phase in setOf(ExportPhaseUi.READY, ExportPhaseUi.INTERRUPTED,
                    ExportPhaseUi.FINISHED_WITH_FAILURES, ExportPhaseUi.ALL_VERIFIED)) {
                TextButton(onClick = onPickFolder, modifier = Modifier.fillMaxWidth()) {
                    Text(say("library.change_export_folder"))
                }
            }
        }
    }
}

/** The words for a choice the Library screens keep by a fixed name. */
internal fun libraryLabel(choice: String): String = when (choice) {
    "Files" -> say("library.view.files")
    "Series" -> say("library.view.series")
    "Folder" -> say("library.view.folder")
    "Episodes" -> say("library.view.episodes")
    "All" -> say("library.format.all")
    "Comics" -> say("library.format.comics")
    "Novels" -> say("library.format.novels")
    "Title A–Z" -> say("library.sort.title_a_z")
    "Title Z–A" -> say("library.sort.title_z_a")
    "Most saved" -> say("library.sort.most_saved")
    else -> choice
}
