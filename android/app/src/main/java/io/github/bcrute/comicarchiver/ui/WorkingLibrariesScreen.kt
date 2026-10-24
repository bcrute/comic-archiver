package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.runtime.WorkingLibrariesState
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryPhase
import java.util.Locale

data class WorkingLibraryActions(val load: () -> Unit = {}, val pick: () -> Unit = {},
    val confirm: (Boolean) -> Unit = {}, val stop: () -> Unit = {}, val discard: () -> Unit = {},
    val retry: () -> Unit = {}, val select: (String) -> Unit = {}, val refresh: () -> Unit = {},
    val rename: (String, String) -> Unit = { _, _ -> })

@Composable
fun WorkingLibrariesScreen(state: WorkingLibrariesState, actions: WorkingLibraryActions) {
    LaunchedEffect(Unit) { actions.load() }
    val names = state.choices.groupingBy { it.label.lowercase(Locale.ROOT) }.eachCount()
    fun displayName(index: Int): String = state.choices[index].let {
        if ((names[it.label.lowercase(Locale.ROOT)] ?: 0) > 1) "${it.label} · working copy ${index + 1}" else it.label
    }
    var renameId by rememberSaveable { mutableStateOf<String?>(null) }
    var draft by rememberSaveable { mutableStateOf("") }
    var requestedName by rememberSaveable { mutableStateOf<String?>(null) }
    val renameIndex = state.choices.indexOfFirst { it.id == renameId }
    LaunchedEffect(state.choices, state.loading, state.renamedId, requestedName) {
        if (!state.loading && state.renamedId == renameId && requestedName != null &&
            state.choices.any { it.id == renameId && it.label == requestedName }) {
            renameId = null; requestedName = null
        }
    }
    if (renameIndex >= 0) {
        val choice = state.choices[renameIndex]
        val label = draft.trim()
        val duplicate = state.choices.any { it.id != choice.id && it.label.equals(label, ignoreCase = true) }
        val valid = label.isNotBlank() && label.length <= 120 && label.none { it.code < 32 || it.code == 127 } && !duplicate
        AlertDialog(
            onDismissRequest = { if (!state.loading) { renameId = null; requestedName = null } },
            title = { Text("Rename library") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(displayName(renameIndex))
                    Text("Choose a name for this working copy. Its files and source folder stay where they are.")
                    OutlinedTextField(value = draft, onValueChange = { draft = it.take(120); requestedName = null },
                        label = { Text("Library name") }, singleLine = true, enabled = !state.loading,
                        isError = duplicate, modifier = Modifier.fillMaxWidth())
                    if (duplicate) Text("Another working library already uses that name.")
                    if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                    else if (requestedName != null) state.message?.let { Text(it) }
                }
            },
            confirmButton = {
                TextButton(enabled = valid && !state.loading && !state.busy && !state.pending,
                    onClick = { requestedName = label; actions.rename(choice.id, label) }) { Text("Save name") }
            },
            dismissButton = {
                TextButton(enabled = !state.loading, onClick = { renameId = null; requestedName = null }) { Text("Cancel") }
            },
        )
    }
    LazyColumn(Modifier.fillMaxSize().padding(16.dp).testTag("working-libraries"),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Text("Working libraries", style = MaterialTheme.typography.headlineSmall)
            Text("Choose the library used for saving. Each working copy keeps its own saved files, settings and export destination.")
            Text("Folder browsing in Library is separate and reads files where they are.")
        }
        state.message?.let { item { Text(it) } }
        if (state.loading || state.busy && state.phase != WorkingLibraryPhase.PREVIEW) item {
            LinearProgressIndicator(Modifier.fillMaxWidth())
            Text(when (state.phase) {
                WorkingLibraryPhase.CHOOSING -> "Choose a folder in Android."
                WorkingLibraryPhase.SCANNING -> "Checking the selected folder…"
                WorkingLibraryPhase.CHECKING_COPY -> "Checking retained copies…"
                WorkingLibraryPhase.COPYING -> "Copying library files…"
                WorkingLibraryPhase.CHECKING_SOURCE -> "Checking that the source stayed unchanged…"
                WorkingLibraryPhase.VERIFYING -> "Verifying saved files and account binding…"
                WorkingLibraryPhase.SELECTING -> "Opening the working library…"
                WorkingLibraryPhase.STOPPING -> "Stopping the copy safely…"
                else -> "Loading library choices…"
            })
            if (state.phase in setOf(WorkingLibraryPhase.CHECKING_COPY, WorkingLibraryPhase.COPYING, WorkingLibraryPhase.CHECKING_SOURCE)) {
                Text("${state.filesDone} of ${state.filesTotal} files; ${workingBytes(state.bytesRead)} read")
            }
            if (state.phase !in setOf(WorkingLibraryPhase.SELECTING, WorkingLibraryPhase.STOPPING) && !state.loading)
                OutlinedButton(onClick = actions.stop) { Text(if (state.phase == WorkingLibraryPhase.CHOOSING) "Cancel folder choice" else "Stop copy") }
        }
        state.preview?.takeIf { state.phase == WorkingLibraryPhase.PREVIEW }?.let { preview -> item {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(preview.label, style = MaterialTheme.typography.titleMedium)
                Text("${preview.files} files to check; ${workingBytes(preview.knownBytes)}${if (preview.unknownSizes > 0) " plus files with unknown sizes" else ""}")
                Text("The source stays unchanged. This phone keeps a verified working copy, so space is needed for capture and preparation. Sign-in is checked separately before Go.")
                Text("New downloads stay in this working copy. Use Export for finished copies in a folder of your choice.")
                if (preview.empty) Text("This location is empty. A later Go saves into a new library here; the previous working library is kept.")
                if (preview.refreshedCopy || preview.existingCopy) Text("Refreshing creates a new copy from the source. Previous copies, including files saved on this phone, stay available below.")
                if (state.pending) Text("Continue the held capture. Refresh from the source later to include newer source changes.")
                Button(onClick = { actions.confirm(preview.refreshedCopy) }) {
                    Text(if (preview.existingCopy && !preview.refreshedCopy && !state.pending) "Open saved working copy"
                         else if (state.pending) "Continue held copy" else "Copy, verify and use")
                }
                if (preview.existingCopy && !preview.refreshedCopy && !state.pending)
                    OutlinedButton(onClick = { actions.confirm(true) }) { Text("Create refreshed copy") }
                OutlinedButton(onClick = actions.discard) { Text(if (state.pending) "Discard held copy" else "Cancel") }
            }
        } }
        if (state.pending && !state.busy) item {
            Text("${state.pendingLabel ?: "Library"} copy is held. The current library remains selected.")
            Button(onClick = actions.retry) { Text(if (state.phase == WorkingLibraryPhase.FAILED) "Retry library copy" else "Continue library copy") }
            OutlinedButton(onClick = actions.pick) { Text("Choose source folder again") }
            OutlinedButton(onClick = actions.discard) { Text("Discard held copy") }
        }
        if (!state.pending && !state.busy && !state.loading) item {
            Button(onClick = actions.pick, modifier = Modifier.fillMaxWidth()) { Text("Choose working library folder") }
            if (state.choices.any { it.current && it.hasSource })
                OutlinedButton(onClick = actions.refresh, modifier = Modifier.fillMaxWidth()) { Text("Refresh from source folder") }
            if (state.phase == WorkingLibraryPhase.FAILED)
                OutlinedButton(onClick = actions.load) { Text("Reload library choices") }
        }
        itemsIndexed(state.choices, key = { _, choice -> choice.id }) { index, choice ->
            val title = displayName(index)
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                if (choice.current) Text("Current working library")
                if (!choice.available) Text("This working copy is unavailable. Existing files are retained.")
                OutlinedButton(onClick = { actions.select(choice.id) },
                    enabled = !state.busy && !state.pending && !state.loading && choice.available,
                    modifier = Modifier.semantics {
                        contentDescription = if (choice.current) "Reopen working library: $title"
                            else "Use working library: $title"
                    }) {
                    Text(if (choice.current) "Reopen working library" else "Use this working library")
                }
                TextButton(onClick = { renameId = choice.id; draft = choice.label; requestedName = null },
                    enabled = !state.busy && !state.pending && !state.loading,
                    modifier = Modifier.semantics { contentDescription = "Rename working library: $title" }) { Text("Rename") }
            }
        }
    }
}

private fun workingBytes(value: Long) = String.format(Locale.ROOT, "%.1f MiB", value / (1024.0 * 1024.0))
