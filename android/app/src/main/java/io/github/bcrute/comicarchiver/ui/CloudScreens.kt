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
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TriStateCheckbox
import androidx.compose.ui.state.ToggleableState
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
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.AppTheme
import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.CloudStatus
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.runtime.WorkingLibrariesState
import io.github.bcrute.comicarchiver.words.say

/** Fixed public destinations only. Site sessions never enter these browser intents. */
internal enum class B2Page(val label: String, val url: String) {
    ACCOUNT(say("cloud.create_b2_account"), "https://www.backblaze.com/sign-up/s3"),
    SIGN_IN(say("cloud.sign_in_to_backblaze"), "https://secure.backblaze.com/user_signin.htm"),
    BILLING(say("cloud.open_billing"), "https://secure.backblaze.com/billing.htm"),
    CAPS(say("cloud.open_caps_alerts"), "https://secure.backblaze.com/b2_caps_alerts.htm"),
    BUCKETS(say("cloud.open_buckets"), "https://secure.backblaze.com/b2_buckets.htm"),
    KEYS(say("cloud.open_application_keys"), "https://secure.backblaze.com/app_keys.htm"),
    BROWSE(say("cloud.open_in_backblaze"), "https://secure.backblaze.com/b2_browse_files2.htm"),
    REPORTS(say("cloud.open_reports"), "https://secure.backblaze.com/b2_reports.htm"),
    PRICING(say("cloud.current_backblaze_prices"), "https://www.backblaze.com/cloud-storage/pricing"),
}

@Composable
fun SettingsPages(state: AppState, onTheme: (AppTheme) -> Unit, onWifiOnly: (Boolean) -> Unit,
                  onSpeed: (String) -> Unit, onRefresh: () -> Unit,
                  onDownloadTypes: (Map<String, Boolean>) -> Unit = {},
                  onCloudRefresh: () -> Unit, onCloudAction: (CloudRequest) -> Unit,
                  workingLibraries: WorkingLibrariesState = WorkingLibrariesState(),
                  workingLibraryActions: WorkingLibraryActions = WorkingLibraryActions(),
                  selectedPage: String? = null, onPageSelected: (String) -> Unit = {},
                  compactEditing: Boolean = false, onOpenCatalog: (String) -> Unit = {},
                  onAbout: () -> Unit = {}, onSupport: () -> Unit = {}, onAccount: () -> Unit = {},
                  onSelectSite: (String) -> Unit = {}) {
    var savedPage by rememberSaveable { mutableStateOf("Overview") }
    val page = selectedPage ?: savedPage
    fun choose(name: String) {
        savedPage = name; onPageSelected(name)
        if (name == "Downloads") onRefresh()
    }
    LaunchedEffect(page, state.site, state.libraryId, state.workspaceReady, state.siteBusy) {
        if (page in setOf("Storage", "B2 setup") && state.workspaceReady && !state.siteBusy) onCloudRefresh()
    }
    Column(Modifier.fillMaxSize()) {
        if (page == "Overview") {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(say("app.settings"), style = MaterialTheme.typography.headlineMedium)
                Text(say("cloud.make_saving_fit_your_setup"), color = MaterialTheme.colorScheme.onSurfaceVariant)
                SettingsDestination(say("cloud.download_preferences"), say("cloud.connection_speed_and_appearance")) { choose("Downloads") }
                SettingsDestination("Storage & destination", say("cloud.choose_where_new_episodes_are_saved")) { choose("Storage") }
                SettingsDestination(say("cloud.backblaze_b2"), say("cloud.connect_your_own_bucket_or_reuse")) { choose("B2 setup") }
                if (workingLibraries.enabled) SettingsDestination(say("cloud.working_libraries"), say("cloud.switch_import_or_name_a_library")) { choose("Libraries") }
                SettingsDestination(say("app.support"), say("support.need_help_email_with_the_log"), onSupport)
                SettingsDestination(say("app.about"), say("cloud.app_information_licenses_and_sign_out"), onAbout)
                val siteName = state.sites.firstOrNull { it.key == state.site }?.name
                    ?: if (state.site == "tapas") "Tapas" else state.site
                Text(say("cloud.account", "siteName" to siteName), style = MaterialTheme.typography.titleMedium)
                Text(if (state.signIn is SignInState.SignedIn) say("cloud.signed_in_on_this_phone") else say("cloud.sign_in_needed"))
                val held = signOutHeldBySaving(state)
                if (held) Text(SIGN_OUT_HELD)
                TextButton(onClick = onAccount,
                    enabled = !state.siteBusy && !state.libraryBusy && state.workspaceReady && !held) {
                    Text(if (state.signOutPending) say("app.retry_sign_out")
                        else if (state.signIn is SignInState.SignedIn) say("cloud.sign_out_of", "siteName" to siteName) else say("signin.sign_in_to", "siteName" to siteName))
                }
            }
            return@Column
        }
        if (!compactEditing) TextButton(onClick = { choose("Overview") }) { Text(say("app.all_settings")) }
        when (page) {
            "Libraries" -> WorkingLibrariesScreen(workingLibraries, workingLibraryActions)
            "Downloads" -> SettingsScreen(state.settings, state.settingsBusy || state.siteBusy || state.libraryBusy || state.signOutPending || !state.workspaceReady,
                state.download is DownloadState.Running || state.download is DownloadState.Planning,
                state.settingsNotice, onWifiOnly, onSpeed, onRefresh, state.theme, onTheme,
                state.siteCards.firstOrNull { it.site == state.site },
                state.sites.firstOrNull { it.key == state.site }?.name ?: if (state.site == "tapas") "Tapas" else state.site,
                onDownloadTypes)
            else -> {
                val cloud = state.cloud
                if (cloud == null) {
                    Column(Modifier.padding(16.dp)) {
                        Text(state.cloudNotice?.message ?: say("cloud.loading_cloud_settings"))
                        OutlinedButton(onClick = onCloudRefresh) { Text(say("cloud.refresh_cloud_settings")) }
                    }
                } else {
                    val open = LocalUriHandler.current
                    CloudScreen(cloud, state.cloudBusy || state.siteBusy || state.libraryBusy || !state.workspaceReady || state.signOutPending, state.cloudNotice?.message,
                        page == "B2 setup", state.signIn is SignInState.SignedIn,
                        onCloudRefresh, onCloudAction, { open.openUri(it.url) }, onOpenCatalog,
                        providerPicker = { SourcePicker(state, onSelectSite) })
                }
            }
        }
    }
}

@Composable
private fun SettingsDestination(title: String, description: String, onClick: () -> Unit) {
    OutlinedButton(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(description, style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
internal fun CloudScreen(state: CloudStatus, busy: Boolean, notice: String?, setup: Boolean,
                         signedIn: Boolean, onRefresh: () -> Unit,
                         onAction: (CloudRequest) -> Unit, onPage: (B2Page) -> Unit,
                         onOpenCatalog: (String) -> Unit = {}, providerPicker: @Composable () -> Unit = {}) {
    // Key text deliberately uses remember, never rememberSaveable. Leaving setup,
    // recreation and process death discard it; AppState contains no credentials.
    var bucket by remember { mutableStateOf(state.suggestion) }
    var keyId by remember { mutableStateOf("") }
    var applicationKey by remember { mutableStateOf("") }
    var showSteps by rememberSaveable { mutableStateOf(false) }
    var replaceKey by rememberSaveable { mutableStateOf(false) }
    var showCost by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(state.authStatus, setup) {
        if (state.authStatus == "connected" || !setup) {
            keyId = ""; applicationKey = ""
            if (state.bucket.isNotBlank()) bucket = state.bucket
        }
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)
        .testTag("cloud-screen"), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(if (setup) say("cloud.backblaze_b2") else say("cloud.storage"), style = MaterialTheme.typography.headlineSmall)
        OutlinedButton(onClick = onRefresh, enabled = !busy) { Text(say("cloud.refresh_cloud_settings")) }
        if (busy || state.authStatus == "running" || (state.taskRunning && !state.taskHeld)) {
            LinearProgressIndicator(Modifier.fillMaxWidth())
            Text(if (state.taskRunning) say("cloud.cloud_task_running") else say("cloud.updating_cloud_settings"))
        }
        notice?.let { Text(it) }
        if (state.message.isNotBlank()) Text(state.message)
        if (state.taskHeld) {
            Text(say("cloud.cloud_task_held_nothing_restarts_until"))
            Button(enabled = signedIn && !busy, onClick = { onAction(CloudRequest.Resume) }) {
                Text(say("cloud.continue_cloud_task"))
            }
        }
        Text(state.title, style = MaterialTheme.typography.titleMedium)
        Text(state.explanation)
        Text(state.status)
        if (state.auth.isNotBlank()) Text(state.auth)
        if (state.repair.isNotBlank()) Text(state.repair)
        if (!setup) {
            Text(say("cloud.store_new_episodes_on"), style = MaterialTheme.typography.titleMedium)
            if (state.destinations.isEmpty()) Text(say("cloud.new_episodes_stay_on_this_device"))
            state.destinations.forEach { destination ->
                FilterChip(selected = destination.checked, enabled = destination.enabled && !busy,
                    onClick = { onAction(CloudRequest.Destination(destination.key)) },
                    label = { Text(destination.label) })
            }
            Text(state.destinationHelp)
            if (state.destinationLock.isNotBlank()) Text(state.destinationLock)
            Text(say("cloud.this_device", "deviceStored" to state.deviceStored))
            if (state.b2Stored.isNotBlank()) Text(say("cloud.backblaze_b2_2", "b2Stored" to state.b2Stored))
            state.overview?.takeIf { it.available }?.locations?.forEach { place ->
                OutlinedButton(onClick = { onOpenCatalog(place.id) }, enabled = !busy) {
                    Text(say("cloud.view_stored_episodes_on", "label" to place.label))
                }
            }
            Text(say("cloud.finished_archives_can_also_be_copied"))
            B2Link(B2Page.BROWSE, onPage)
            return@Column
        }
        if (state.showSharing) {
            Row(Modifier.fillMaxWidth()) {
                Text(say("cloud.use_the_same_backblaze_setup_for"), Modifier.weight(1f))
                TriStateCheckbox(state = if (state.sharingMixed) ToggleableState.Indeterminate else
                    if (state.shared) ToggleableState.On else ToggleableState.Off,
                    enabled = state.canShare && !busy && !state.taskRunning,
                    onClick = { onAction(CloudRequest.Sharing(!state.shared)) },
                    modifier = Modifier.semantics { contentDescription = say("cloud.use_the_same_backblaze_setup_for") })
            }
            Text(state.sharingHelp)
            if (!state.shared && !state.sharingMixed) providerPicker()
        }
        TextButton(onClick = { showCost = !showCost }) { Text(say("cloud.backblaze_cost_details")) }
        if (showCost) {
            Text(say("cloud.backblaze_bills_you_directly_comic_archiver"))
            Text(state.cost.ifBlank { say("cloud.a_library_size_estimate_is_not") })
            B2Link(B2Page.PRICING, onPage)
            B2Link(B2Page.BILLING, onPage)
            B2Link(B2Page.CAPS, onPage)
        }
        if (state.bucket.isNotBlank()) TextButton(onClick = { replaceKey = !replaceKey }) {
            Text(if (replaceKey) say("cloud.cancel_key_replacement") else say("cloud.replace_connection_key"))
        }
        if (state.bucket.isBlank() || replaceKey) {
            Text(say("cloud.connect_your_bucket"), style = MaterialTheme.typography.titleMedium)
            Text(say("cloud.already_use_b2_on_your_computer"))
            OutlinedTextField(value = bucket, onValueChange = { bucket = it.filterNot(Char::isWhitespace).take(63) },
                label = { Text(say("cloud.bucket_name")) }, enabled = state.canConnect && !busy,
                singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = keyId, onValueChange = { keyId = it.filterNot(Char::isWhitespace).take(256) },
                label = { Text("keyID") }, enabled = state.canConnect && !busy,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.None,
                    autoCorrectEnabled = false, keyboardType = KeyboardType.Password, imeAction = ImeAction.Next),
                singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = applicationKey,
                onValueChange = { applicationKey = it.filterNot(Char::isWhitespace).take(256) },
                label = { Text("applicationKey") }, enabled = state.canConnect && !busy,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.None,
                    autoCorrectEnabled = false, keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
            Button(enabled = state.canConnect && !busy && bucket.length in 6..63 &&
                keyId.length in 8..256 && applicationKey.length in 8..256, onClick = {
                    val request = CloudRequest.Connect(bucket, keyId, applicationKey)
                    keyId = ""; applicationKey = ""
                    onAction(request)
                }) { Text(state.connectLabel) }
        }
        if (state.showCancel)
            OutlinedButton(enabled = state.canCancel && !busy, onClick = { onAction(CloudRequest.Cancel) }) { Text(say("cloud.cancel_b2_sign_in")) }
        Text(say("cloud.verify_your_connection"), style = MaterialTheme.typography.titleMedium)
        Text(say("cloud.the_test_uploads_two_finished_episodes"))
        if (!signedIn) Text(say("cloud.sign_in_to_the_site_before"))
        Button(enabled = state.canTest && signedIn && !busy, onClick = { onAction(CloudRequest.Test) }) { Text(say("cloud.test_cloud_connection")) }
        state.cleanups.forEach { cleanup ->
            OutlinedButton(enabled = cleanup.enabled && signedIn && !busy,
                onClick = { onAction(CloudRequest.Cleanup(cleanup.id)) },
                modifier = Modifier.testTag("cloud-cleanup-${cleanup.id}")) { Text(cleanup.label) }
        }
        Text(say("cloud.after_a_successful_test_choose_backblaze"))
        if (state.cleanupRemaining > 0)
            Text(say("cloud.additional_recorded_cleanups_remain_finish_these", "cleanupRemaining" to state.cleanupRemaining))
        OutlinedButton(enabled = state.canRetry && signedIn && !busy,
            onClick = { onAction(CloudRequest.Retry) }) { Text(say("cloud.retry_unfinished_uploads")) }
        TextButton(onClick = { showSteps = !showSteps }) {
            Text(if (showSteps) say("cloud.hide_setup_guide") else say("cloud.need_a_bucket_or_key_setup"))
        }
        if (showSteps) {
            Text(say("cloud.1_use_your_own_backblaze_account"), style = MaterialTheme.typography.titleMedium)
            B2Link(B2Page.ACCOUNT, onPage); B2Link(B2Page.SIGN_IN, onPage)
            Text(say("cloud.2_create_a_private_bucket_disable"))
            B2Link(B2Page.BUCKETS, onPage)
            Text(say("cloud.3_create_an_application_key_for"))
            B2Link(B2Page.KEYS, onPage)
        }
        listOf(B2Page.BROWSE, B2Page.REPORTS, B2Page.BILLING, B2Page.CAPS, B2Page.KEYS).forEach { page ->
            B2Link(page, onPage)
        }
    }
}

/** Keep browser refusal beside the link and let the same control retry. No cloud action. */
@Composable
private fun B2Link(page: B2Page, onPage: (B2Page) -> Unit) {
    var failed by remember(page) { mutableStateOf(false) }
    TextButton(onClick = {
        failed = try { onPage(page); false } catch (_: Exception) { true }
    }) { Text(page.label) }
    if (failed) Text(say("cloud.could_not_open_backblaze_check_that"))
}
