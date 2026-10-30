package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.DownloadSettings
import io.github.bcrute.comicarchiver.controller.Notice
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
    onSiteSpeed: (String, String) -> Unit = { _, _ -> },
    notDownloaded: Map<String, Long>? = null,
    siteName: String = "Tapas",
) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineSmall)
        // First on the page: which checked episodes were not downloaded, and why.
        notDownloaded?.let { NotDownloadedCard(it, siteName) }
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
                Text("Download speed", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    settings.speeds.forEach { speed ->
                        FilterChip(
                            selected = speed.key == settings.speed,
                            enabled = speed.available && !busy && !saving,
                            onClick = { onSpeed(speed.key) },
                            label = { Text(speed.label, maxLines = 1, softWrap = false) },
                        )
                    }
                }
                Text(if (saving) "Pause saving to change the speed."
                     else "The stored choice applies when you start saving. It never starts saving on its own.")
                if (settings.speeds.none { it.key == settings.speed && it.available })
                    Text("Your stored speed is unavailable. Choose Medium or Slow, or update Android System WebView before starting saving.")
                if (settings.speeds.any { it.key == "fast" && !it.available })
                    Text("Update Android System WebView to use Tapas Fast mode and Recent/Free Episodes discovery.")
            }
        }
        if (settings.otherSites.isNotEmpty()) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Other sites", style = MaterialTheme.typography.titleMedium)
                Text("Save a speed choice without switching sites or starting a download. It applies the next time you start saving on that site.")
                settings.otherSites.forEach { site ->
                    var expanded by rememberSaveable(site.key) { mutableStateOf(false) }
                    val label = site.speeds.firstOrNull { it.key == site.speed }?.label ?: "Unavailable"
                    Box {
                        OutlinedButton(onClick = { expanded = true }, enabled = !busy && site.enabled,
                            modifier = Modifier.fillMaxWidth()) { Text("${site.name} speed: $label") }
                        DropdownMenu(expanded = expanded && !busy && site.enabled,
                            onDismissRequest = { expanded = false }) {
                            site.speeds.forEach { choice ->
                                DropdownMenuItem(enabled = choice.available, text = { Text(choice.label) },
                                    modifier = Modifier.semantics { contentDescription = "${choice.label} speed on ${site.name}" },
                                    onClick = { expanded = false; onSiteSpeed(site.key, choice.key) })
                            }
                        }
                    }
                    site.notice?.let { Text(it) }
                }
            }
        }
    }
}

/** Why a checked episode was not downloaded, as Settings words each reason. */
fun notDownloadedReason(key: String, site: String): String = when (key) {
    "locked" -> "Locked on your $site account. Buy or unlock them on $site, then start saving again"
    "scheduled" -> "Not released yet"
    "temporary_access" -> "Time-limited, with access the app couldn’t confirm is still current"
    "novel" -> "Novels this site’s download doesn’t support"
    "unsupported" -> "In a form the app can’t save yet"
    "failed" -> "Stopped by download errors"
    else -> error("Unknown reason $key")
}

@Composable
private fun NotDownloadedCard(counts: Map<String, Long>, site: String) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Not downloaded", style = MaterialTheme.typography.titleMedium)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                Text("Reason", Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
                Text("Episodes", style = MaterialTheme.typography.labelLarge)
            }
            counts.forEach { (key, count) ->
                val reason = notDownloadedReason(key, site)
                val episodes = "%,d".format(count)
                Row(Modifier.fillMaxWidth().semantics(mergeDescendants = true) {},
                    horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                    Text(reason, Modifier.weight(1f))
                    Text(episodes, textAlign = TextAlign.End)
                }
            }
        }
    }
}
