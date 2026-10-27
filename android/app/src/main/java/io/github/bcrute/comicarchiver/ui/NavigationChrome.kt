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
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppState

@Composable
internal fun SourceHeader(state: AppState, onSelect: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp, top = 4.dp, bottom = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Text("Comic Archiver", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
        if (state.sites.isNotEmpty()) Box {
            val current = state.sites.firstOrNull { it.key == state.site }
            TextButton(onClick = { expanded = true },
                enabled = !state.siteBusy && !state.libraryBusy && !state.signOutPending,
                modifier = Modifier.testTag("source-picker")) {
                Text(current?.name ?: state.site)
                Icon(Icons.Default.ArrowDropDown, contentDescription = "Choose source")
            }
            DropdownMenu(expanded, onDismissRequest = { expanded = false }) {
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
                label = { Text(candidate.label, maxLines = 2) },
                modifier = Modifier.testTag("nav-${candidate.name.lowercase()}"))
        }
    }
}
