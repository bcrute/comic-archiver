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
import io.github.bcrute.comicarchiver.words.say

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
        Text(say("app.settings"), style = MaterialTheme.typography.headlineSmall)
        // First on the page: what the shown site saved and what it could not.
        SiteCountsCard(card)
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(say("settings.appearance"), style = MaterialTheme.typography.titleMedium)
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
            Text(if (notice == null) say("settings.loading_download_settings") else say("settings.download_settings_are_unavailable"))
            if (notice == null) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            OutlinedButton(onClick = onRefresh) { Text(say("settings.retry_settings")) }
            return@Column
        }
        if (busy) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Text(say("settings.saving_setting"))
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(say("settings.connections"), style = MaterialTheme.typography.titleMedium)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(say("settings.also_save_on_metered_and_mobile"), Modifier.weight(1f))
                    Switch(
                        checked = !settings.wifiOnly,
                        enabled = !busy,
                        onCheckedChange = { onWifiOnly(!it) },
                        modifier = Modifier.semantics {
                            contentDescription = say("settings.also_save_on_metered_and_mobile")
                        },
                    )
                }
                Text(say("settings.mobile_data_can_incur_charges_changing"))
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
                Text(say("settings.current_connection", "connection" to connection, "metering" to metering))
                if (!settings.allowed) Text(say("settings.saving_is_held_by_your_connection"))
            }
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                // Another site's speed is chosen on that site, through the source list at the top.
                Text(say("settings.download_speed_on", "siteName" to siteName), style = MaterialTheme.typography.titleMedium)
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
                Text(if (saving) say("settings.a_running_download_pauses_to_apply")
                     else say("settings.the_stored_choice_applies_when_you"))
                if (settings.speeds.none { it.key == settings.speed && it.available })
                    Text(say("settings.your_stored_speed_is_unavailable_choose"))
                if (settings.speeds.any { it.key == "fast" && !it.available })
                    Text(say("settings.update_android_system_webview_to_save"))
            }
        }
        if (settings.downloadTypes.isNotEmpty())
            DownloadTypesCard(settings.downloadTypes, settings.downloadExtras, siteName, busy, onDownloadTypes)
    }
}

/** As the desktop's episode-type choices: nothing is saved until Save, and one type always stays chosen. */
@Composable
private fun DownloadTypesCard(types: List<DownloadTypeChoice>, extras: List<DownloadTypeChoice>, siteName: String,
                              busy: Boolean, onSave: (Map<String, Boolean>) -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(say("settings.episode_types_on", "siteName" to siteName), style = MaterialTheme.typography.titleMedium)
            DownloadTypeChoices(types, busy, onSave, extras)
        }
    }
}

/** The episode types, then the extras the site shows, and their one Save: for Settings and a dashboard card. */
@Composable
internal fun DownloadTypeChoices(types: List<DownloadTypeChoice>, busy: Boolean, onSave: (Map<String, Boolean>) -> Unit,
                                 extras: List<DownloadTypeChoice> = emptyList()) {
    val stored = (types + extras).associate { it.key to it.chosen }
    var draft by remember(stored) { mutableStateOf(stored) }
    @Composable
    fun Choice(type: DownloadTypeChoice) {
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
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        types.forEach { Choice(it) }
        Text(say("settings.already_saved_files_are_kept_choices"))
        if (extras.isNotEmpty()) {
            Text(say("settings.also_save"), style = MaterialTheme.typography.titleSmall)
            extras.forEach { Choice(it) }
            Text(say("settings.extras_are_kept"), style = MaterialTheme.typography.bodySmall)
        }
        // One episode type always stays chosen; the extras may all be off.
        OutlinedButton(onClick = { onSave(draft) },
            enabled = !busy && draft != stored && types.any { draft[it.key] == true }) { Text(say("settings.save_choices")) }
    }
}

/** As the desktop's Settings: saved and pending by episode type, then the site's download issues. */
@Composable
private fun SiteCountsCard(card: SiteCard?) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (card != null && card.types.isNotEmpty()) {
                Text(say("settings.episodes", "name" to card.name), style = MaterialTheme.typography.titleMedium)
                Column {
                    CountLine(0, listOf(say("settings.episode_type"), say("settings.saved"), say("settings.pending")), heading = true)
                    card.types.forEachIndexed { index, type -> CountLine(index, listOf(type.label, type.saved, type.waiting)) }
                }
            }
            Text(say("settings.download_issues"), style = MaterialTheme.typography.titleMedium)
            if (card == null) Text(say("settings.not_checked"))
            else if (card.issues.isEmpty()) Text(say("settings.no_recorded_issues"))
            else Column {
                CountLine(0, listOf(say("settings.issue"), say("settings.episodes_2")), heading = true)
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
