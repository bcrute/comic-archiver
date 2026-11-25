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
import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.library.FolderLibraryState
import io.github.bcrute.comicarchiver.runtime.WorkingLibrariesState
import io.github.bcrute.comicarchiver.worker.DownloadNotificationAccess
import io.github.bcrute.comicarchiver.words.say

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
 * restoration) chooses Downloads / Library / Settings; About lives under Settings. An open sign-in page replaces only the
 * Downloads content; local library browsing and export work offline and while signed out.
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
    updates: UpdateSettings? = null,
    signInContent: @Composable () -> Unit = {},
) {
    val state by controller.state.collectAsState()
    var tab by rememberSaveable { mutableStateOf(AppTab.SAVING) }
    var settingsPage by rememberSaveable { mutableStateOf("Overview") }
    var showFilesRequested by rememberSaveable(state.site, state.libraryId) { mutableStateOf(false) }
    var comicsProvider by rememberSaveable(state.libraryId) { mutableStateOf<String?>(null) }
    var showComicsRequested by rememberSaveable(state.libraryId) { mutableStateOf(false) }
    var signOutSite by rememberSaveable { mutableStateOf<String?>(null) }
    var signOutLibrary by rememberSaveable { mutableStateOf<String?>(null) }
    // A card's Sign out or episode types on a site that is not the one shown: the site is shown first.
    var signOutWanted by rememberSaveable { mutableStateOf<String?>(null) }
    var typesSite by rememberSaveable { mutableStateOf<String?>(null) }
    // As on the desktop: a site that is downloading is paused first, then signed out.
    val signOutHeld = signOutHeldBySaving(state)
    val signOutAvailable = state.signIn is SignInState.SignedIn && !state.signOutPending &&
        !state.siteBusy && !state.libraryBusy && state.workspaceReady && !signOutHeld
    LaunchedEffect(state.site, state.libraryId, signOutAvailable) {
        if (signOutSite != state.site || signOutLibrary != state.libraryId || !signOutAvailable) {
            signOutSite = null
            signOutLibrary = null
        }
        if (signOutWanted == state.site && signOutAvailable) {
            signOutSite = state.site
            signOutLibrary = state.libraryId
            signOutWanted = null
        }
    }
    val openCatalog: (String) -> Unit = { location ->
        controller.loadCatalog(CatalogQuery(location = location), false)
        showFilesRequested = true
        tab = AppTab.LIBRARY
    }
    val tabState = rememberSaveableStateHolder()
    val signingIn = state.signIn is SignInState.Page || state.signIn is SignInState.Settling ||
        state.signIn is SignInState.Restoring || state.signIn.couldNotOpen()
    BackHandler(enabled = tab != AppTab.SAVING || signingIn) {
        if (tab == AppTab.LICENSES) tab = AppTab.ABOUT
        else if (tab == AppTab.ABOUT || tab == AppTab.SUPPORT) tab = AppTab.SETTINGS
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
            title = { Text(say("app.sign_out_of", "name" to name)) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    Text(say("app.this_signs_out_of_in_comic", "name" to name, "name2" to name, "name3" to name))
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
                }) { Text(say("app.sign_out")) }
            },
            dismissButton = {
                TextButton(onClick = { signOutSite = null; signOutLibrary = null }) { Text(say("app.cancel")) }
            },
        )
    }
    typesSite?.let { wanted ->
        val name = state.sites.firstOrNull { it.key == wanted }?.name ?: if (wanted == "tapas") "Tapas" else wanted
        val types = state.settings?.downloadTypes.orEmpty().takeIf { state.site == wanted && !state.siteBusy }.orEmpty()
        val extras = state.settings?.downloadExtras.orEmpty().takeIf { types.isNotEmpty() }.orEmpty()
        AlertDialog(
            onDismissRequest = { typesSite = null },
            title = { Text(say("window.download_choices_for", "name" to name)) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    if (types.isEmpty()) LinearProgressIndicator(Modifier.fillMaxWidth())
                    else DownloadTypeChoices(types, state.settingsBusy, { chosen ->
                        typesSite = null
                        controller.setDownloadTypes(chosen)
                    }, extras)
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(onClick = { typesSite = null }) { Text(say("app.cancel")) } },
        )
    }
    BoxWithConstraints(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }) {
        // A landscape IME can leave less height than the navigation rows alone.
        // Keep the selected editor mounted; dismissing IME restores its navigation.
        val compactEditing = keyboardVisible && maxHeight < 320.dp
        Column(Modifier.fillMaxSize()) {
            if (!compactEditing) SourceHeader(state, controller::selectSite, exportState.phase == ExportPhaseUi.RUNNING,
                // The dashboard shows every site at once; only the Library and Settings are one site's at a time.
                chooseSite = tab != AppTab.SAVING && !(tab == AppTab.SETTINGS && settingsPage == "B2 setup"))
            updates?.let { UpdateControls(it, manual = false) }
            Column(Modifier.weight(1f).fillMaxWidth()) {
                if (state.siteBusy) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text(say("app.updating_account_and_library"), Modifier.padding(horizontal = 16.dp))
                }
                if (state.signOutPending && !state.siteBusy) {
                    Text(say("app.sign_out_did_not_finish_your"), Modifier.padding(horizontal = 16.dp))
                    OutlinedButton(onClick = controller::signOut, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
                        Text(say("app.retry_sign_out"))
                    }
                }
                if (tab != AppTab.SAVING) state.notice?.let { notice ->
                    NoticeCard(notice.toNoticeUi(), onDismiss = { controller.openNotice(notice) }, showRecovery = false)
                }
                if (state.libraryBusy && tab != AppTab.SETTINGS) Text(say("app.working_library_change_in_progress_open"), Modifier.padding(horizontal = 16.dp))
                // The dashboard is every site's: it keeps its place whichever site a card's button shows.
                val shownSite = if (tab == AppTab.SAVING) "" else state.site
                tabState.SaveableStateProvider(if (state.libraryId == "default") "$shownSite:${tab.name}" else "${state.libraryId}:$shownSite:${tab.name}") {
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
                                onSignIn = controller::signInSite,
                                onGo = controller::goSite,
                                onPause = controller::pauseSite,
                                onSignOut = { key ->
                                    if (key == state.site) accountAction()
                                    else { signOutWanted = key; controller.selectSite(key) }
                                },
                                onSpeed = controller::speedSite,
                                onDownloadChoices = { key ->
                                    typesSite = key
                                    if (key == state.site) controller.refreshSettings() else controller.selectSite(key)
                                },
                                onListAction = { site, action ->
                                    if (action == "choose") {
                                        comicsProvider = site
                                        showComicsRequested = true
                                        tab = AppTab.LIBRARY
                                    } else controller.listPreference(site, action)
                                },
                                onGoAll = controller::goAll,
                                onPauseAll = controller::pauseAll,
                                onOpenLibrary = { showFilesRequested = true; tab = AppTab.LIBRARY },
                                libraryFolder = workingLibraries.choices.firstOrNull { it.current }?.label
                                    ?.takeIf { workingLibraries.enabled },
                                onChangeLibrary = { settingsPage = "Libraries"; tab = AppTab.SETTINGS },
                                notificationAccess = notificationAccess,
                                notificationError = notificationError,
                                onNotificationAccess = onNotificationAccess,
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
                                showComicsRequested = showComicsRequested,
                                onComicsRequestConsumed = { showComicsRequested = false },
                                sites = state.sites, episodes = state.episodes, initialProvider = comicsProvider, bulkChoices = state.siteCards.associate { it.site to it.bulk },
                                onLoadEpisodes = controller::loadEpisodes, onRefreshEpisodes = controller::refreshEpisodes,
                                onChooseComics = controller::chooseComics, comicChoicesBusy = state.settingsBusy,
                                comicChoicesNotice = state.comicChoicesNotice,
                            )
                        }
                        AppTab.SETTINGS -> SettingsPages(
                            state = state,
                            onTheme = controller::setTheme,
                            onWifiOnly = controller::setWifiOnly,
                            onSpeed = controller::setDownloadSpeed,
                            onDownloadTypes = controller::setDownloadTypes,
                            onRefresh = controller::refreshSettings,
                            onCloudRefresh = controller::refreshCloud,
                            onCloudAction = controller::cloudAction,
                            workingLibraries = workingLibraries, workingLibraryActions = workingLibraryActions,
                            selectedPage = settingsPage, onPageSelected = { settingsPage = it },
                            compactEditing = compactEditing,
                            onOpenCatalog = openCatalog,
                            onAbout = { tab = AppTab.ABOUT },
                            onSupport = { tab = AppTab.SUPPORT },
                            onAccount = accountAction,
                            onSelectSite = controller::selectSite,
                        )
                        AppTab.ABOUT -> Column(Modifier.fillMaxSize()) {
                            TextButton(onClick = { tab = AppTab.SETTINGS }) { Text(say("app.all_settings")) }
                            LimitationsScreen(modifier = Modifier.weight(1f), onLicenses = { tab = AppTab.LICENSES }, updates = updates)
                            if (signOutHeld) Text(SIGN_OUT_HELD, Modifier.padding(horizontal = 16.dp))
                            OutlinedButton(
                                onClick = accountAction,
                                modifier = Modifier.fillMaxWidth().padding(16.dp),
                                enabled = !state.siteBusy && !state.libraryBusy && state.workspaceReady && !signOutHeld,
                            ) {
                                val name = state.sites.firstOrNull { it.key == state.site }?.name
                                    ?: if (state.site == "tapas") "Tapas" else state.site
                                Text(if (state.signIn is SignInState.SignedIn || state.signOutPending) say("app.sign_out_of_2", "name" to name)
                                     else say("app.go_to_sign_in"))
                            }
                        }
                        AppTab.SUPPORT -> Column(Modifier.fillMaxSize()) {
                            TextButton(onClick = { tab = AppTab.SETTINGS }) { Text(say("app.all_settings")) }
                            SupportScreen(modifier = Modifier.weight(1f), supportNote = state.supportNote,
                                supportBusy = state.supportBusy, onEmailSupport = controller::emailSupport)
                        }
                        AppTab.LICENSES -> Column(Modifier.fillMaxSize()) {
                            TextButton(onClick = { tab = AppTab.ABOUT }) { Text(say("app.about")) }
                            LicensesScreen(modifier = Modifier.weight(1f))
                        }
                    }
                }
            }
            if (!compactEditing) MainNavigation(tab) { selected ->
                if (tab.underSettings() && selected == AppTab.SETTINGS) settingsPage = "Overview"
                tab = selected
            }
        }
    }
}

/** Which surface the Saving tab shows. */
internal enum class RootRoute { SIGN_IN, APP }

/** The site's sign-in page takes the tab only while it is open, as the desktop's sign-in window does;
 *  otherwise the dashboard shows, and a site that is signed out offers Sign in on its own card. A page
 *  that could not be opened stays, with the desktop's Try again, until the person goes back. */
internal fun routeFor(signIn: SignInState): RootRoute =
    if (signIn is SignInState.Page || signIn is SignInState.Settling || signIn.couldNotOpen()) RootRoute.SIGN_IN
    else RootRoute.APP

internal fun SignInState.couldNotOpen() = (this as? SignInState.Failed)?.code == "signin_unreachable"

/** Support, About and the Licenses About leads to are Settings subpages; only the first three destinations appear in navigation. */
internal enum class AppTab(val label: String) {
    SAVING(say("app.downloads")), LIBRARY(say("app.library")), SETTINGS(say("app.settings")), SUPPORT(say("app.support")),
    ABOUT(say("app.about")), LICENSES(say("app.licenses"));

    fun underSettings() = this == SUPPORT || this == ABOUT || this == LICENSES
}

/** The desktop's words for a Sign out that waits for the download to be paused. */
val SIGN_OUT_HELD get() = say("app.pause_the_download_to_sign_out")

/** A sign-out that did not finish may always be retried; otherwise a signed-in site that is saving keeps its sign-in. */
fun signOutHeldBySaving(state: AppState): Boolean =
    state.signIn is SignInState.SignedIn && !state.signOutPending &&
        (state.download is DownloadState.Running || state.download is DownloadState.Planning)
