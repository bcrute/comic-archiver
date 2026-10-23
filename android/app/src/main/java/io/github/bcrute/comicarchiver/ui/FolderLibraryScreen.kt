package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.library.FolderLibraryState

@Composable
fun FolderLibraryScreen(state: FolderLibraryState, onLoad: () -> Unit, onChoose: () -> Unit,
    onClose: () -> Unit, onQuery: (CatalogQuery, Boolean) -> Unit, onOpen: (String) -> Unit) {
    LaunchedEffect(Unit) { onLoad() }
    val panel: @Composable () -> Unit = {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(state.label?.let { "Folder: $it" } ?: "Open a library folder", style = MaterialTheme.typography.titleMedium)
                Text("Browse a Library, project or exported series folder. Originals stay where they are. " +
                    "Opening needs temporary space on this device. New downloads keep their current destination.")
                if (state.choosing) { LinearProgressIndicator(Modifier.fillMaxWidth()); Text("Reading folder…") }
                state.message?.let { Text(it) }
                if (state.needsFolder) Text("Choose the folder again to restore access.")
                Button(onClick = onChoose, enabled = !state.choosing) {
                    Text(if (state.selected) "Change library folder" else "Open library folder")
                }
                if (state.selected || state.choosing) TextButton(onClick = onClose) { Text("Close folder") }
            }
        }
    }
    if (state.selected && !state.choosing) SavedFilesScreen(state.catalog, onQuery, onOpen, panel)
    else LazyColumn(Modifier.fillMaxSize().padding(16.dp)) { item { panel() } }
}
