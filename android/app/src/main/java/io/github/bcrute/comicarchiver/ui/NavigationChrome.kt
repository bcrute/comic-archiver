package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState

@Composable
internal fun SourceHeader(state: AppState, onSelect: (String) -> Unit, exporting: Boolean = false) {
    var expanded by remember { mutableStateOf(false) }
    // As on desktop, the site stays put while saving, signing in or another task runs.
    val saving = state.download is DownloadState.Running || state.download is DownloadState.Planning ||
        state.cloud?.taskRunning == true || exporting
    val working = saving || state.signIn is SignInState.Page || state.signIn is SignInState.Settling ||
        state.settingsBusy || state.cloudBusy || state.cloud?.authStatus == "running"
    val enabled = !working && !state.siteBusy && !state.libraryBusy && !state.signOutPending
    Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp, top = 4.dp, bottom = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Text("Comic Archiver", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
        if (state.sites.isNotEmpty()) Box {
            val current = state.sites.firstOrNull { it.key == state.site }
            TextButton(onClick = { expanded = true }, enabled = enabled,
                modifier = Modifier.testTag("source-picker").semantics {
                    if (saving) stateDescription = "Pause saving to change the site."
                }) {
                Text(current?.name ?: state.site)
                Icon(Icons.Default.ArrowDropDown, contentDescription = "Choose source")
            }
            DropdownMenu(expanded && enabled, onDismissRequest = { expanded = false }) {
                state.sites.forEach { site ->
                    DropdownMenuItem(enabled = site.enabled,
                        text = { Column { Text(site.name); site.reason?.let {
                            Text(it, style = MaterialTheme.typography.bodySmall)
                        } } },
                        onClick = { expanded = false; onSelect(site.key) })
                }
            }
        }
    }
}

@Composable
@OptIn(androidx.compose.ui.ExperimentalComposeUiApi::class)
internal fun MainNavigation(tab: AppTab, onSelect: (AppTab) -> Unit) {
    val largeText = LocalDensity.current.fontScale >= 1.5f
    // MainActivity already consumes system safeDrawing insets.
    NavigationBar(windowInsets = WindowInsets(0, 0, 0, 0),
        modifier = Modifier.semantics { testTagsAsResourceId = true }) {
        listOf(AppTab.SAVING, AppTab.LIBRARY, AppTab.SETTINGS).forEach { candidate ->
            val icon = when (candidate) {
                AppTab.SAVING -> Icons.Default.PlayArrow
                AppTab.LIBRARY -> Icons.AutoMirrored.Filled.List
                else -> Icons.Default.Settings
            }
            NavigationBarItem(selected = tab == candidate || tab == AppTab.ABOUT && candidate == AppTab.SETTINGS,
                onClick = { onSelect(candidate) },
                icon = { Icon(icon, contentDescription = null) },
                // Keep whole words at the user's font size. "Downloads" can
                // otherwise strand its final letter under a fixed-width item.
                label = { Text(if (largeText && candidate == AppTab.SAVING) "Save" else candidate.label, maxLines = 1) },
                modifier = Modifier.testTag("nav-${candidate.name.lowercase()}"))
        }
    }
}
