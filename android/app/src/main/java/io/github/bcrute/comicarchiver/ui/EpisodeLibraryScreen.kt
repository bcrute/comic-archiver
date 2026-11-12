package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.EpisodeList
import io.github.bcrute.comicarchiver.controller.SiteChoice

/**
 * The desktop's Library page: recorded episodes by site, series and episode, each with its
 * status, format and access. A list is read only when its site or series is opened, and
 * only the library's own record is read: no saved file is opened or checked.
 */
@Composable
fun EpisodeLibraryScreen(sites: List<SiteChoice>, lists: Map<String, EpisodeList>,
                         onLoad: (String, String?) -> Unit, onRefresh: () -> Unit) {
    // Places opened on screen, by the same key as their lists.
    var opened by rememberSaveable { mutableStateOf(listOf<String>()) }
    fun toggle(site: String, seriesId: String?) {
        val key = EpisodeList.key(site, seriesId)
        if (key in opened) opened = opened - key
        else {
            opened = opened + key
            // Opening reads the list, and reads it again after a failure, as on the desktop.
            if (lists[key]?.loaded != true) onLoad(site, seriesId)
        }
    }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp).testTag("episode-library-list"),
        verticalArrangement = Arrangement.spacedBy(4.dp)) {
        item(key = "heading") {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Library", Modifier.weight(1f), style = MaterialTheme.typography.headlineSmall)
                    OutlinedButton(onClick = { opened = emptyList(); onRefresh() }) { Text("Refresh") }
                }
                Text("Recorded episodes · expand a provider and series", style = MaterialTheme.typography.bodySmall)
                Text("Saved can include cloud storage. This list does not check files on this device.",
                    style = MaterialTheme.typography.bodySmall)
            }
        }
        sites.filter { it.enabled }.forEach { site ->
            val siteKey = EpisodeList.key(site.key)
            val series = lists[siteKey]
            val siteOpen = siteKey in opened
            item(key = siteKey) {
                EpisodeLine(site.name, placeStatus(series, "Expand to view", "Recorded list"), null, 0, siteOpen,
                    "episodes-$siteKey") { toggle(site.key, null) }
            }
            if (!siteOpen || series == null) return@forEach
            series.rows.forEach { row ->
                val seriesKey = EpisodeList.key(site.key, row.id)
                val episodes = lists[seriesKey]
                val seriesOpen = seriesKey in opened
                item(key = seriesKey) {
                    EpisodeLine(row.title, placeStatus(episodes, row.status, row.status), "${row.format} · ${row.access}",
                        1, seriesOpen, "episodes-$seriesKey") { toggle(site.key, row.id) }
                }
                if (!seriesOpen || episodes == null) return@forEach
                episodes.rows.forEach { episode ->
                    item(key = "$seriesKey/${episode.id}") {
                        EpisodeLine(episode.title, episode.status, "${episode.format} · ${episode.access}", 2, null,
                            "episode-$seriesKey/${episode.id}", null)
                    }
                }
                more(episodes, "$seriesKey/more", "More episodes…", 2) { onLoad(site.key, row.id) }
            }
            more(series, "$siteKey/more", "More series…", 1) { onLoad(site.key, null) }
        }
    }
}

/** The desktop's second column for a site or a series: what it says before, while and after its list is read. */
private fun placeStatus(list: EpisodeList?, unread: String, read: String): String = when {
    list?.loading == true -> "Loading…"
    list?.failed == true -> "Could not read · retry"
    list?.loaded != true -> unread
    list.rows.isEmpty() -> list.empty.ifBlank { read }
    else -> read
}

private fun LazyListScope.more(list: EpisodeList, key: String, label: String, depth: Int, onMore: () -> Unit) {
    if (list.nextOffset == null) return
    item(key = key) {
        TextButton(onClick = onMore, enabled = !list.loading,
            modifier = Modifier.padding(start = (16 * depth).dp).testTag(key)) {
            Text(if (list.loading) "Loading…" else if (list.failed) "Could not read · retry" else label)
        }
    }
}

/** One line of the tree. A site or a series opens and closes; an episode only says what is recorded. */
@Composable
private fun EpisodeLine(title: String, status: String, detail: String?, depth: Int, open: Boolean?, tag: String,
                        onToggle: (() -> Unit)?) {
    val place = if (onToggle == null) Modifier else Modifier
        .clickable(role = Role.Button, onClick = onToggle)
        .semantics { stateDescription = if (open == true) "Expanded" else "Collapsed" }
    Row(Modifier.fillMaxWidth().then(place).padding(start = (16 * depth).dp, top = 8.dp, bottom = 8.dp).testTag(tag),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        if (open != null) Text(if (open) "▾" else "▸")
        Column(Modifier.weight(1f)) {
            Text(title, maxLines = 2, overflow = TextOverflow.Ellipsis,
                style = if (depth == 0) MaterialTheme.typography.titleMedium else MaterialTheme.typography.bodyLarge)
            if (detail != null) Text(detail, style = MaterialTheme.typography.bodySmall)
        }
        Text(status, style = MaterialTheme.typography.bodyMedium)
    }
}
