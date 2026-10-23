package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Tapas sign-in entry. The screen owns no sign-in logic: it renders [SignInScreenUi]
 * (derived from the controller's `AppState`) and hands the single action to the caller.
 * The real sign-in surface is the WebView/session line's `SignInEngine`, which drives a
 * UI-owned WebView slotted in through [signInContent] by app assembly; this file never
 * constructs a WebView itself.
 */
@Composable
fun SignInScreen(
    state: SignInScreenUi,
    onStartSignIn: () -> Unit,
    signInContent: @Composable () -> Unit = {},
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(state.siteName, style = MaterialTheme.typography.headlineSmall)
        if (!state.busy && !state.restoring) Text(
            "Sign in with your ${state.siteName} account to save episodes you can access.",
            style = MaterialTheme.typography.bodyMedium,
        )
        state.origin?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        state.notice?.let { NoticeCard(it) }
        if (state.restoring) {
            Text("Checking saved sign-in…", style = MaterialTheme.typography.bodyMedium)
        }
        if (!state.busy && !state.restoring) {
            Button(onClick = onStartSignIn, enabled = state.controlsEnabled, modifier = Modifier.fillMaxWidth()) {
                Text("Sign in to ${state.siteName}")
            }
        }
        Spacer(Modifier.height(4.dp))
        // The WebView sign-in surface, mounted only while the controller reports a
        // sign-in in flight (Page/Settling → busy). An unloaded WebView mounted on the
        // entry screen paints blank and blanked the whole entry screen with it.
        // While mounted it owns the screen's remaining height: the site's mobile login
        // page puts its form below a large hero graphic, so a short fixed-height
        // surface kept the form below the fold behind the cookie banner.
        if (state.busy) {
            Box(Modifier.weight(1f).fillMaxWidth()) { signInContent() }
        }
    }
}

/** One plain-words notice plus its single recovery action; [onDismiss], when given,
 *  acknowledges the notice through the caller (the controller's openNotice action). */
@Composable
fun NoticeCard(
    notice: NoticeUi,
    modifier: Modifier = Modifier,
    onDismiss: (() -> Unit)? = null,
    showRecovery: Boolean = true,
) {
    val words = NoticeText.wordsFor(notice.issue)
    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(words.notice, style = MaterialTheme.typography.bodyMedium)
            if (showRecovery) Text(
                    words.recoveryAction,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            notice.detail?.let {
                Text(it, style = MaterialTheme.typography.bodySmall)
            }
            if (onDismiss != null) {
                TextButton(onClick = onDismiss) { Text("Got it") }
            }
        }
    }
}
