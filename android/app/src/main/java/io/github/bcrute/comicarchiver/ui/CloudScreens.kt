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
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.AppTheme
import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.CloudStatus
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState

/** Fixed public destinations only. Site sessions never enter these browser intents. */
internal enum class B2Page(val label: String, val url: String) {
    ACCOUNT("Create B2 account", "https://www.backblaze.com/sign-up/s3"),
    SIGN_IN("Sign in to Backblaze", "https://secure.backblaze.com/user_signin.htm"),
    BILLING("Open Billing", "https://secure.backblaze.com/billing.htm"),
    CAPS("Open Caps and Alerts", "https://secure.backblaze.com/b2_caps_alerts.htm"),
    BUCKETS("Open Buckets", "https://secure.backblaze.com/b2_buckets.htm"),
    KEYS("Open Application Keys", "https://secure.backblaze.com/app_keys.htm"),
    BROWSE("Open in Backblaze", "https://secure.backblaze.com/b2_browse_files2.htm"),
    REPORTS("Open Reports", "https://secure.backblaze.com/b2_reports.htm"),
    PRICING("Current Backblaze prices", "https://www.backblaze.com/cloud-storage/pricing"),
}

@Composable
fun SettingsPages(state: AppState, onTheme: (AppTheme) -> Unit, onWifiOnly: (Boolean) -> Unit,
                  onSpeed: (String) -> Unit, onRefresh: () -> Unit,
                  onCloudRefresh: () -> Unit, onCloudAction: (CloudRequest) -> Unit) {
    var page by rememberSaveable { mutableStateOf("Downloads") }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("Downloads", "Storage", "B2 setup").forEach { name ->
                FilterChip(selected = page == name, onClick = {
                    page = name
                    if (name != "Downloads") onCloudRefresh()
                }, label = { Text(name, maxLines = 1, softWrap = false) })
            }
        }
        when (page) {
            "Downloads" -> SettingsScreen(state.settings, state.settingsBusy,
                state.download is DownloadState.Running || state.download is DownloadState.Planning,
                state.settingsNotice, onWifiOnly, onSpeed, onRefresh, state.theme, onTheme)
            else -> {
                val cloud = state.cloud
                if (cloud == null) {
                    Column(Modifier.padding(16.dp)) {
                        Text(state.cloudNotice?.message ?: "Loading cloud settings…")
                        OutlinedButton(onClick = onCloudRefresh) { Text("Refresh cloud settings") }
                    }
                } else {
                    val open = LocalUriHandler.current
                    CloudScreen(cloud, state.cloudBusy, state.cloudNotice?.message,
                        page == "B2 setup", state.signIn is SignInState.SignedIn,
                        onCloudRefresh, onCloudAction, { open.openUri(it.url) })
                }
            }
        }
    }
}

@Composable
internal fun CloudScreen(state: CloudStatus, busy: Boolean, notice: String?, setup: Boolean,
                         signedIn: Boolean, onRefresh: () -> Unit,
                         onAction: (CloudRequest) -> Unit, onPage: (B2Page) -> Unit) {
    // Key text deliberately uses remember, never rememberSaveable. Leaving setup,
    // recreation and process death discard it; AppState contains no credentials.
    var bucket by remember { mutableStateOf(state.suggestion) }
    var keyId by remember { mutableStateOf("") }
    var applicationKey by remember { mutableStateOf("") }
    var showSteps by rememberSaveable { mutableStateOf(false) }
    var showCost by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(state.authStatus, setup) {
        if (state.authStatus == "connected" || !setup) {
            keyId = ""; applicationKey = ""
            if (state.bucket.isNotBlank()) bucket = state.bucket
        }
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)
        .testTag("cloud-screen"), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(if (setup) "Backblaze B2" else "Storage", style = MaterialTheme.typography.headlineSmall)
        OutlinedButton(onClick = onRefresh, enabled = !busy) { Text("Refresh cloud settings") }
        if (busy || state.authStatus == "running" || (state.taskRunning && !state.taskHeld)) {
            LinearProgressIndicator(Modifier.fillMaxWidth())
            Text(if (state.taskRunning) "Cloud task running…" else "Updating cloud settings…")
        }
        notice?.let { Text(it) }
        if (state.message.isNotBlank()) Text(state.message)
        if (state.taskHeld) {
            Text("Cloud task held. Nothing restarts until you continue it.")
            Button(enabled = signedIn && !busy, onClick = { onAction(CloudRequest.Resume) }) {
                Text("Continue cloud task")
            }
        }
        Text(state.title, style = MaterialTheme.typography.titleMedium)
        Text(state.explanation)
        Text(state.status)
        if (state.auth.isNotBlank()) Text(state.auth)
        if (state.repair.isNotBlank()) Text(state.repair)
        if (!setup) {
            Text("Store new episodes on", style = MaterialTheme.typography.titleMedium)
            if (state.destinations.isEmpty()) Text("New episodes stay on this device. Connect and test B2 to offer another destination.")
            state.destinations.forEach { destination ->
                FilterChip(selected = destination.checked, enabled = destination.enabled && !busy,
                    onClick = { onAction(CloudRequest.Destination(destination.key)) },
                    label = { Text(destination.label) })
            }
            Text(state.destinationHelp)
            if (state.destinationLock.isNotBlank()) Text(state.destinationLock)
            Text("This device: ${state.deviceStored}")
            if (state.b2Stored.isNotBlank()) Text("Backblaze B2: ${state.b2Stored}")
            Text("Finished archives can also be copied to a folder from Library. Exported copies and the working library are separate.")
            TextButton(onClick = { onPage(B2Page.BROWSE) }) { Text(B2Page.BROWSE.label) }
            return@Column
        }
        if (state.showSharing) {
            Row(Modifier.fillMaxWidth()) {
                Text("Use the same Backblaze setup for every site", Modifier.weight(1f))
                Switch(checked = state.shared, enabled = state.canShare && !busy && !state.taskRunning,
                    onCheckedChange = { onAction(CloudRequest.Sharing(it)) },
                    modifier = Modifier.semantics { contentDescription = "Use the same Backblaze setup for every site" })
            }
            Text(state.sharingHelp)
        }
        TextButton(onClick = { showCost = !showCost }) { Text("Backblaze cost details") }
        if (showCost) {
            Text("Backblaze bills you directly. Comic Archiver does not store your payment details.")
            Text(state.cost.ifBlank { "A library-size estimate is not available yet. Check Backblaze's prices and your usage." })
            TextButton(onClick = { onPage(B2Page.PRICING) }) { Text(B2Page.PRICING.label) }
            TextButton(onClick = { onPage(B2Page.BILLING) }) { Text(B2Page.BILLING.label) }
            TextButton(onClick = { onPage(B2Page.CAPS) }) { Text(B2Page.CAPS.label) }
        }
        if (state.bucket.isNotBlank())
            TextButton(onClick = { showSteps = !showSteps }) {
                Text(if (showSteps) "Hide setup steps" else "Show setup steps or replace key")
            }
        if (state.bucket.isBlank() || showSteps) {
            Text("1. Use your own Backblaze account", style = MaterialTheme.typography.titleMedium)
            TextButton(onClick = { onPage(B2Page.ACCOUNT) }) { Text(B2Page.ACCOUNT.label) }
            TextButton(onClick = { onPage(B2Page.SIGN_IN) }) { Text(B2Page.SIGN_IN.label) }
            Text("2. Create a private bucket. Disable Object Lock for this library's test cleanup. Use the same bucket name here and in Backblaze.")
            TextButton(onClick = { onPage(B2Page.BUCKETS) }) { Text(B2Page.BUCKETS.label) }
            OutlinedTextField(value = bucket, onValueChange = { bucket = it.filterNot(Char::isWhitespace).take(63) },
                label = { Text("Bucket name") }, enabled = state.canConnect && !busy,
                singleLine = true, modifier = Modifier.fillMaxWidth())
            Text("3. Add an application key for this bucket only: Read and Write, no name prefix or expiry. Leave List All Bucket Names off. Keep the applicationKey page open until connected.")
            TextButton(onClick = { onPage(B2Page.KEYS) }) { Text(B2Page.KEYS.label) }
            Text("4. Paste the two key values. They stay on this device.")
            OutlinedTextField(value = keyId, onValueChange = { keyId = it.filterNot(Char::isWhitespace).take(256) },
                label = { Text("keyID") }, enabled = state.canConnect && !busy,
                singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = applicationKey,
                onValueChange = { applicationKey = it.filterNot(Char::isWhitespace).take(256) },
                label = { Text("applicationKey") }, enabled = state.canConnect && !busy,
                singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
            Button(enabled = state.canConnect && !busy && bucket.length in 6..63 &&
                keyId.length in 8..256 && applicationKey.length in 8..256, onClick = {
                    val request = CloudRequest.Connect(bucket, keyId, applicationKey)
                    keyId = ""; applicationKey = ""
                    onAction(request)
                }) { Text(state.connectLabel) }
        }
        if (state.showCancel)
            OutlinedButton(enabled = state.canCancel && !busy, onClick = { onAction(CloudRequest.Cancel) }) { Text("Cancel B2 sign-in") }
        Text("5. Test the connection with two finished episodes: upload, read back, verify and remove the test copies. Local originals stay here.")
        if (!signedIn) Text("Sign in to the site before starting cloud tests or retries.")
        Button(enabled = state.canTest && signedIn && !busy, onClick = { onAction(CloudRequest.Test) }) { Text("Test cloud connection") }
        state.cleanups.forEach { cleanup ->
            OutlinedButton(enabled = cleanup.enabled && signedIn && !busy,
                onClick = { onAction(CloudRequest.Cleanup(cleanup.id)) },
                modifier = Modifier.testTag("cloud-cleanup-${cleanup.id}")) { Text(cleanup.label) }
        }
        Text("6. After the test passes, choose Backblaze B2 on Storage. Verified uploads follow that destination's retention policy; a failed upload keeps its local original.")
        if (state.cleanupRemaining > 0)
            Text("${state.cleanupRemaining} additional recorded cleanups remain. Finish these and refresh to see the next ones.")
        OutlinedButton(enabled = state.canRetry && signedIn && !busy,
            onClick = { onAction(CloudRequest.Retry) }) { Text("Retry unfinished uploads") }
        listOf(B2Page.BROWSE, B2Page.REPORTS, B2Page.BILLING, B2Page.CAPS, B2Page.KEYS).forEach { page ->
            TextButton(onClick = { onPage(page) }) { Text(page.label) }
        }
    }
}
