package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.worker.DownloadNotificationAccess
import io.github.bcrute.comicarchiver.words.say

/**
 * The desktop's dashboard on the phone: the saved library over every site first, then one card
 * for each site, one to a row. Each card carries its own button for what can be done now —
 * Sign in, Start, Continue or Pause — with Sign out, the site's speed and the episode types it
 * downloads. The screen owns no download policy: every press goes to the controller.
 */
@Composable
fun DownloadsScreen(
    state: DownloadsScreenUi,
    onSignIn: (String) -> Unit = {},
    onGo: (String) -> Unit = {},
    onPause: (String) -> Unit = {},
    onSignOut: (String) -> Unit = {},
    onSpeed: (String, String) -> Unit = { _, _ -> },
    onDownloadChoices: (String) -> Unit = {},
    onListAction: (String, String) -> Unit = { _, _ -> },
    onGoAll: () -> Unit = {},
    onPauseAll: () -> Unit = {},
    onOpenLibrary: () -> Unit = {},
    /** The working library's name and the way to change it; null where there is only the one. */
    libraryFolder: String? = null,
    onChangeLibrary: () -> Unit = {},
    onDismissNotice: (() -> Unit)? = null,
    notificationAccess: DownloadNotificationAccess = DownloadNotificationAccess.ENABLED,
    notificationError: String? = null,
    onNotificationAccess: () -> Unit = {},
) {
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).testTag("downloads-scroll"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Column(Modifier.fillMaxWidth().testTag("library-overview"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Row(Modifier.weight(1f).semantics(mergeDescendants = true) {},
                    verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(state.savedEpisodes ?: "—", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text(say("window.saved_episodes"), style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                }
                OutlinedButton(onClick = onOpenLibrary, modifier = Modifier.testTag("open-library")) {
                    Text(say("library.open_library_folder"), maxLines = 1)
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedButton(onClick = onGoAll, enabled = state.startAllEnabled,
                    modifier = Modifier.testTag("start-all")) { Text(say("downloads.start_all")) }
                if (state.pauseAllVisible) OutlinedButton(onClick = onPauseAll,
                    modifier = Modifier.testTag("pause-all")) { Text(say("downloads.pause_all")) }
            }
            if (libraryFolder != null) Row(verticalAlignment = Alignment.CenterVertically) {
                Text(say("window.library_folder", "root" to libraryFolder), Modifier.weight(1f, fill = false),
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                IconButton(onClick = onChangeLibrary) {
                    Icon(Icons.Default.Edit, contentDescription = say("window.change_library_folder_for_all_sites"))
                }
            }
        }
        state.notice?.let { NoticeCard(it, onDismiss = onDismissNotice) }
        state.cards.forEach { card ->
            SiteCardView(card, onSignIn, onGo, onPause, onSignOut, onSpeed, onDownloadChoices, onListAction)
        }
        Text(say("window.estimates_adjust_as_episodes_are_discovered"),
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (notificationAccess != DownloadNotificationAccess.ENABLED) {
            Text(notificationError ?: say("downloads.notifications_are_off_enable_them_to"),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            TextButton(onClick = onNotificationAccess) {
                Text(if (notificationAccess == DownloadNotificationAccess.ASK_PERMISSION)
                    say("downloads.allow_download_notifications") else say("downloads.notification_settings"))
            }
        }
    }
}

/** The desktop's card palettes, light and dark: the card takes the color of what its site is doing. */
internal data class CardColors(val surface: Color, val shade: Color, val accent: Color, val text: Color, val buttonText: Color)

private fun pair(light: Long, dark: Long, isDark: Boolean) = Color(if (isDark) dark else light)

internal fun cardColors(appearance: String, dark: Boolean): CardColors = when (appearance) {
    "active" -> CardColors(pair(0xFFFFF4EE, 0xFF573535, dark), pair(0xFFFFE6DC, 0xFF2B2227, dark),
        pair(0xFF914333, 0xFFFF9D88, dark), pair(0xFF402C29, 0xFFFFF0E1, dark), pair(0xFFFFFFFF, 0xFF32201A, dark))
    "complete" -> CardColors(pair(0xFFF0F8EB, 0xFF345439, dark), pair(0xFFE1EFD6, 0xFF1F2B23, dark),
        pair(0xFF315E32, 0xFFA1E39A, dark), pair(0xFF263E28, 0xFFF1F5E4, dark), pair(0xFFFFFFFF, 0xFF19311D, dark))
    "paused" -> CardColors(pair(0xFFF5F1FF, 0xFF514169, dark), pair(0xFFE7DFF8, 0xFF292536, dark),
        pair(0xFF5B438C, 0xFFBCB0FF, dark), pair(0xFF352C49, 0xFFF4EFFF, dark), pair(0xFFFFFFFF, 0xFF28203D, dark))
    "waiting" -> CardColors(pair(0xFFFFF7E5, 0xFF58462C, dark), pair(0xFFF5E6C7, 0xFF2C2720, dark),
        pair(0xFF805A17, 0xFFEFC373, dark), pair(0xFF49371D, 0xFFFFF1D2, dark), pair(0xFFFFFFFF, 0xFF382B19, dark))
    "attention" -> CardColors(pair(0xFFFFF0F0, 0xFF583235, dark), pair(0xFFFFDDDD, 0xFF302225, dark),
        pair(0xFF9D2432, 0xFFFF9FA3, dark), pair(0xFF4A2429, 0xFFFFEDE7, dark), pair(0xFFFFFFFF, 0xFF3A1D22, dark))
    else -> CardColors(pair(0xFFF7F4F0, 0xFF2E2B2E, dark), pair(0xFFEEE8E2, 0xFF252326, dark),
        pair(0xFF625650, 0xFFD0C3BC, dark), pair(0xFF352C2A, 0xFFF6ECDF, dark), pair(0xFFFFFFFF, 0xFF2A2422, dark))
}

@Composable
private fun SiteCardView(
    card: SiteCardUi,
    onSignIn: (String) -> Unit,
    onGo: (String) -> Unit,
    onPause: (String) -> Unit,
    onSignOut: (String) -> Unit,
    onSpeed: (String, String) -> Unit,
    onDownloadChoices: (String) -> Unit,
    onListAction: (String, String) -> Unit,
) {
    val c = cardColors(card.appearance, MaterialTheme.colorScheme.background.luminance() < 0.5f)
    val rule = c.text.copy(alpha = 0.27f)
    val soft = c.text.copy(alpha = 0.8f)
    val error = MaterialTheme.colorScheme.error
    Surface(Modifier.fillMaxWidth().testTag("site-card-${card.site}"), shape = RoundedCornerShape(14.dp),
        color = c.surface, contentColor = c.text, border = BorderStroke(1.dp, rule)) {
        Column(Modifier.padding(horizontal = 18.dp, vertical = 22.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(card.name, Modifier.weight(1f), style = MaterialTheme.typography.headlineSmall,
                    fontFamily = FontFamily.Serif, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(card.pill, Modifier.background(c.accent, RoundedCornerShape(8.dp)).padding(horizontal = 12.dp, vertical = 5.dp)
                    .semantics { liveRegion = LiveRegionMode.Polite },
                    color = c.buttonText, style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.Bold, maxLines = 1)
            }
            Column(Modifier.semantics(mergeDescendants = true) {}) {
                Text(card.saved, style = MaterialTheme.typography.displayMedium, fontFamily = FontFamily.Serif, fontWeight = FontWeight.Bold)
                Text(say("window.episodes_saved"), style = MaterialTheme.typography.titleMedium)
            }
            Row(Modifier.height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Estimate(say("window.est_time_left"), card.timeLeft, soft, Modifier.weight(1f))
                Box(Modifier.width(1.dp).fillMaxHeight().background(rule))
                Estimate(say("window.est_space_needed"), card.space, soft, Modifier.weight(1f))
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(rule))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(Modifier.weight(1f).semantics(mergeDescendants = true) {}, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(say("window.transfer_rate"))
                    Text(card.rate, fontWeight = FontWeight.Bold, maxLines = 1)
                }
                Box(Modifier.width(1.dp).height(26.dp).background(rule))
                SpeedChoice(card, c, rule, onSpeed)
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(rule))
            Text(card.status, color = if (card.statusWarning) error else c.text,
                modifier = Modifier.testTag("card-status-${card.site}"))
            if (card.message.isNotBlank()) Text(card.message, color = if (card.messageError) error else c.text,
                modifier = Modifier.testTag("card-message-${card.site}").semantics { liveRegion = LiveRegionMode.Polite })
            var menu by remember(card.site) { mutableStateOf(false) }
            Box {
            TextButton(onClick = { menu = true }, enabled = card.selectionEnabled,
                colors = ButtonDefaults.textButtonColors(contentColor = c.text),
                modifier = Modifier.testTag("card-types-${card.site}")
                    .semantics { contentDescription = say("window.download_choices_for", "name" to card.name) }) {
                Text(card.selection, fontWeight = FontWeight.Bold)
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                for ((key, label) in listOf("files" to "Episode files", "list" to "Episode list only",
                    "choose" to "Choose comics in Library", "clear" to "Clear episode list", "settings" to "Download settings…")) {
                    val enabled = when (key) {
                        "choose" -> card.hasEpisodes
                        "clear" -> card.hasList && card.action != CardAction.PAUSE
                        "settings" -> true
                        else -> card.action != CardAction.PAUSE
                    }
                    DropdownMenuItem(text = { Column {
                        Text(label)
                        if (key == "choose" && !card.hasEpisodes) Text("Select Episode list only, then press Start.",
                            style = MaterialTheme.typography.bodySmall)
                    } }, leadingIcon = if (key == (if (card.listOnly) "list" else "files")) ({ Text("✓") }) else null,
                        enabled = enabled, onClick = {
                            menu = false
                            if (key == "settings") onDownloadChoices(card.site) else onListAction(card.site, key)
                        })
                }
            }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = {
                        when (card.action) {
                            CardAction.SIGN_IN -> onSignIn(card.site)
                            CardAction.GO -> onGo(card.site)
                            CardAction.PAUSE -> onPause(card.site)
                        }
                    },
                    enabled = card.actionEnabled, shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = c.accent, contentColor = c.buttonText),
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).testTag("card-action-${card.site}"),
                ) { Text(card.actionText, fontWeight = FontWeight.Bold, maxLines = 1) }
                if (card.signOut) OutlinedButton(
                    onClick = { onSignOut(card.site) }, enabled = card.signOutEnabled, shape = RoundedCornerShape(10.dp),
                    border = BorderStroke(1.dp, rule), colors = ButtonDefaults.outlinedButtonColors(contentColor = c.text),
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).testTag("card-signout-${card.site}")
                        .semantics { contentDescription = say("app.sign_out_of_2", "name" to card.name) },
                ) { Text(say("app.sign_out"), maxLines = 1) }
            }
        }
    }
}

@Composable
private fun Estimate(caption: String, value: String, soft: Color, modifier: Modifier) {
    Column(modifier.semantics(mergeDescendants = true) {}) {
        Text(caption, style = MaterialTheme.typography.bodySmall, color = soft)
        Text(value, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
    }
}

/** The site's speed, changed here for this site alone, as on the desktop's card. */
@Composable
private fun SpeedChoice(card: SiteCardUi, c: CardColors, rule: Color, onSpeed: (String, String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        OutlinedButton(onClick = { open = true }, enabled = card.speedEnabled, shape = RoundedCornerShape(6.dp),
            border = BorderStroke(1.dp, rule), colors = ButtonDefaults.outlinedButtonColors(contentColor = c.text),
            modifier = Modifier.testTag("card-speed-${card.site}").semantics {
                contentDescription = if (card.speed.isBlank()) say("window.sign_in_to_to_choose_a", "name" to card.name)
                    else say("window.speed_on", "name" to card.name)
            }) {
            Text(card.speeds.firstOrNull { it.key == card.speed }?.label ?: "—", maxLines = 1)
            Icon(Icons.Default.ArrowDropDown, contentDescription = null)
        }
        DropdownMenu(open && card.speedEnabled, onDismissRequest = { open = false }) {
            card.speeds.forEach { speed ->
                DropdownMenuItem(text = { Text(speed.label) }, onClick = { open = false; onSpeed(card.site, speed.key) })
            }
        }
    }
}
