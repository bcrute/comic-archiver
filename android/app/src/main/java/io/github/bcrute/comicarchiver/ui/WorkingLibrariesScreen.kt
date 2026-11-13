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
import io.github.bcrute.comicarchiver.words.say

data class WorkingLibraryActions(val load: () -> Unit = {}, val pick: () -> Unit = {},
    val confirm: (Boolean) -> Unit = {}, val stop: () -> Unit = {}, val discard: () -> Unit = {},
    val retry: () -> Unit = {}, val select: (String) -> Unit = {}, val refresh: () -> Unit = {},
    val rename: (String, String) -> Unit = { _, _ -> })

@Composable
fun WorkingLibrariesScreen(state: WorkingLibrariesState, actions: WorkingLibraryActions) {
    LaunchedEffect(Unit) { actions.load() }
    val names = state.choices.groupingBy { it.label.lowercase(Locale.ROOT) }.eachCount()
    fun displayName(index: Int): String = state.choices[index].let {
        if ((names[it.label.lowercase(Locale.ROOT)] ?: 0) > 1) say("libraries.working_copy", "label" to it.label, "value" to (index + 1)) else it.label
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
            title = { Text(say("libraries.rename_library")) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(displayName(renameIndex))
                    Text(say("libraries.choose_a_name_for_this_working"))
                    OutlinedTextField(value = draft, onValueChange = { draft = it.take(120); requestedName = null },
                        label = { Text(say("libraries.library_name")) }, singleLine = true, enabled = !state.loading,
                        isError = duplicate, modifier = Modifier.fillMaxWidth())
                    if (duplicate) Text(say("libraries.another_working_library_already_uses_that"))
                    if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                    else if (requestedName != null) state.message?.let { Text(it) }
                }
            },
            confirmButton = {
                TextButton(enabled = valid && !state.loading && !state.busy && !state.pending,
                    onClick = { requestedName = label; actions.rename(choice.id, label) }) { Text(say("libraries.save_name")) }
            },
            dismissButton = {
                TextButton(enabled = !state.loading, onClick = { renameId = null; requestedName = null }) { Text(say("app.cancel")) }
            },
        )
    }
    LazyColumn(Modifier.fillMaxSize().padding(16.dp).testTag("working-libraries"),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Text(say("cloud.working_libraries"), style = MaterialTheme.typography.headlineSmall)
            Text(say("libraries.choose_the_library_used_for_saving"))
            Text(say("libraries.folder_browsing_in_library_is_separate"))
        }
        state.message?.let { item { Text(it) } }
        if (state.loading || state.busy && state.phase != WorkingLibraryPhase.PREVIEW) item {
            LinearProgressIndicator(Modifier.fillMaxWidth())
            Text(when (state.phase) {
                WorkingLibraryPhase.CHOOSING -> say("libraries.choose_a_folder_in_android")
                WorkingLibraryPhase.SCANNING -> say("libraries.checking_the_selected_folder")
                WorkingLibraryPhase.CHECKING_COPY -> say("libraries.checking_retained_copies")
                WorkingLibraryPhase.COPYING -> say("libraries.copying_library_files")
                WorkingLibraryPhase.CHECKING_SOURCE -> say("libraries.checking_that_the_source_stayed_unchanged")
                WorkingLibraryPhase.VERIFYING -> say("libraries.verifying_saved_files_and_account_binding")
                WorkingLibraryPhase.SELECTING -> say("libraries.opening_the_working_library")
                WorkingLibraryPhase.STOPPING -> say("libraries.stopping_the_copy_safely")
                else -> say("libraries.loading_library_choices")
            })
            if (state.phase in setOf(WorkingLibraryPhase.CHECKING_COPY, WorkingLibraryPhase.COPYING, WorkingLibraryPhase.CHECKING_SOURCE)) {
                Text(say("libraries.of_files_read", "filesDone" to state.filesDone, "filesTotal" to state.filesTotal, "value" to (workingBytes(state.bytesRead))))
            }
            if (state.phase !in setOf(WorkingLibraryPhase.SELECTING, WorkingLibraryPhase.STOPPING) && !state.loading)
                OutlinedButton(onClick = actions.stop) { Text(if (state.phase == WorkingLibraryPhase.CHOOSING) say("libraries.cancel_folder_choice") else say("libraries.stop_copy")) }
        }
        state.preview?.takeIf { state.phase == WorkingLibraryPhase.PREVIEW }?.let { preview -> item {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(preview.label, style = MaterialTheme.typography.titleMedium)
                val size = workingBytes(preview.knownBytes)
                Text(if (preview.unknownSizes > 0) say("libraries.files_to_check_plus_files_with", "files" to preview.files, "size" to size) else say("libraries.files_to_check", "files" to preview.files, "size" to size))
                Text(say("libraries.the_source_stays_unchanged_this_phone"))
                Text(say("libraries.new_downloads_stay_in_this_working"))
                if (preview.empty) Text(say("libraries.this_location_is_empty_starting_saving"))
                if (preview.refreshedCopy || preview.existingCopy) Text(say("libraries.refreshing_creates_a_new_copy_from"))
                if (state.pending) Text(say("libraries.continue_the_held_capture_refresh_from"))
                Button(onClick = { actions.confirm(preview.refreshedCopy) }) {
                    Text(if (preview.existingCopy && !preview.refreshedCopy && !state.pending) say("libraries.open_saved_working_copy")
                         else if (state.pending) say("libraries.continue_held_copy") else say("libraries.copy_verify_and_use"))
                }
                if (preview.existingCopy && !preview.refreshedCopy && !state.pending)
                    OutlinedButton(onClick = { actions.confirm(true) }) { Text(say("libraries.create_refreshed_copy")) }
                OutlinedButton(onClick = actions.discard) { Text(if (state.pending) say("libraries.discard_held_copy") else say("app.cancel")) }
            }
        } }
        if (state.pending && !state.busy) item {
            Text(state.pendingLabel?.let { say("libraries.copy_is_held_the_current_library", "it" to it) } ?: say("libraries.library_copy_is_held_the_current"))
            Button(onClick = actions.retry) { Text(if (state.phase == WorkingLibraryPhase.FAILED) say("libraries.retry_library_copy") else say("libraries.continue_library_copy")) }
            OutlinedButton(onClick = actions.pick) { Text(say("libraries.choose_source_folder_again")) }
            OutlinedButton(onClick = actions.discard) { Text(say("libraries.discard_held_copy")) }
        }
        if (!state.pending && !state.busy && !state.loading) item {
            Button(onClick = actions.pick, modifier = Modifier.fillMaxWidth()) { Text(say("libraries.choose_working_library_folder")) }
            if (state.choices.any { it.current && it.hasSource })
                OutlinedButton(onClick = actions.refresh, modifier = Modifier.fillMaxWidth()) { Text(say("libraries.refresh_from_source_folder")) }
            if (state.phase == WorkingLibraryPhase.FAILED)
                OutlinedButton(onClick = actions.load) { Text(say("libraries.reload_library_choices")) }
        }
        itemsIndexed(state.choices, key = { _, choice -> choice.id }) { index, choice ->
            val title = displayName(index)
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                if (choice.current) Text(say("libraries.current_working_library"))
                if (!choice.available) Text(say("libraries.this_working_copy_is_unavailable_existing"))
                OutlinedButton(onClick = { actions.select(choice.id) },
                    enabled = !state.busy && !state.pending && !state.loading && choice.available,
                    modifier = Modifier.semantics {
                        contentDescription = if (choice.current) say("libraries.reopen_working_library", "title" to title)
                            else say("libraries.use_working_library", "title" to title)
                    }) {
                    Text(if (choice.current) say("libraries.reopen_working_library_2") else say("libraries.use_this_working_library"))
                }
                TextButton(onClick = { renameId = choice.id; draft = choice.label; requestedName = null },
                    enabled = !state.busy && !state.pending && !state.loading,
                    modifier = Modifier.semantics { contentDescription = say("libraries.rename_working_library", "title" to title) }) { Text(say("libraries.rename")) }
            }
        }
    }
}

private fun workingBytes(value: Long) = String.format(Locale.ROOT, "%.1f MiB", value / (1024.0 * 1024.0))
