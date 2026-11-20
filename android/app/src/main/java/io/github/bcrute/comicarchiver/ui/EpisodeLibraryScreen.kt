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
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.EpisodeList
import io.github.bcrute.comicarchiver.controller.SiteChoice
import io.github.bcrute.comicarchiver.words.say

/**
 * The desktop's Library page: recorded episodes by site, series and episode, each with its
 * status, format and access. A list is read only when its site or series is opened, and
 * only the library's own record is read: no saved file is opened or checked.
 */
@Composable
fun EpisodeLibraryScreen(sites: List<SiteChoice>, lists: Map<String, EpisodeList>,
                         onLoad: (String, String?) -> Unit, onRefresh: () -> Unit,
                         onChooseComics: (Boolean, String?, String?) -> Unit = { _, _, _ -> },
                         changing: Boolean = false, notice: String? = null) {
    // Places opened on screen, by the same key as their lists.
    var opened by rememberSaveable { mutableStateOf(listOf<String>()) }
    LaunchedEffect(opened, lists, changing) {
        if (!changing) opened.filter { lists[it]?.let { value -> !value.loaded && !value.loading && !value.failed } != false }.forEach { key ->
            val parts = key.split('/', limit = 2)
            onLoad(parts[0], parts.getOrNull(1))
        }
    }
    fun toggle(site: String, seriesId: String?) {
        if (changing) return
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
                    Text(say("app.library"), Modifier.weight(1f), style = MaterialTheme.typography.headlineSmall)
                    OutlinedButton(onClick = { opened = emptyList(); onRefresh() }, enabled = !changing) { Text(say("library.refresh")) }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { onChooseComics(true, null, null) }, enabled = !changing,
                        modifier = Modifier.testTag("comics-check-all")) { Text("Check all") }
                    OutlinedButton(onClick = { onChooseComics(false, null, null) }, enabled = !changing,
                        modifier = Modifier.testTag("comics-uncheck-all")) { Text("Uncheck all") }
                }
                Text("Choose which comics to download. Unchecking keeps saved files.", style = MaterialTheme.typography.bodySmall)
                if (notice != null) Text(notice, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("comic-selection-notice"))
                Text(say("library.recorded_episodes_expand_a_provider_and"), style = MaterialTheme.typography.bodySmall)
                Text(say("library.saved_can_include_cloud_storage_this"),
                    style = MaterialTheme.typography.bodySmall)
            }
        }
        sites.filter { it.enabled }.forEach { site ->
            val siteKey = EpisodeList.key(site.key)
            val series = lists[siteKey]
            val siteOpen = siteKey in opened
            item(key = siteKey) {
                EpisodeLine(site.name, placeStatus(series, say("library.expand_to_view"), say("library.recorded_list")), null, 0, siteOpen,
                    "episodes-$siteKey", onToggle = { toggle(site.key, null) })
            }
            if (!siteOpen || series == null) return@forEach
            series.rows.forEach { row ->
                val seriesKey = EpisodeList.key(site.key, row.id)
                val episodes = lists[seriesKey]
                val seriesOpen = seriesKey in opened
                item(key = seriesKey) {
                    EpisodeLine(row.title, placeStatus(episodes, row.status, row.status), "${row.format} · ${row.access}",
                        1, seriesOpen, "episodes-$seriesKey", onToggle = { toggle(site.key, row.id) },
                        selected = row.selected, enabled = !changing,
                        onSelected = { onChooseComics(it, site.key, row.id) })
                }
                if (!seriesOpen || episodes == null) return@forEach
                episodes.rows.forEach { episode ->
                    item(key = "$seriesKey/${episode.id}") {
                        EpisodeLine(episode.title, episode.status, "${episode.format} · ${episode.access}", 2, null,
                            "episode-$seriesKey/${episode.id}", null)
                    }
                }
                more(episodes, "$seriesKey/more", say("library.more_episodes"), 2) { onLoad(site.key, row.id) }
            }
            more(series, "$siteKey/more", say("library.more_series"), 1) { onLoad(site.key, null) }
        }
    }
}

/** The desktop's second column for a site or a series: what it says before, while and after its list is read. */
private fun placeStatus(list: EpisodeList?, unread: String, read: String): String = when {
    list?.loading == true -> say("library.loading")
    list?.failed == true -> say("library.could_not_read_retry")
    list?.loaded != true -> unread
    list.rows.isEmpty() -> list.empty.ifBlank { read }
    else -> read
}

private fun LazyListScope.more(list: EpisodeList, key: String, label: String, depth: Int, onMore: () -> Unit) {
    if (list.nextOffset == null) return
    item(key = key) {
        TextButton(onClick = onMore, enabled = !list.loading,
            modifier = Modifier.padding(start = (16 * depth).dp).testTag(key)) {
            Text(if (list.loading) say("library.loading") else if (list.failed) say("library.could_not_read_retry") else label)
        }
    }
}

/** One line of the tree. A site or a series opens and closes; an episode only says what is recorded. */
@Composable
private fun EpisodeLine(title: String, status: String, detail: String?, depth: Int, open: Boolean?, tag: String,
                        onToggle: (() -> Unit)?, selected: Boolean? = null, enabled: Boolean = true,
                        onSelected: (Boolean) -> Unit = {}) {
    val place = if (onToggle == null) Modifier else Modifier
        .clickable(role = Role.Button, onClick = onToggle)
        .semantics { stateDescription = if (open == true) say("library.expanded") else say("library.collapsed") }
    Row(Modifier.fillMaxWidth().then(place).padding(start = (16 * depth).dp, top = 8.dp, bottom = 8.dp).testTag(tag),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        if (selected != null) Checkbox(checked = selected, onCheckedChange = onSelected, enabled = enabled,
            modifier = Modifier.testTag("select-$tag").semantics { contentDescription = "Download $title" })
        if (open != null) Text(if (open) "▾" else "▸")
        Column(Modifier.weight(1f)) {
            Text(title, maxLines = 2, overflow = TextOverflow.Ellipsis,
                style = if (depth == 0) MaterialTheme.typography.titleMedium else MaterialTheme.typography.bodyLarge)
            if (detail != null) Text(detail, style = MaterialTheme.typography.bodySmall)
        }
        Text(status, style = MaterialTheme.typography.bodyMedium)
    }
}
