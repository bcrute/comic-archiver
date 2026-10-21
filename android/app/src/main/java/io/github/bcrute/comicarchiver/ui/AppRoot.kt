package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.SignInState

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
 * restoration) chooses Saving / Library / About, and the sign-in screen replaces all of
 * it while signed out, settling, or after a failed sign-in.
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

    if (routeFor(state.signIn) == RootRoute.SIGN_IN) {
        SignInScreen(
            state = state.toSignInScreenUi(),
            onStartSignIn = controller::startSignIn,
            signInContent = signInContent,
        )
        return
    }

    var tab by rememberSaveable { mutableStateOf(AppTab.SAVING) }
    Column(Modifier.fillMaxSize()) {
        TabRow(selectedTabIndex = tab.ordinal) {
            AppTab.entries.forEach { candidate ->
                Tab(
                    selected = candidate == tab,
                    onClick = { tab = candidate },
                    text = { Text(candidate.label) },
                )
            }
        }
        when (tab) {
            AppTab.SAVING -> DownloadsScreen(
                state = state.toDownloadsScreenUi(),
                onGo = controller::go,
                onPause = controller::pause,
                onDismissNotice = state.notice?.let { notice ->
                    { controller.openNotice(notice) }
                },
            )
            AppTab.LIBRARY -> LibraryScreen(
                series = state.toLibraryUi(),
                exportState = exportState,
                onPickFolder = onPickFolder,
                onStartExport = onStartExport,
                onStopExport = onStopExport,
            )
            AppTab.ABOUT -> Column(Modifier.fillMaxSize()) {
                LimitationsScreen()
                OutlinedButton(
                    onClick = controller::signOut,
                    modifier = Modifier.fillMaxWidth().padding(16.dp),
                ) { Text("Sign out of Tapas") }
            }
        }
    }
}

/** Which top-level surface the root shows. */
internal enum class RootRoute { SIGN_IN, APP }

/** Signed-in is the only state that reaches the app tabs; every other sign-in shape,
 *  including a failed sign-in, stays on the sign-in screen with its notice. */
internal fun routeFor(signIn: SignInState): RootRoute =
    if (signIn is SignInState.SignedIn) RootRoute.APP else RootRoute.SIGN_IN

/** The three beta tabs; About carries the truthful limitations and sign-out. */
internal enum class AppTab(val label: String) { SAVING("Saving"), LIBRARY("Library"), ABOUT("About") }

/**
 * Go is offered only while held (any reason) or after a finished run — never while
 * running, where pressing Go again would be meaningless or policy-changing.
 */
internal fun DownloadsViewUi.goEnabled(): Boolean =
    this is DownloadsViewUi.Held || this is DownloadsViewUi.Finished

/** Pause is offered only while something is actually in flight (planning or running). */
internal fun DownloadsViewUi.pauseEnabled(): Boolean = this is DownloadsViewUi.Running
