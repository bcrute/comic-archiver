package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Tapas sign-in entry. The screen owns no sign-in logic: it renders the controller's
 * signed-out shape and hands the single action to the caller. The real sign-in surface is
 * the WebView/session line's engine, slotted in through [signInContent] by app assembly;
 * this file never constructs a WebView itself.
 */
@Composable
fun SignInScreen(
    busy: Boolean,
    notice: NoticeUi?,
    onStartSignIn: () -> Unit,
    signInContent: @Composable () -> Unit = {},
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Tapas", style = MaterialTheme.typography.headlineSmall)
        Text(
            "Sign in with your Tapas account to save the comics and novels you bought " +
                "before the site closes.",
            style = MaterialTheme.typography.bodyMedium,
        )
        notice?.let { NoticeCard(it) }
        if (!busy) {
            Button(onClick = onStartSignIn, modifier = Modifier.fillMaxWidth()) {
                Text("Sign in to Tapas")
            }
        }
        Spacer(Modifier.height(4.dp))
        // The WebView sign-in surface, when a sign-in is in flight.
        signInContent()
    }
}

/** One plain-words notice plus its single recovery action. */
@Composable
fun NoticeCard(notice: NoticeUi, modifier: Modifier = Modifier) {
    val words = NoticeText.wordsFor(notice.issue)
    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(words.notice, style = MaterialTheme.typography.bodyMedium)
            Text(
                words.recoveryAction,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
            )
            notice.detail?.let {
                Text(it, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
