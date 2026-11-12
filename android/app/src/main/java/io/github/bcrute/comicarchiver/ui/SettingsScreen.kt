package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.DownloadSettings
import io.github.bcrute.comicarchiver.controller.DownloadTypeChoice
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SiteCard
import io.github.bcrute.comicarchiver.controller.AppTheme

/** Stored core policy, not local toggle state. No settings action admits a transfer. */
@Composable
fun SettingsScreen(
    settings: DownloadSettings?,
    busy: Boolean,
    saving: Boolean,
    notice: Notice?,
    onWifiOnly: (Boolean) -> Unit,
    onSpeed: (String) -> Unit,
    onRefresh: () -> Unit,
    theme: AppTheme = AppTheme.SYSTEM,
    onTheme: (AppTheme) -> Unit = {},
    card: SiteCard? = null,
    siteName: String = "Tapas",
    onDownloadTypes: (Map<String, Boolean>) -> Unit = {},
) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineSmall)
        // First on the page: what the shown site saved and what it could not.
        SiteCountsCard(card)
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Appearance", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    AppTheme.entries.forEach { choice ->
                        FilterChip(selected = choice == theme, onClick = { onTheme(choice) },
                            label = { Text(choice.label, maxLines = 1, softWrap = false) })
                    }
                }
            }
        }
        notice?.let { NoticeCard(it.toNoticeUi()) }
        if (settings == null) {
            Text(if (notice == null) "Loading download settings…" else "Download settings are unavailable.")
            if (notice == null) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            OutlinedButton(onClick = onRefresh) { Text("Retry settings") }
            return@Column
        }
        if (busy) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Text("Saving setting…")
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Connections", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Also save on metered and mobile connections", Modifier.weight(1f))
                    Switch(
                        checked = !settings.wifiOnly,
                        enabled = !busy,
                        onCheckedChange = { onWifiOnly(!it) },
                        modifier = Modifier.semantics {
                            contentDescription = "Also save on metered and mobile connections"
                        },
                    )
                }
                Text("Mobile data can incur charges. Changing this choice never starts a download.")
                val connection = when (settings.connection) {
                    "wifi" -> "Wi-Fi"
                    "wired" -> "wired"
                    "cellular" -> "mobile"
                    else -> "unverified"
                }
                val metering = when (settings.metered) {
                    true -> " · metered"
                    false -> " · unmetered"
                    null -> ""
                }
                Text("Current connection: $connection$metering")
                if (!settings.allowed) Text("Saving is held by your connection choice. Return to Downloads and start saving when ready.")
            }
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                // Another site's speed is chosen on that site, through the source list at the top.
                Text("Download speed on $siteName", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    settings.speeds.forEach { speed ->
                        FilterChip(
                            selected = speed.key == settings.speed,
                            enabled = speed.available && !busy,
                            onClick = { onSpeed(speed.key) },
                            label = { Text(speed.label, maxLines = 1, softWrap = false) },
                        )
                    }
                }
                Text(if (saving) "A running download pauses to apply the change, then continues."
                     else "The stored choice applies when you start saving. It never starts saving on its own.")
                if (settings.speeds.none { it.key == settings.speed && it.available })
                    Text("Your stored speed is unavailable. Choose Medium or Slow, or update Android System WebView before starting saving.")
                if (settings.speeds.any { it.key == "fast" && !it.available })
                    Text("Update Android System WebView to use Tapas Fast mode and Recent/Free Episodes discovery.")
            }
        }
        if (settings.downloadTypes.isNotEmpty())
            DownloadTypesCard(settings.downloadTypes, siteName, busy, onDownloadTypes)
    }
}

/** As the desktop's episode-type choices: nothing is saved until Save, and one type always stays chosen. */
@Composable
private fun DownloadTypesCard(types: List<DownloadTypeChoice>, siteName: String, busy: Boolean,
                              onSave: (Map<String, Boolean>) -> Unit) {
    val stored = types.associate { it.key to it.chosen }
    var draft by remember(stored) { mutableStateOf(stored) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Episode types on $siteName", style = MaterialTheme.typography.titleMedium)
            types.forEach { type ->
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Checkbox(
                        checked = draft[type.key] == true,
                        enabled = !busy,
                        onCheckedChange = { draft = draft + (type.key to it) },
                        modifier = Modifier.semantics { contentDescription = type.label },
                    )
                    Column(Modifier.weight(1f)) {
                        Text(type.label)
                        if (type.help.isNotBlank()) Text(type.help, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
            Text("Already saved files are kept. Choices also apply on future runs. " +
                 "A running download pauses to apply changes, then continues.")
            OutlinedButton(onClick = { onSave(draft) },
                enabled = !busy && draft != stored && draft.values.any { it }) { Text("Save episode types") }
        }
    }
}

/** As the desktop's Settings: saved and pending by episode type, then the site's download issues. */
@Composable
private fun SiteCountsCard(card: SiteCard?) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (card != null && card.types.isNotEmpty()) {
                Text("${card.name} episodes", style = MaterialTheme.typography.titleMedium)
                Column {
                    CountLine(0, listOf("Episode type", "Saved", "Pending"), heading = true)
                    card.types.forEachIndexed { index, type -> CountLine(index, listOf(type.label, type.saved, type.waiting)) }
                }
            }
            Text("Download issues", style = MaterialTheme.typography.titleMedium)
            if (card == null) Text("Not checked")
            else if (card.issues.isEmpty()) Text("No recorded issues")
            else Column {
                CountLine(0, listOf("Issue", "Episodes"), heading = true)
                card.issues.forEachIndexed { index, issue -> CountLine(index, listOf(issue.reason, issue.count)) }
            }
        }
    }
}

@Composable
private fun CountLine(index: Int, cells: List<String>, heading: Boolean = false) {
    // Alternate rows are shaded, so a name that wraps stays matched to its counts.
    val band = if (!heading && index % 2 == 0)
        Modifier.background(MaterialTheme.colorScheme.surfaceContainer, RoundedCornerShape(6.dp)) else Modifier
    Row(Modifier.fillMaxWidth().then(band).padding(horizontal = 8.dp, vertical = if (heading) 4.dp else 8.dp)
        .semantics(mergeDescendants = true) {}, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
        val style = if (heading) MaterialTheme.typography.labelLarge else MaterialTheme.typography.bodyLarge
        Text(cells[0], Modifier.weight(1f), style = style)
        cells.drop(1).forEach { Text(it, textAlign = TextAlign.End, style = style) }
    }
}
