package io.github.bcrute.comicarchiver.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Tab
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.controller.DownloadState

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
 * restoration) chooses Saving / Library / About. Sign-in replaces only the Saving
 * content; local library browsing and export work offline and while signed out.
 */
@Composable
fun ComicArchiverAppRoot(
    controller: AppController,
    exportState: ExportPanelStateUi,
    onPickFolder: () -> Unit,
    onStartExport: () -> Unit,
    onStopExport: () -> Unit,
    signInContent: @Composable () -> Unit = {},
) {
    val state by controller.state.collectAsState()

    var tab by rememberSaveable { mutableStateOf(AppTab.SAVING) }
    val tabState = rememberSaveableStateHolder()
    val signingIn = state.signIn is SignInState.Page || state.signIn is SignInState.Settling ||
        state.signIn is SignInState.Restoring
    BackHandler(enabled = tab != AppTab.SAVING || signingIn) {
        if (tab != AppTab.SAVING) tab = AppTab.SAVING else controller.cancelSignIn()
    }
    Column(Modifier.fillMaxSize()) {
        ScrollableTabRow(selectedTabIndex = tab.ordinal, edgePadding = 0.dp) {
            AppTab.entries.forEach { candidate ->
                Tab(
                    selected = candidate == tab,
                    onClick = { tab = candidate },
                    text = { Text(candidate.label, maxLines = 1, softWrap = false) },
                )
            }
        }
        tabState.SaveableStateProvider(tab) {
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
                        onDismissNotice = state.notice?.let { notice ->
                            { controller.openNotice(notice) }
                        },
                    )
                }
                AppTab.LIBRARY -> LibraryScreen(
                    series = state.toLibraryUi(),
                    catalog = state.catalog,
                    onCatalogQuery = controller::loadCatalog,
                    onOpenFile = controller::openSavedFile,
                    exportState = exportState,
                    onPickFolder = onPickFolder,
                    onStartExport = onStartExport,
                    onStopExport = onStopExport,
                )
                AppTab.SETTINGS -> SettingsScreen(
                    theme = state.theme,
                    onTheme = controller::setTheme,
                    settings = state.settings,
                    busy = state.settingsBusy,
                    saving = state.download is DownloadState.Running ||
                        state.download is DownloadState.Planning,
                    notice = state.settingsNotice,
                    onWifiOnly = controller::setWifiOnly,
                    onSpeed = controller::setDownloadSpeed,
                    onRefresh = controller::refreshSettings,
                )
                AppTab.ABOUT -> Column(Modifier.fillMaxSize()) {
                    LimitationsScreen(modifier = Modifier.weight(1f))
                    OutlinedButton(
                        onClick = {
                            if (state.signIn is SignInState.SignedIn) controller.signOut()
                            else tab = AppTab.SAVING
                        },
                        modifier = Modifier.fillMaxWidth().padding(16.dp),
                    ) {
                        Text(if (state.signIn is SignInState.SignedIn) "Sign out of Tapas"
                             else "Go to sign in")
                    }
                }
            }
        }
    }
}

/** Which surface the Saving tab shows. */
internal enum class RootRoute { SIGN_IN, APP }

/** Only verified sign-in offers download controls. Local library access is independent. */
internal fun routeFor(signIn: SignInState): RootRoute =
    if (signIn is SignInState.SignedIn) RootRoute.APP else RootRoute.SIGN_IN

/** The three beta tabs; About carries the truthful limitations and sign-out. */
internal enum class AppTab(val label: String) {
    SAVING("Saving"), LIBRARY("Library"), SETTINGS("Settings"), ABOUT("About")
}

/**
 * Go is offered only while held (any reason) or after a finished run — never while
 * running, where pressing Go again would be meaningless or policy-changing.
 */
internal fun DownloadsViewUi.goEnabled(): Boolean =
    this is DownloadsViewUi.Held || this is DownloadsViewUi.Finished

/** Pause is offered only while something is actually in flight (planning or running). */
internal fun DownloadsViewUi.pauseEnabled(): Boolean = this is DownloadsViewUi.Running
