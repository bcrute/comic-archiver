package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
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
) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineSmall)
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
                if (!settings.allowed) Text("Saving is held by your connection choice. Press Go when ready.")
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
                     else "The stored choice applies when you press Go. It never starts saving on its own.")
                if (settings.speeds.none { it.key == settings.speed && it.available })
                    Text("Your stored speed is unavailable. Choose Medium or Slow, or update Android System WebView before pressing Go.")
                if (settings.speeds.any { it.key == "fast" && !it.available })
                    Text("Update Android System WebView to use Tapas Fast mode and Recent/Free Episodes discovery.")
            }
        }
    }
}
