package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.SupportNote
import io.github.bcrute.comicarchiver.controller.SupportWords
import io.github.bcrute.comicarchiver.words.say

/** Settings' Support page, as on the desktop: one button that emails support with the app's log, and the subreddit. */
@Composable
fun SupportScreen(modifier: Modifier = Modifier, supportNote: SupportNote? = null, supportBusy: Boolean = false,
                  onEmailSupport: () -> Unit = {}) {
    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        val browser = LocalUriHandler.current
        var subredditUnavailable by remember { mutableStateOf(false) }
        Text(say("support.need_help"), style = MaterialTheme.typography.headlineSmall)
        Text(say("support.email_support_does_phone", "address" to SupportWords.ADDRESS))
        Text(say("support.what_the_log_holds"), style = MaterialTheme.typography.bodySmall)
        Button(onClick = onEmailSupport, enabled = !supportBusy,
            modifier = Modifier.fillMaxWidth().testTag("support-email")) { Text(say("support.email_support")) }
        supportNote?.let {
            Text(it.text, modifier = Modifier.testTag("support-note"),
                color = if (it.failed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
        }
        SelectionContainer {
            Text(say("support.no_email_app_phone", "address" to SupportWords.ADDRESS),
                style = MaterialTheme.typography.bodySmall)
        }
        Text(say("support.community"))
        TextButton(onClick = {
            subredditUnavailable = try {
                browser.openUri(say("support.subreddit"))
                false
            } catch (_: Exception) {
                true
            }
        }, modifier = Modifier.testTag("support-subreddit")) { Text(say("support.open_the_subreddit")) }
        if (subredditUnavailable) Text(say("support.could_not_open_the_subreddit"))
    }
}
