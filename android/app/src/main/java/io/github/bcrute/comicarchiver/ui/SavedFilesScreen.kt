package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.CatalogChoice
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.controller.CatalogState
import java.text.DateFormat
import java.util.Date
import io.github.bcrute.comicarchiver.words.say

/** The desktop catalog's projection, laid out for a phone. Filters are server-supplied,
 * storage is never scanned here, and only a catalog-offered CBZ/EPUB gets Open. */
@Composable
@OptIn(ExperimentalLayoutApi::class)
fun SavedFilesScreen(state: CatalogState, onQuery: (CatalogQuery, Boolean) -> Unit,
                     onOpen: (String) -> Unit,
                     exportPanel: @Composable () -> Unit) {
    val query = state.query
    val page = state.page
    val browser = LocalUriHandler.current
    var draft by rememberSaveable { mutableStateOf(query.query) }
    var filters by rememberSaveable { mutableStateOf(false) }
    var pageDraft by rememberSaveable { mutableStateOf("1") }
    LaunchedEffect(query.query) { draft = query.query }
    LaunchedEffect(query.offset, query.limit) { pageDraft = (query.offset / query.limit + 1).toString() }
    LazyColumn(Modifier.fillMaxSize().padding(16.dp).testTag("saved-files-list"),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(say("library.saved_files"), style = MaterialTheme.typography.headlineSmall)
                OutlinedTextField(value = draft, onValueChange = { draft = it.take(200) },
                    label = { Text(say("library.find_saved_files")) }, singleLine = true, enabled = !state.loading,
                    modifier = Modifier.fillMaxWidth())
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Button(enabled = !state.loading, onClick = { onQuery(query.copy(query = draft.trim(), offset = 0), false) }) {
                        Text(say("library.search"))
                    }
                    TextButton(enabled = !state.loading, onClick = {
                        draft = ""; onQuery(query.copy(query = "", kind = "episodes", offset = 0), false)
                    }) { Text(say("library.clear")) }
                    TextButton(enabled = !state.loading, onClick = { onQuery(query, true) }) { Text(say("library.refresh_files")) }
                }
                TextButton(onClick = { filters = !filters }) { Text(if (filters) say("library.hide_filters") else "Filters & sort") }
                if (page != null && filters) {
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        CatalogMenu(say("library.file_types"), page.kinds, query.kind, !state.loading, Modifier.weight(1f)) {
                            onQuery(query.copy(kind = it, offset = 0), false)
                        }
                        CatalogMenu(say("library.sort_by"), page.sorts, query.sort, !state.loading, Modifier.weight(1f)) {
                            onQuery(query.copy(sort = it, offset = 0), false)
                        }
                    }
                    CatalogMenu(say("cloud.storage"), page.locations, query.location, !state.loading) {
                        onQuery(query.copy(location = it, offset = 0), false)
                    }
                    CatalogMenu(say("library.files_per_page"), page.pageSizes.map { CatalogChoice(it.toString(), it.toString()) },
                        query.limit.toString(), !state.loading) {
                        onQuery(query.copy(limit = it.toInt(), offset = 0), false)
                    }
                }
                if (state.loading) {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    Text(say("library.loading_saved_files"))
                }
                state.error?.let { Text(it) }
                if (state.feedbackFor == null) state.feedback?.let { Text(it) }
            }
        }
        item { exportPanel() }
        if (!state.loading && state.error == null && page != null) {
            item { Text(if (page.total == 1) "1 file" else say("library.files", "total" to page.total)) }
            if (page.total > query.limit) item {
                val first = query.offset + 1
                val last = (query.offset + page.rows.size).coerceAtMost(page.total)
                Text(say("library.showing", "first" to first, "last" to last))
            }
            if (page.notice.isNotBlank()) item { Text(page.notice) }
            if (page.rows.isEmpty()) item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(say("library.no_saved_files_in_this_view"), style = MaterialTheme.typography.titleMedium)
                        Text(if (query.query.isNotBlank()) say("library.try_clearing_your_search_or_changing")
                            else say("library.save_episodes_from_downloads_change_the"))
                    }
                }
            }
            items(page.rows, key = { it.key }) { file ->
                Card(Modifier.fillMaxWidth().testTag("saved-file-${file.key}")) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(file.series, style = MaterialTheme.typography.titleSmall)
                        Text(file.title)
                        Text("${file.type} · ${file.size} · ${file.storage}")
                        file.openId?.let { id ->
                            if (state.feedbackFor == id) state.feedback?.let {
                                Text(it, modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
                            }
                            val action = if (state.openingId == id) say("library.opening") else say("library.open")
                            Button(onClick = { onOpen(id) }, enabled = state.openingId == null,
                                modifier = Modifier.semantics {
                                    contentDescription = "$action ${file.title}, ${file.series}. ${file.filename}. ${file.storage}"
                                }) {
                                Text(if (state.openingId == id) say("library.opening_2") else say("library.open"))
                            }
                        }
                        var details by rememberSaveable(file.key) { mutableStateOf(false) }
                        TextButton(onClick = { details = !details }, modifier = Modifier.testTag("file-details-${file.key}")) {
                            Text(if (details) say("library.hide_file_details") else say("library.file_details"))
                        }
                        if (details) {
                            Text(file.filename, style = MaterialTheme.typography.bodySmall)
                        file.savedEpoch?.let { epoch ->
                            Text(DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT)
                                .format(Date((epoch * 1000).toLong())) + if (file.fileTime) say("library.file_time") else "")
                        }
                        }
                        if (file.b2Copy) {
                            var browserFailed by rememberSaveable(file.key, file.filename, file.series, file.storage) {
                                mutableStateOf(false)
                            }
                            Text(say("library.cloud_copy_use_your_backblaze_account"))
                            TextButton(onClick = {
                                browserFailed = try {
                                    browser.openUri(B2Page.BROWSE.url)
                                    false
                                } catch (_: Exception) { true }
                            }, modifier = Modifier.semantics {
                                contentDescription = say("library.browse_in_backblaze", "title" to file.title, "series" to file.series, "filename" to file.filename)
                            }) { Text(say("cloud.open_in_backblaze")) }
                            if (browserFailed) Text(say("library.could_not_open_backblaze_check_that"))
                        }
                    }
                }
            }
            item {
                val pages = maxOf(1L, (page.total.toLong() + query.limit - 1) / query.limit).toInt()
                if (pages > 1) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(enabled = query.offset > 0, onClick = {
                        onQuery(query.copy(offset = (query.offset - query.limit).coerceAtLeast(0)), false)
                    }) { Text(say("library.previous")) }
                    TextButton(enabled = query.offset + query.limit < page.total, onClick = {
                        onQuery(query.copy(offset = (query.offset + query.limit).coerceAtMost(10_000_000)), false)
                    }) { Text(say("library.next")) }
                    Text(say("library.page_of", "value" to (query.offset / query.limit + 1), "pages" to pages))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(value = pageDraft, onValueChange = { value ->
                        if (value.length <= 8 && value.all(Char::isDigit)) pageDraft = value
                    }, label = { Text(say("library.page")) }, singleLine = true, modifier = Modifier.weight(1f))
                    TextButton(enabled = pageDraft.toIntOrNull() != null, onClick = {
                        val chosen = (pageDraft.toIntOrNull() ?: 1).coerceIn(1, pages)
                        onQuery(query.copy(offset = ((chosen.toLong() - 1) * query.limit)
                            .coerceAtMost(10_000_000).toInt()), false)
                    }) { Text(say("library.go_to_page")) }
                }
                }
            }
        }
    }
}

@Composable
private fun CatalogMenu(label: String, choices: List<CatalogChoice>, selected: String, enabled: Boolean,
                        modifier: Modifier = Modifier, onSelect: (String) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    Box(modifier) {
        TextButton(onClick = { expanded = true }, enabled = enabled) {
            Text("$label: ${choices.firstOrNull { it.key == selected }?.label ?: selected}")
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            choices.forEach { choice ->
                DropdownMenuItem(text = { Text(choice.label) }, onClick = {
                    expanded = false; onSelect(choice.key)
                })
            }
        }
    }
}
