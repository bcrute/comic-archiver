package io.github.bcrute.comicarchiver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.library.FolderLibraryState
import io.github.bcrute.comicarchiver.runtime.WorkingLibrariesState
import io.github.bcrute.comicarchiver.worker.DownloadNotificationAccess

/**
 * The whole Compose application root over the frozen controller. Native's MainActivity
 * calls exactly this one composable; the root observes `controller.state`, maps it
 * through [UiMapping], renders the existing screens and forwards every action straight
 * to the controller. It owns no download, network, sign-in or export policy:
 *
 * - sign-in's WebView surface is native/engine-supplied through [signInContent];
 * - export progress and outcome are native-supplied as [exportState] (the root never
 *   fabricates completion — the panel shows only what it is given);
 * - folder picking is a callback ([onPickFolder]); native owns the intent and grant;
 * - every transition toward Running remains the controller's `go()`.
 *
 * No navigation framework: an internal tab (saved across recomposition and process
 * restoration) chooses Downloads / Library / Settings; About lives under Settings. Sign-in replaces only the Saving
 * content; local library browsing and export work offline and while signed out.
 */
@Composable
@OptIn(androidx.compose.ui.ExperimentalComposeUiApi::class)
fun ComicArchiverAppRoot(
    controller: AppController,
    exportState: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
    folderState: FolderLibraryState = FolderLibraryState(),
    onFolderLoad: () -> Unit = {}, onOpenFolder: () -> Unit = {}, onCloseFolder: () -> Unit = {},
    onFolderQuery: (CatalogQuery, Boolean) -> Unit = { _, _ -> }, onOpenFolderFile: (String) -> Unit = {},
    workingLibraries: WorkingLibrariesState = WorkingLibrariesState(),
    workingLibraryActions: WorkingLibraryActions = WorkingLibraryActions(),
    notificationAccess: DownloadNotificationAccess = DownloadNotificationAccess.ENABLED,
    notificationError: String? = null,
    onNotificationAccess: () -> Unit = {},
    signInContent: @Composable () -> Unit = {},
) {
    val state by controller.state.collectAsState()
    var tab by rememberSaveable { mutableStateOf(AppTab.SAVING) }
    var settingsPage by rememberSaveable { mutableStateOf("Overview") }
    var showFilesRequested by rememberSaveable(state.site, state.libraryId) { mutableStateOf(false) }
    var signOutSite by rememberSaveable { mutableStateOf<String?>(null) }
    var signOutLibrary by rememberSaveable { mutableStateOf<String?>(null) }
    val signOutAvailable = state.signIn is SignInState.SignedIn && !state.signOutPending &&
        !state.siteBusy && !state.libraryBusy && state.workspaceReady
    LaunchedEffect(state.site, state.libraryId, signOutAvailable) {
        if (signOutSite != state.site || signOutLibrary != state.libraryId || !signOutAvailable) {
            signOutSite = null
            signOutLibrary = null
        }
    }
    val openCatalog: (String) -> Unit = { location ->
        controller.loadCatalog(CatalogQuery(location = location), false)
        showFilesRequested = true
        tab = AppTab.LIBRARY
    }
    val tabState = rememberSaveableStateHolder()
    val signingIn = state.signIn is SignInState.Page || state.signIn is SignInState.Settling ||
        state.signIn is SignInState.Restoring
    BackHandler(enabled = tab != AppTab.SAVING || signingIn) {
        if (tab == AppTab.ABOUT) tab = AppTab.SETTINGS
        else if (tab == AppTab.SETTINGS && settingsPage != "Overview") settingsPage = "Overview"
        else if (tab != AppTab.SAVING) tab = AppTab.SAVING else controller.cancelSignIn()
    }
    val keyboardVisible = WindowInsets.ime.getBottom(LocalDensity.current) > 0
    val accountAction: () -> Unit = {
        if (state.signOutPending) controller.signOut()
        else if (signOutAvailable) {
            signOutSite = state.site
            signOutLibrary = state.libraryId
        } else tab = AppTab.SAVING
    }
    if (signOutSite == state.site && signOutLibrary == state.libraryId && signOutAvailable) {
        val targetSite = state.site
        val targetLibrary = state.libraryId
        val name = state.sites.firstOrNull { it.key == targetSite }?.name
            ?: if (targetSite == "tapas") "Tapas" else targetSite
        AlertDialog(
            onDismissRequest = { signOutSite = null; signOutLibrary = null },
            title = { Text("Sign out of $name?") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    Text("This signs out of $name in Comic Archiver on this phone and stops saving. " +
                        "Your $name account, other sites and sign-ins in other apps stay unchanged.\n\n" +
                        "Your saved episodes, export copies and B2 setup stay where they are. " +
                        "Sign in again to download more from $name.")
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val current = controller.state.value
                    val confirmed = signOutSite == targetSite && signOutLibrary == targetLibrary
                    signOutSite = null; signOutLibrary = null
                    // A restored dialog or late tap must never target a different session.
                    if (confirmed && current.site == targetSite && current.libraryId == targetLibrary &&
                        current.signIn is SignInState.SignedIn && !current.signOutPending &&
                        !current.siteBusy && !current.libraryBusy && current.workspaceReady) controller.signOut()
                }) { Text("Sign out") }
            },
            dismissButton = {
                TextButton(onClick = { signOutSite = null; signOutLibrary = null }) { Text("Cancel") }
            },
        )
    }
    BoxWithConstraints(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }) {
        // A landscape IME can leave less height than the navigation rows alone.
        // Keep the selected editor mounted; dismissing IME restores its navigation.
        val compactEditing = keyboardVisible && maxHeight < 320.dp
        Column(Modifier.fillMaxSize()) {
            if (!compactEditing) SourceHeader(state, controller::selectSite, exportState.phase == ExportPhaseUi.RUNNING)
            Column(Modifier.weight(1f).fillMaxWidth()) {
                if (state.siteBusy) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text("Updating account and library…", Modifier.padding(horizontal = 16.dp))
                }
                if (state.signOutPending && !state.siteBusy) {
                    Text("Sign-out did not finish. Your saved files are kept.", Modifier.padding(horizontal = 16.dp))
                    OutlinedButton(onClick = controller::signOut, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
                        Text("Retry sign-out")
                    }
                }
                if (tab != AppTab.SAVING) state.notice?.let { notice ->
                    NoticeCard(notice.toNoticeUi(), onDismiss = { controller.openNotice(notice) }, showRecovery = false)
                }
                if (state.libraryBusy && tab != AppTab.SETTINGS) Text("Working library change in progress. Open Settings → Libraries for its status.", Modifier.padding(horizontal = 16.dp))
                tabState.SaveableStateProvider(if (state.libraryId == "default") "${state.site}:${tab.name}" else "${state.libraryId}:${state.site}:${tab.name}") {
                    when (tab) {
                        AppTab.SAVING -> if (routeFor(state.signIn) == RootRoute.SIGN_IN) {
                            SignInScreen(
                                state = state.toSignInScreenUi(),
                                onStartSignIn = controller::startSignIn,
                                signInContent = signInContent,
                            )
                        } else {
                            DownloadsScreen(
                                state = state.toDownloadsScreenUi(),
                                onGo = controller::go,
                                onPause = controller::pause,
                                notificationAccess = notificationAccess,
                                notificationError = notificationError,
                                onNotificationAccess = onNotificationAccess,
                                onDownloadSettings = { settingsPage = "Downloads"; controller.refreshSettings(); tab = AppTab.SETTINGS },
                                onStorageSettings = { settingsPage = "Storage"; controller.refreshCloud(); tab = AppTab.SETTINGS },
                                onOpenCatalog = openCatalog,
                                onDismissNotice = state.notice?.let { notice ->
                                    { controller.openNotice(notice) }
                                },
                            )
                        }
                        AppTab.LIBRARY -> Column(Modifier.fillMaxSize()) {
                            LibraryScreen(
                                series = state.toLibraryUi(),
                                catalog = state.catalog,
                                onCatalogQuery = controller::loadCatalog,
                                onOpenFile = controller::openSavedFile,
                                exportState = exportState,
                                onPickFolder = onPickFolder,
                                onStartExport = onStartExport,
                                onStopExport = onStopExport,
                                folderState = folderState, onFolderLoad = onFolderLoad, onOpenFolder = onOpenFolder,
                                onCloseFolder = onCloseFolder, onFolderQuery = onFolderQuery, onOpenFolderFile = onOpenFolderFile,
                                actionsEnabled = state.workspaceReady && !state.libraryBusy && !state.siteBusy,
                                compactEditing = compactEditing,
                                onWorkingLibrary = if (workingLibraries.enabled) ({ settingsPage = "Libraries"; tab = AppTab.SETTINGS }) else null,
                                showFilesRequested = showFilesRequested,
                                onFilesRequestConsumed = { showFilesRequested = false },
                            )
                        }
                        AppTab.SETTINGS -> SettingsPages(
                            state = state,
                            onTheme = controller::setTheme,
                            onWifiOnly = controller::setWifiOnly,
                            onSpeed = controller::setDownloadSpeed,
                            onRefresh = controller::refreshSettings,
                            onCloudRefresh = controller::refreshCloud,
                            onCloudAction = controller::cloudAction,
                            workingLibraries = workingLibraries, workingLibraryActions = workingLibraryActions,
                            selectedPage = settingsPage, onPageSelected = { settingsPage = it },
                            compactEditing = compactEditing,
                            onOpenCatalog = openCatalog,
                            onAbout = { tab = AppTab.ABOUT },
                            onAccount = accountAction,
                        )
                        AppTab.ABOUT -> Column(Modifier.fillMaxSize()) {
                            TextButton(onClick = { tab = AppTab.SETTINGS }) { Text("All settings") }
                            LimitationsScreen(modifier = Modifier.weight(1f))
                            OutlinedButton(
                                onClick = accountAction,
                                modifier = Modifier.fillMaxWidth().padding(16.dp),
                                enabled = !state.siteBusy && !state.libraryBusy && state.workspaceReady,
                            ) {
                                val name = state.sites.firstOrNull { it.key == state.site }?.name
                                    ?: if (state.site == "tapas") "Tapas" else state.site
                                Text(if (state.signIn is SignInState.SignedIn || state.signOutPending) "Sign out of $name"
                                     else "Go to sign in")
                            }
                        }
                    }
                }
            }
            if (!compactEditing) MainNavigation(tab) { selected ->
                if (tab == AppTab.ABOUT && selected == AppTab.SETTINGS) settingsPage = "Overview"
                tab = selected
            }
        }
    }
}

/** Which surface the Saving tab shows. */
internal enum class RootRoute { SIGN_IN, APP }

/** Only verified sign-in offers download controls. Local library access is independent. */
internal fun routeFor(signIn: SignInState): RootRoute =
    if (signIn is SignInState.SignedIn) RootRoute.APP else RootRoute.SIGN_IN

/** About is a Settings subpage; only the first three destinations appear in navigation. */
internal enum class AppTab(val label: String) {
    SAVING("Downloads"), LIBRARY("Library"), SETTINGS("Settings"), ABOUT("About")
}

/**
 * Go is offered only while held (any reason) or after a finished run — never while
 * running, where choosing Start saving again would be meaningless or policy-changing.
 */
internal fun DownloadsViewUi.goEnabled(): Boolean =
    this is DownloadsViewUi.Held || this is DownloadsViewUi.Finished

/** Pause is offered only while something is actually in flight (planning or running). */
internal fun DownloadsViewUi.pauseEnabled(): Boolean = this is DownloadsViewUi.Running
