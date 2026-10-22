package io.github.bcrute.comicarchiver.ui

import android.app.Application
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertDoesNotExist
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Production Compose screens, with taps, input, layout and save/restore assertions.
 * Only the controller/export side effects are fake: no account, WebView, worker or
 * filesystem is touched. End-to-end device/engine/SAF tests are a separate gate. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class RenderedScreensTest {
    @get:Rule val compose = createComposeRule()
    private val controller = ScreenController()

    private fun show(export: ExportPhaseUi = ExportPhaseUi.NOT_PICKED,
                     pick: () -> Unit = {}, start: () -> Unit = {}, stop: () -> Unit = {}) {
        compose.setContent {
            MaterialTheme {
                ComicArchiverAppRoot(controller, ExportPanelStateUi(export), pick, start, stop)
            }
        }
    }

    private fun library() = compose.onNode(hasText("Library") and hasClickAction()).performClick()

    @Test fun goImmediatelyShowsStartingAndPauseActuallyPauses() {
        show()
        compose.onNodeWithText("Pause").assertIsNotEnabled()
        compose.onNodeWithText("Go").performClick()
        compose.onNodeWithText("Starting…").assertIsDisplayed()
        compose.onNodeWithText("Running").assertIsNotEnabled()
        compose.onNodeWithText("Pause").assertIsEnabled().performClick()
        compose.onNodeWithText("Paused. Press Go to continue.").assertIsDisplayed()
        compose.onNodeWithText("Go").assertIsEnabled()
        assertEquals(1, controller.goCalls)
        assertEquals(1, controller.pauseCalls)
    }

    @Test fun realProgressAndFailureReachTheRenderedScreen() {
        show()
        compose.runOnIdle {
            controller.state.value = controller.state.value.copy(download = DownloadState.Running(
                0, 0, "Downloading episodes…", "37 episodes saved · 41 image pages in this batch"))
        }
        compose.onNodeWithText("37 episodes saved · 41 image pages in this batch").assertIsDisplayed()
        compose.onNodeWithText("Running").assertIsNotEnabled()
        compose.runOnIdle {
            controller.state.value = controller.state.value.copy(download = DownloadState.Failed("job_failed"))
        }
        compose.onNodeWithText("Go").assertIsEnabled()
        compose.onNodeWithText("Pause").assertIsNotEnabled()
    }

    @Test fun librarySearchHasAnHonestEmptyStateAndCanBeCleared() {
        show(); library()
        compose.onNodeWithText("Find saved series").performTextInput("no such title")
        compose.onNodeWithText("0 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("No saved series match these filters.").assertIsDisplayed()
        compose.onNodeWithText("Alpha Comic").assertDoesNotExist()
        compose.onNodeWithText("Clear search").performClick()
        compose.onNodeWithText("3 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("Alpha Comic").performScrollTo().assertIsDisplayed()
    }

    @Test fun searchAndFormatFilterWorkTogether() {
        show(); library()
        compose.onNodeWithText("Find saved series").performTextInput("  bEtA  ")
        compose.onNodeWithText("Novels").performClick()
        compose.onNodeWithText("1 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("Beta Novel").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Comics").performClick()
        compose.onNodeWithText("0 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("Beta Novel").assertDoesNotExist()
    }

    @Test fun sortChangesTheActualRowOrder() {
        show(); library()
        compose.onNodeWithText("Comics").performClick()
        compose.onNodeWithText("Sort: Title A–Z").performClick()
        compose.onNodeWithText("Title Z–A").performClick()
        fun top(title: String) = compose.onNodeWithText(title).fetchSemanticsNode().boundsInRoot.top
        compose.onNodeWithText("Alpha Comic").performScrollTo().assertIsDisplayed()
        assertTrue(top("Zulu Comic") < top("Alpha Comic"))
        compose.onNodeWithText("Sort: Title Z–A").performClick()
        compose.onNodeWithText("Most saved").performClick()
        compose.onNodeWithText("Zulu Comic").performScrollTo().assertIsDisplayed()
        assertTrue(top("Alpha Comic") < top("Zulu Comic"))
    }

    @Test fun filtersSurviveTabChangesAndSavedStateRestoration() {
        val restoration = StateRestorationTester(compose)
        restoration.setContent {
            MaterialTheme {
                ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
            }
        }
        library()
        compose.onNodeWithText("Find saved series").performTextInput("Beta")
        compose.onNodeWithText("Novels").performClick()
        compose.onNode(hasText("Saving") and hasClickAction()).performClick()
        library()
        compose.onNodeWithText("1 of 3 series").assertIsDisplayed()
        restoration.emulateSavedInstanceStateRestore()
        compose.onNodeWithText("1 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("Beta Novel").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls) // Navigation/restoration must never admit work.
    }

    @Test fun exportFolderPickerIsReachableWithoutStartingDownloads() {
        var picks = 0
        show(pick = { picks++ }); library()
        compose.onNodeWithText("Choose export folder").performScrollTo().performClick()
        assertEquals(1, picks)
        assertEquals(0, controller.goCalls)
    }

    @Test fun retryExportCallsTheExporterAndDoesNotClaimSuccess() {
        var retries = 0
        show(ExportPhaseUi.FINISHED_WITH_FAILURES, start = { retries++ }); library()
        compose.onNodeWithText("Retry failed files").performScrollTo().performClick()
        assertEquals(1, retries)
        compose.onNodeWithText("All files exported").assertDoesNotExist()
    }

    @Test fun runningExportCanBeStopped() {
        var stops = 0
        show(ExportPhaseUi.RUNNING, stop = { stops++ }); library()
        compose.onNodeWithText("Stop export").performScrollTo().performClick()
        assertEquals(1, stops)
    }

    @Test fun signedOutCannotReachLibraryOrGo() {
        controller.state.value = controller.state.value.copy(signIn = SignInState.SignedOut)
        show()
        compose.onNodeWithText("Go").assertDoesNotExist()
        compose.onNode(hasText("Library") and hasClickAction()).assertDoesNotExist()
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun signOutRemainsReachableOnASmallPhone() {
        show()
        compose.onNodeWithText("About").performClick()
        compose.onNodeWithText("Sign out of Tapas").assertIsDisplayed().performClick()
        assertEquals(1, controller.signOutCalls)
        compose.onNodeWithText("Go").assertDoesNotExist()
    }
}

private class ScreenController : AppController {
    override val state = MutableStateFlow(UiFixtures.coldStart.copy(
        signIn = SignInState.SignedIn("tapas"),
        library = listOf(
            LibraryEntry("z", "Zulu Comic", LibraryKind.COMIC, 8, 2),
            LibraryEntry("b", "Beta Novel", LibraryKind.NOVELS, 5, 3),
            LibraryEntry("a", "Alpha Comic", LibraryKind.COMIC, 9, 8),
        ),
    ))
    var goCalls = 0
    var pauseCalls = 0
    var signOutCalls = 0
    override fun go() { goCalls++; state.value = state.value.copy(download = DownloadState.Planning) }
    override fun pause() { pauseCalls++; state.value = state.value.copy(download = DownloadState.Paused("user")) }
    override fun selectSite(key: String) {}
    override fun startSignIn() {}
    override fun cancelSignIn() {}
    override fun signOut() {
        signOutCalls++
        state.value = state.value.copy(signIn = SignInState.SignedOut)
    }
    override fun stop() {}
    override fun requestExport(folderUri: String) {}
    override fun cancelExport() {}
    override fun openNotice(notice: Notice) {}
}
