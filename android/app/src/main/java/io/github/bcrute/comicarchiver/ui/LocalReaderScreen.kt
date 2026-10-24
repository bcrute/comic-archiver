package io.github.bcrute.comicarchiver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import io.github.bcrute.comicarchiver.reader.LocalReaderState
import io.github.bcrute.comicarchiver.reader.ReaderViewport
import io.github.bcrute.comicarchiver.reader.ReadingPosition
import io.github.bcrute.comicarchiver.reader.ReadingPreferences

data class LocalReaderActions(val close: () -> Unit = {}, val previous: () -> Unit = {},
    val next: () -> Unit = {}, val bookmark: () -> Unit = {}, val reload: () -> Unit = {},
    val jump: (ReadingPosition) -> Unit = {}, val preferences: (ReadingPreferences) -> Unit = {},
    val scrolled: (Int) -> Unit = {}, val dismissFeedback: () -> Unit = {})

@Composable
@OptIn(ExperimentalLayoutApi::class)
fun LocalReaderScreen(state: LocalReaderState, actions: LocalReaderActions) {
    BackHandler { if (!state.closing) actions.close() }
    var options by rememberSaveable(state.id) { mutableStateOf(false) }
    val book = state.book
    val noun = if (book?.record?.kind == "epub") "Chapter" else "Page"
    val foreground = MaterialTheme.colorScheme.onSurface.toArgb()
    BoxWithConstraints(Modifier.fillMaxSize().testTag("local-reader")) {
        val headerMaximum = (maxHeight * .5f).coerceAtLeast(60.dp)
        Column(Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxWidth().heightIn(max = headerMaximum)
                .verticalScroll(rememberScrollState()).padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(book?.title ?: "Saved file", Modifier.weight(1f), maxLines = 2,
                        overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.titleMedium)
                    TextButton(onClick = actions.close, enabled = !state.closing) { Text("Close reader") }
                }
                if (book != null) Text("$noun ${state.position.unit + 1} of ${book.record.units}")
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = actions.previous, enabled = !state.busy && book != null && state.position.unit > 0) { Text("Previous") }
                    TextButton(onClick = actions.next, enabled = !state.busy && book != null && state.position.unit + 1 < book.record.units) { Text("Next") }
                    TextButton(onClick = { options = true }, enabled = book != null && !state.closing) { Text("Reader options") }
                }
                if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                Text(when {
                    state.closing -> "Closing reader…"
                    state.loading -> "Loading saved page…"
                    state.saving -> "Saving reading progress…"
                    book?.warning != null -> book.warning.orEmpty()
                    book != null && book.canSave && book.record.revision > 0 && book.record.position == state.position &&
                        book.record.preferences == state.preferences -> "Reading progress saved."
                    else -> "Reading locally."
                })
            }
            if (state.error != null) {
                Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp)) {
                    Text(state.error)
                    Button(onClick = actions.reload, enabled = !state.busy) { Text("Retry reading") }
                }
            } else state.document?.let { document ->
                AndroidView(factory = { ReaderViewport(it) }, modifier = Modifier.weight(1f).fillMaxWidth(),
                    update = { it.show(document, state.position, state.preferences, state.generation, foreground, actions.scrolled) })
            }
        }
    }
    if (options && book != null) Dialog(onDismissRequest = { options = false },
        properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize().padding(16.dp), color = MaterialTheme.colorScheme.surface) {
            LazyColumn(Modifier.fillMaxSize().padding(16.dp).testTag("reader-options"),
                verticalArrangement = Arrangement.spacedBy(10.dp)) {
                item {
                    Text("Reader options", style = MaterialTheme.typography.titleLarge)
                    TextButton(onClick = { options = false }) { Text("Close options") }
                }
                item {
                    if (book.record.kind == "cbz") {
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf("width" to "Fit width", "page" to "Fit page").forEach { (key, label) ->
                                FilterChip(selected = state.preferences.fit == key, enabled = !state.busy,
                                    onClick = { actions.preferences(state.preferences.copy(fit = key)) }, label = { Text(label) })
                            }
                        }
                    } else {
                        Text("Text ${(state.preferences.textScale * 100).toInt()}%")
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            TextButton(enabled = !state.busy && state.preferences.textScale > .75,
                                onClick = { actions.preferences(state.preferences.copy(textScale = (state.preferences.textScale - .25).coerceAtLeast(.75))) }) { Text("Smaller text") }
                            TextButton(enabled = !state.busy && state.preferences.textScale < 2,
                                onClick = { actions.preferences(state.preferences.copy(textScale = (state.preferences.textScale + .25).coerceAtMost(2.0))) }) { Text("Larger text") }
                        }
                    }
                }
                item {
                    Button(onClick = actions.bookmark, enabled = !state.busy && state.document != null) {
                        Text(if (state.position in book.record.bookmarks) "Remove bookmark" else "Add bookmark")
                    }
                    TextButton(onClick = actions.reload, enabled = !state.busy) { Text("Reload saved progress") }
                    book.warning?.let { Text(it) }
                    Text("Bookmarks", style = MaterialTheme.typography.titleMedium)
                    if (book.record.bookmarks.isEmpty()) Text("No bookmarks yet.")
                }
                itemsIndexed(book.record.bookmarks) { _, position ->
                    TextButton(enabled = !state.busy, onClick = { options = false; actions.jump(position) }) {
                        Text("$noun ${position.unit + 1} · ${position.offset / 100}%")
                    }
                }
            }
        }
    }
}
