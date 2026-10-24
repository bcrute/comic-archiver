package io.github.bcrute.comicarchiver.ui

import android.app.Application
import android.view.View
import android.os.SystemClock
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.hasAnyChild
import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.compose.ui.graphics.Color
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import io.github.bcrute.comicarchiver.controller.AppTheme
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.controller.CatalogChoice
import io.github.bcrute.comicarchiver.controller.CatalogState
import io.github.bcrute.comicarchiver.controller.SavedFilePage
import io.github.bcrute.comicarchiver.controller.SavedFileItem
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.DownloadObservations
import io.github.bcrute.comicarchiver.controller.ObservedWorker
import io.github.bcrute.comicarchiver.controller.LibraryOverview
import io.github.bcrute.comicarchiver.controller.LibraryMetric
import io.github.bcrute.comicarchiver.controller.LibraryPlace
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.DownloadSettings
import io.github.bcrute.comicarchiver.controller.SpeedChoice
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.runtime.WorkingLibrariesState
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryChoice
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryPhase
import io.github.bcrute.comicarchiver.worker.DownloadNotificationAccess
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

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

    private fun dashboardCloud() = cloudFixture(true).copy(overview = LibraryOverview(true,
        listOf(LibraryMetric("55", "Saved episodes"), LibraryMetric("112", "Saved files")),
        listOf(LibraryPlace("device", "This device", "53 episodes · 108 files"),
            LibraryPlace("b2", "Backblaze B2", "2 episodes · 4 files · Not connected")),
        "55 saved episodes. Choose a storage location below."))

    @Test fun dashboardStorageLinksOpenTheMatchingCatalogAndReturnWithoutGo() {
        controller.state.value = controller.state.value.copy(cloud = dashboardCloud(), catalog = catalogFixture(),
            cloudCheckedAtMillis = SystemClock.elapsedRealtime())
        show()
        compose.onNodeWithContentDescription("View stored episodes on This device. 53 episodes · 108 files")
            .performScrollTo().performClick()
        compose.onNodeWithText("Files").assertIsSelected()
        assertEquals("device", controller.catalogRequests.last().first.location)
        compose.onNode(hasText("Saving") and hasClickAction()).performClick()
        compose.onNodeWithText("Change saving destination").performScrollTo().performClick()
        compose.onNode(hasText("Storage") and hasClickAction()).assertIsSelected()
        compose.onNodeWithText("View stored episodes on Backblaze B2").performScrollTo().performClick()
        compose.onNodeWithText("Files").assertIsSelected()
        assertEquals("b2", controller.catalogRequests.last().first.location)
        compose.onNode(hasText("Saving") and hasClickAction()).performClick()
        compose.onNodeWithText("View saved files").performScrollTo().performClick()
        assertEquals("all", controller.catalogRequests.last().first.location)
        assertEquals(0, controller.goCalls)
        assertTrue(controller.cloudRequests.isEmpty())
    }

    @Test fun delayedDashboardKeepsPauseAndDoesNotInventZeroCounts() {
        controller.state.value = controller.state.value.copy(download = DownloadState.Running(0, 0),
            observations = DownloadObservations(SystemClock.elapsedRealtime(), ObservedWorker.WORKING),
            statusReadFailed = true, cloudReadFailed = true,
            cloud = dashboardCloud().copy(overview = LibraryOverview(false, emptyList(), emptyList(),
                "Saved file counts are unavailable. Open View saved files to retry.")))
        show()
        compose.onNodeWithText("Status unknown").assertIsNotEnabled()
        compose.onNodeWithText("Status updates are delayed. The downloader's current activity is unknown. You can still Pause if saving was running.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Saved episodes: 0").assertDoesNotExist()
        compose.onNodeWithText("Saved file counts are unavailable. Open View saved files to retry.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Pause").performScrollTo().assertIsEnabled().performClick()
        compose.onNodeWithText("Go").assertIsEnabled()
        assertEquals(1, controller.pauseCalls)
        assertEquals(0, controller.goCalls)
    }

    @Test fun recoveredDashboardUpdatesObservationsWithoutRestartingTheDownload() {
        controller.state.value = controller.state.value.copy(download = DownloadState.Running(0, 0),
            observations = DownloadObservations(SystemClock.elapsedRealtime(), ObservedWorker.WORKING,
                savedEpisodes = 53), statusReadFailed = true)
        show()
        compose.onNodeWithText("Status updates are delayed. The downloader's current activity is unknown. You can still Pause if saving was running.")
            .performScrollTo().assertIsDisplayed()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(statusReadFailed = false,
            observations = DownloadObservations(SystemClock.elapsedRealtime(), ObservedWorker.READY, savedEpisodes = 54)) }
        compose.onNodeWithText("54 saved episodes recorded").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Status updates are delayed. The downloader's current activity is unknown. You can still Pause if saving was running.")
            .assertDoesNotExist()
        compose.onNodeWithText("Pause").performScrollTo().assertIsEnabled()
        compose.onNodeWithText("Running").assertIsNotEnabled()
        assertEquals(0, controller.goCalls)
    }

    @Test
    @Config(qualifiers = "w320dp-h480dp")
    fun largeTextDashboardKeepsEveryStorageAndSettingsActionReachable() {
        val actions = mutableListOf<String>()
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme { DownloadsScreen(UiFixtures.coldStart.copy(signIn = SignInState.SignedIn("tapas"),
                    cloud = dashboardCloud().copy(timeEstimate = "About an hour remains. This is a rough estimate; Android may limit background saving.",
                        spaceEstimate = "The complete library may need more space than this device currently has free.")).toDownloadsScreenUi(),
                    { actions.add("go") }, { actions.add("pause") },
                    onDownloadSettings = { actions.add("settings") }, onStorageSettings = { actions.add("storage") },
                    onOpenCatalog = { actions.add(it) }) }
            }
        }
        compose.onNodeWithText("Open download settings").performScrollTo().assertIsDisplayed().performClick()
        compose.onNodeWithContentDescription("View stored episodes on This device. 53 episodes · 108 files")
            .performScrollTo().assertIsDisplayed().performClick()
        compose.onNodeWithContentDescription("View stored episodes on Backblaze B2. 2 episodes · 4 files · Not connected")
            .performScrollTo().assertIsDisplayed().performClick()
        compose.onNodeWithText("View saved files").performScrollTo().assertIsDisplayed().performClick()
        compose.onNodeWithText("Change saving destination").performScrollTo().assertIsDisplayed().performClick()
        assertEquals(listOf("settings", "device", "b2", "all", "storage"), actions)
    }

    @Test fun busyDashboardKeepsAllMutationAndNavigationControlsDisabled() {
        controller.state.value = controller.state.value.copy(libraryBusy = true, cloud = dashboardCloud())
        show()
        compose.onNodeWithText("Go").assertIsNotEnabled()
        for (text in listOf("Open download settings", "View saved files", "Change saving destination"))
            compose.onNodeWithText(text).performScrollTo().assertIsNotEnabled()
        compose.onNodeWithContentDescription("View stored episodes on This device. 53 episodes · 108 files")
            .performScrollTo().assertIsNotEnabled()
        assertEquals(0, controller.goCalls)
        assertTrue(controller.catalogRequests.isEmpty())
    }

    @Test fun notificationAccessAndSettingsReturnNeverAdmitSaving() {
        val access = mutableStateOf(DownloadNotificationAccess.ASK_PERMISSION)
        var requests = 0
        compose.setContent { MaterialTheme {
            ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {},
                notificationAccess = access.value, onNotificationAccess = {
                    requests++
                    access.value = if (requests == 1) DownloadNotificationAccess.SETTINGS else DownloadNotificationAccess.ENABLED
                })
        } }
        compose.onNodeWithText("Allow download notifications").performScrollTo().performClick()
        assertEquals(1, requests)
        assertEquals(0, controller.goCalls)
        compose.onNodeWithText("Go").performScrollTo().assertIsEnabled().performClick()
        assertEquals(1, controller.goCalls)
        compose.onNodeWithText("Notification settings").performScrollTo().performClick()
        assertEquals(2, requests)
        compose.onNodeWithText("Notification settings").assertDoesNotExist()
        compose.onNodeWithText("Allow download notifications").assertDoesNotExist()
        assertEquals(1, controller.goCalls)
    }

    @Test
    @Config(qualifiers = "w320dp-h640dp")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun blockedNotificationSettingsRemainReachableAtDoubleTextSize() {
        var requests = 0
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {},
                        notificationAccess = DownloadNotificationAccess.SETTINGS,
                        notificationError = "Open this app's settings from your device settings.",
                        onNotificationAccess = { requests++ })
                }
            }
        }
        compose.onNodeWithText("Notification settings").performScrollTo().assertIsDisplayed().performClick()
        assertEquals(1, requests)
        assertEquals(0, controller.goCalls)
        compose.onNodeWithText("Go").performScrollTo().assertIsEnabled()
    }

    @Test fun aRecordedNetworkHoldDoesNotClaimTheReconnectedRouteIsMeteredOrAutoResume() {
        controller.state.value = controller.state.value.copy(
            download = DownloadState.Paused(BetaCodes.METERED_HOLD),
            settings = controller.state.value.settings?.copy(connection = "wifi", metered = false, allowed = true))
        show()
        compose.onNodeWithText("Saving was held by your network setting. Check your connection and metered-data choice in Settings, then press Go.")
            .assertIsDisplayed()
        compose.onNodeWithText("Go").assertIsEnabled()
        compose.onNodeWithText("Pause").assertIsNotEnabled()
        assertEquals(0, controller.goCalls)
    }

    @Test fun signedOutLibraryCanOpenAFolderWithoutStartingSavingOrChangingExport() {
        controller.state.value = controller.state.value.copy(signIn = SignInState.SignedOut)
        var folder = 0; var load = 0; var export = 0
        compose.setContent {
            MaterialTheme { ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED),
                { export++ }, {}, {}, onOpenFolder = { folder++ }, onFolderLoad = { load++ }) }
        }
        library()
        compose.onNodeWithText("Folder").performScrollTo().performClick()
        compose.onNodeWithText("Open library folder").performScrollTo().performClick()
        assertEquals(1, folder)
        assertEquals(1, load)
        assertEquals(0, export)
        assertEquals(0, controller.goCalls)
        assertTrue(controller.catalogRequests.isEmpty())
    }

    // Compose 1.7.5's String semantics rebuilds MultiParagraph with the parent's
    // constraints, rather than the text's measured width. In a scrolling Row this
    // makes didOverflowWidth compare against an unbounded synthetic paragraph.
    // Check the complete, unellipsized line against the actual measured text box.
    private fun fitsSingleLine(layout: TextLayoutResult): Boolean =
        layout.lineCount == 1 && !layout.isLineEllipsized(0) &&
            layout.getLineStart(0) == 0 &&
            layout.getLineEnd(0, visibleEnd = true) == layout.layoutInput.text.length &&
            layout.getLineLeft(0) >= 0f && layout.getLineRight(0) <= layout.size.width.toFloat()

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
        compose.onNodeWithText("Last export verified.").assertDoesNotExist()
    }

    @Test fun runningExportCanBeStopped() {
        var stops = 0
        show(ExportPhaseUi.RUNNING, stop = { stops++ }); library()
        compose.onNodeWithText("Stop export").performScrollTo().performClick()
        assertEquals(1, stops)
    }

    @Test fun aCompletedBatchCanExportNewFilesOrChangeFolderWithoutGo() {
        var starts = 0; var picks = 0
        show(ExportPhaseUi.ALL_VERIFIED, start = { starts++ }, pick = { picks++ }); library()
        compose.onNodeWithText("Last export verified.").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Export all").performScrollTo().performClick()
        compose.onNodeWithText("Change export folder").performScrollTo().performClick()
        assertEquals(1, starts)
        assertEquals(1, picks)
        assertEquals(0, controller.goCalls)
    }

    @Test fun settingsWriteExplicitPolicyAndPacingWithoutGo() {
        show()
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        val mobile = compose.onNodeWithContentDescription("Also save on metered and mobile connections")
        mobile.assertIsOff().performClick()
        mobile.assertIsOn()
        compose.onNodeWithText("Slow").performScrollTo().performClick().assertIsSelected()
        compose.onNodeWithText("Fast").performScrollTo().assertIsNotEnabled()
        assertEquals(false, controller.state.value.settings?.wifiOnly)
        assertEquals("slow", controller.state.value.settings?.speed)
        assertEquals(0, controller.goCalls)
    }

    @Test fun speedCannotBeChangedWhileSavingButNetworkChoiceStaysAvailable() {
        controller.state.value = controller.state.value.copy(download = DownloadState.Planning)
        show()
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        compose.onNodeWithText("Slow").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Pause saving to change the speed.").assertIsDisplayed()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertIsEnabled()
    }

    @Test fun unreadableSettingsDoNotOfferAnUnverifiedMobileDataToggle() {
        controller.state.value = controller.state.value.copy(settings = null,
            settingsNotice = Notice("job_failed", "Download settings could not be read."))
        show()
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertDoesNotExist()
        compose.onNodeWithText("Retry settings").performClick()
        assertEquals(1, controller.settingsRefreshes)
        assertEquals(0, controller.goCalls)
    }

    @Test fun appearanceChangesThePaletteEvenWhenDownloadSettingsAreUnavailable() {
        controller.state.value = controller.state.value.copy(settings = null)
        var background: Color? = null
        compose.setContent {
            val state by controller.state.collectAsState()
            ComicArchiverTheme(state.theme) {
                val color = MaterialTheme.colorScheme.background
                SideEffect { background = color }
                ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
            }
        }
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        compose.onNodeWithText("Dark").performClick().assertIsSelected()
        compose.runOnIdle { assertEquals(darkColorScheme().background, background) }
        compose.onNodeWithText("Light").performClick().assertIsSelected()
        compose.runOnIdle { assertEquals(lightColorScheme().background, background) }
        assertEquals(0, controller.goCalls)
    }

    @Test fun signedOutCanBrowseSavedLibraryWithoutOfferingGo() {
        controller.state.value = controller.state.value.copy(signIn = SignInState.SignedOut)
        show()
        compose.onNodeWithText("Go").assertDoesNotExist()
        library()
        compose.onNodeWithText("Find saved series").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test fun signedOutFilesSearchSortAndPagingReachTheCatalogWithoutGo() {
        controller.state.value = controller.state.value.copy(signIn = SignInState.SignedOut,
            catalog = catalogFixture())
        show(); library()
        compose.onNodeWithText("Files").performClick()
        compose.onNodeWithText("Find saved files").performTextInput("  one  ")
        compose.onNodeWithText("Search").performClick()
        assertEquals("one", controller.catalogRequests.last().first.query)
        compose.onNodeWithText("Sort by: Newest saved").performClick()
        compose.onNodeWithText("Series A–Z").performClick()
        assertEquals("series", controller.catalogRequests.last().first.sort)
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText("Next"))
        compose.onNodeWithText("Next").performClick()
        assertEquals(50, controller.catalogRequests.last().first.offset)
        assertEquals(0, controller.goCalls)
    }

    @Test fun onlyARegisteredPortableArchiveOffersOpen() {
        controller.state.value = controller.state.value.copy(catalog = catalogFixture())
        show(); library(); compose.onNodeWithText("Files").performClick()
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText("Open"))
        compose.onNodeWithText("Open").performClick()
        assertEquals(listOf("a".repeat(64)), controller.openRequests)
        assertEquals(0, controller.goCalls)
    }

    @Test fun repeatedArchiveActionsIdentifyTheirFilesAndOpenMatchingIds() {
        val comic = SavedFileItem("a".repeat(64), "Shared title", "Chapter one", "first.cbz",
            "Comic", "4 B", "This device", null, false, "a".repeat(64))
        val novel = comic.copy(key = "b".repeat(64), filename = "second.epub", type = "Novel",
            openId = "b".repeat(64))
        val metadata = comic.copy(key = "metadata", filename = "chapter.json", type = "Data", openId = null)
        val catalog = catalogFixture().let { it.copy(page = it.page!!.copy(
            rows = listOf(comic, novel, metadata), total = 3, range = "Showing 1–3 of 3")) }
        compose.setContent { MaterialTheme { SavedFilesScreen(catalog, { _, _ -> }, controller::openSavedFile) {} } }
        for ((description, id) in listOf(
            "Open Chapter one, Shared title. first.cbz. This device" to comic.openId,
            "Open Chapter one, Shared title. second.epub. This device" to novel.openId,
        )) {
            compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
            compose.onNodeWithContentDescription(description).assertIsDisplayed().assertIsEnabled()
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button)).performClick()
            assertEquals(id, controller.openRequests.last())
        }
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText("chapter.json"))
        compose.onNodeWithContentDescription("chapter.json", substring = true).assertDoesNotExist()
        assertEquals(listOf(comic.openId, novel.openId), controller.openRequests)
        assertEquals(0, controller.goCalls)
    }

    @Test fun archiveActionsKeepContextAndDisableWhileOpeningThenRecover() {
        val first = catalogFixture().page!!.rows.single()
        val second = first.copy(key = "b".repeat(64), filename = "next.epub", type = "Novel", openId = "b".repeat(64))
        val state = mutableStateOf(catalogFixture().let { it.copy(
            page = it.page!!.copy(rows = listOf(first, second), total = 2), openingId = first.openId) })
        compose.setContent { MaterialTheme { SavedFilesScreen(state.value, { _, _ -> }, controller::openSavedFile) {} } }
        val opening = "Opening Chapter one, Synthetic series. chapter.cbz. This device"
        val next = "Open Chapter one, Synthetic series. next.epub. This device"
        for (description in listOf(opening, next)) {
            compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
            compose.onNodeWithContentDescription(description).assertIsNotEnabled()
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
        }
        compose.runOnIdle { state.value = state.value.copy(openingId = null,
            feedbackFor = first.openId, feedback = "No app could open this file. Choose a CBZ or EPUB reader and try again.") }
        val retry = "Open Chapter one, Synthetic series. chapter.cbz. This device"
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(retry))
        compose.onNodeWithText("No app could open this file. Choose a CBZ or EPUB reader and try again.").assertIsDisplayed()
        compose.onNodeWithContentDescription(retry).assertIsEnabled().performClick()
        assertEquals(listOf(first.openId), controller.openRequests)
        assertEquals(0, controller.goCalls)
    }

    @Test fun workingLibraryActionsIdentifyChoicesAndSelectTheirMatchingIds() {
        val selected = mutableListOf<String>()
        val choices = listOf(
            WorkingLibraryChoice("default", "Original library", true, true, false),
            WorkingLibraryChoice("copy-one", "Reading copy", false, true, true),
            WorkingLibraryChoice("copy-two", "Reading copy (copy 2)", false, true, true),
        )
        compose.setContent { MaterialTheme { WorkingLibrariesScreen(
            WorkingLibrariesState(enabled = true, choices = choices), WorkingLibraryActions(select = { selected.add(it) })) } }
        for ((description, id) in listOf(
            "Reopen working library: Original library" to "default",
            "Use working library: Reading copy" to "copy-one",
            "Use working library: Reading copy (copy 2)" to "copy-two",
        )) {
            compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(description))
            compose.onNodeWithContentDescription(description).assertIsDisplayed().assertIsEnabled()
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button)).performClick()
            assertEquals(id, selected.last())
        }
        assertEquals(choices.map { it.id }, selected)
    }

    @Test fun heldBusyAndUnavailableLibrariesKeepContextAndDisableSelection() {
        val selected = mutableListOf<String>()
        val initial = WorkingLibrariesState(enabled = true, choices = listOf(
            WorkingLibraryChoice("default", "Original library", true, true, false),
            WorkingLibraryChoice("unavailable", "Retained copy", false, false, true),
        ))
        val state = mutableStateOf(initial)
        compose.setContent { MaterialTheme { WorkingLibrariesScreen(state.value, WorkingLibraryActions(select = { selected.add(it) })) } }
        val current = "Reopen working library: Original library"
        val unavailable = "Use working library: Retained copy"
        for (blocked in listOf(initial.copy(pending = true), initial.copy(loading = true),
            initial.copy(phase = WorkingLibraryPhase.SELECTING))) {
            compose.runOnIdle { state.value = blocked }
            for (description in listOf(current, unavailable)) {
                compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(description))
                compose.onNodeWithContentDescription(description).assertIsNotEnabled()
                    .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
            }
        }
        compose.runOnIdle { state.value = initial }
        compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(unavailable))
        compose.onNodeWithContentDescription(unavailable).assertIsNotEnabled()
        compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(current))
        compose.onNodeWithContentDescription(current).assertIsEnabled().performClick()
        assertEquals(listOf("default"), selected)
    }

    @Test fun catalogFailureKeepsAnHonestRetryAndHidesStaleRows() {
        controller.state.value = controller.state.value.copy(catalog = catalogFixture().copy(
            error = "Saved files could not be loaded. Try Refresh files."))
        show(); library(); compose.onNodeWithText("Files").performClick()
        compose.onNodeWithText("Saved files could not be loaded. Try Refresh files.").assertIsDisplayed()
        compose.onNodeWithText("chapter.cbz").assertDoesNotExist()
        compose.onNodeWithText("Refresh files").performClick()
        assertTrue(controller.catalogRequests.last().second)
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun largeTextFilesCanScrollPastTheControlsToOpen() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme { SavedFilesScreen(catalogFixture(), { _, _ -> }, controller::openSavedFile) {} }
            }
        }
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText("Open"))
        compose.onNodeWithText("Open").assertIsDisplayed().performClick()
        assertEquals(listOf("a".repeat(64)), controller.openRequests)
    }

    @Test
    @Config(qualifiers = "w320dp-h480dp")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun largeTextDashboardFilesRouteKeepsRefreshWholeAndActionable() {
        controller.state.value = controller.state.value.copy(cloud = dashboardCloud(), catalog = catalogFixture())
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme { ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {}) }
            }
        }
        compose.onNodeWithText("View saved files").performScrollTo().performClick()
        compose.onNodeWithText("Files").assertIsSelected()
        val layouts = mutableListOf<TextLayoutResult>()
        compose.onNodeWithText("Refresh files", useUnmergedTree = true).performScrollTo()
            .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
        assertEquals(1, layouts.size)
        assertTrue("The whole Refresh files label must fit on one line", fitsSingleLine(layouts.single()))
        compose.onNodeWithText("Refresh files").assertIsDisplayed().assertIsEnabled().performClick()
        assertTrue(controller.catalogRequests.last().second)
        assertEquals(0, controller.goCalls)
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    // Legacy ShadowPaint measures character counts; overflow needs real font metrics.
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun largeTextNavigationKeepsWordsWholeAndCanReachAbout() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
                }
            }
        }
        val layouts = mutableListOf<TextLayoutResult>()
        compose.onNodeWithText("Settings", useUnmergedTree = true).performScrollTo()
            .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
        assertTrue(layouts.isNotEmpty())
        assertTrue("Tab text layouts: " + layouts.joinToString { "lines=${it.lineCount}, " +
            "overflow=${it.didOverflowWidth}, size=${it.size}, paragraphWidth=${it.multiParagraph.width}, " +
            "intrinsic=${it.multiParagraph.maxIntrinsicWidth}, constraints=${it.layoutInput.constraints}, " +
            "lineBounds=${it.getLineLeft(0)}..${it.getLineRight(0)}, density=${it.layoutInput.density}" },
            layouts.all(::fitsSingleLine))
        compose.onNodeWithText("About").performScrollTo().performClick()
        compose.onNodeWithText("Sign out of Tapas").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun singleLineFitRejectsClippedAndWrappedLabels() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    Column {
                        Text("Settings", Modifier.testTag("fit-natural"), maxLines = 1, softWrap = false)
                        Text("Settings", Modifier.width(20.dp).testTag("fit-clipped"),
                            maxLines = 1, softWrap = false)
                        Text("Settings menu", Modifier.width(20.dp).testTag("fit-wrapped"))
                    }
                }
            }
        }
        fun fits(tag: String): Boolean {
            val layouts = mutableListOf<TextLayoutResult>()
            compose.onNodeWithTag(tag, useUnmergedTree = true)
                .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
            assertEquals(1, layouts.size)
            return fitsSingleLine(layouts.single())
        }
        assertTrue("A complete natural-width label fits", fits("fit-natural"))
        assertTrue("A clipped label must fail the same fit check", !fits("fit-clipped"))
        assertTrue("A wrapped label must fail the same fit check", !fits("fit-wrapped"))
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun largeTextSeriesAndExportControlsRemainReachableOnAShortScreen() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
                }
            }
        }
        library()
        compose.onNodeWithTag("series-library-list").performScrollToNode(hasText("Alpha Comic"))
        compose.onNodeWithText("Alpha Comic").assertIsDisplayed()
        compose.onNodeWithText("8 of 9 saved · CBZ").assertIsDisplayed()
        compose.onNodeWithText("Choose export folder").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    /** Real insets delivery plus a constrained remaining viewport: the old fixed
     * navigation leaves no editor space here. Platform pan/resize still needs the
     * actual-APK keyboard check, separate from these rendered layout regressions. */
    private fun keyboardInsets(view: View, visible: Boolean) {
        ViewCompat.dispatchApplyWindowInsets(view, WindowInsetsCompat.Builder()
            .setInsets(WindowInsetsCompat.Type.ime(), Insets.of(0, 0, 0, if (visible) 800 else 0))
            .setVisible(WindowInsetsCompat.Type.ime(), visible).build())
    }

    private fun editorFitsViewport(label: String) {
        val editor = compose.onNodeWithText(label).getUnclippedBoundsInRoot()
        val viewport = compose.onNodeWithTag("editor-viewport").getUnclippedBoundsInRoot()
        assertTrue("Editor must have a positive height", editor.bottom > editor.top)
        assertTrue("Whole editor $editor must fit in $viewport",
            editor.top >= viewport.top && editor.bottom <= viewport.bottom)
    }

    @Test
    @Config(qualifiers = "w891dp-h411dp")
    fun landscapeKeyboardKeepsLargeTextSeriesSearchVisibleAndPreservesItsQuery() {
        val viewportHeight = mutableStateOf(411.dp)
        lateinit var view: View
        compose.setContent {
            val localView = LocalView.current
            SideEffect { view = localView }
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    Box(Modifier.height(viewportHeight.value).testTag("editor-viewport")) {
                        ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {},
                            workingLibraries = WorkingLibrariesState(enabled = true))
                    }
                }
            }
        }
        library()
        compose.onNodeWithText("Find saved series").performScrollTo().performClick()
        compose.runOnIdle {
            viewportHeight.value = 160.dp
            keyboardInsets(view, true)
        }
        compose.onNodeWithText("Find saved series").performScrollTo().performTextInput("CompactNoMatch")
        compose.onNodeWithText("CompactNoMatch").assertIsDisplayed()
        editorFitsViewport("Find saved series")
        compose.onNodeWithText("Clear search").assertIsDisplayed()
        compose.runOnIdle {
            keyboardInsets(view, false)
            viewportHeight.value = 411.dp
        }
        compose.onNode(hasText("Library") and hasClickAction()).assertIsDisplayed()
        compose.onNodeWithText("Series").assertIsSelected()
        compose.onNodeWithText("CompactNoMatch").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Clear search").performClick()
        compose.onNodeWithText("3 of 3 series").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test
    @Config(qualifiers = "w891dp-h411dp")
    fun landscapeKeyboardKeepsB2DraftVisibleWithoutConnectingOrChangingPage() {
        controller.state.value = controller.state.value.copy(cloud = cloudFixture())
        val viewportHeight = mutableStateOf(411.dp)
        lateinit var view: View
        compose.setContent {
            val localView = LocalView.current
            SideEffect { view = localView }
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    Box(Modifier.height(viewportHeight.value).testTag("editor-viewport")) {
                        ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
                    }
                }
            }
        }
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        compose.onNodeWithText("B2 setup").performScrollTo().performClick()
        compose.onNodeWithText("Bucket name").performScrollTo().performClick()
        compose.runOnIdle {
            viewportHeight.value = 160.dp
            keyboardInsets(view, true)
        }
        compose.onNodeWithText("Bucket name").performScrollTo().performTextInput("-compact")
        editorFitsViewport("Bucket name")
        compose.runOnIdle {
            keyboardInsets(view, false)
            viewportHeight.value = 411.dp
        }
        compose.onNodeWithText("B2 setup").assertIsSelected()
        compose.onNodeWithText("fixture-library-compact").performScrollTo().assertIsDisplayed()
        assertTrue(controller.cloudRequests.isEmpty())
        assertEquals(0, controller.goCalls)
    }

    @Test
    @Config(qualifiers = "w320dp-h640dp")
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun narrowLargeTextThemeAndSpeedChoicesStayWholeAndCanBeSelectedWithoutGo() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
                }
            }
        }
        compose.onNode(hasText("Settings") and hasClickAction()).performScrollTo().performClick()
        fun wholeLabel(label: String) {
            val layouts = mutableListOf<TextLayoutResult>()
            compose.onNodeWithText(label, useUnmergedTree = true)
                .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
            assertTrue("The complete $label label must fit on one line",
                layouts.isNotEmpty() && layouts.all(::fitsSingleLine))
        }
        compose.onNodeWithText("Dark").performScrollTo().assertIsDisplayed()
        wholeLabel("Dark")
        compose.onNodeWithText("Dark").performClick().assertIsSelected()
        assertEquals(AppTheme.DARK, controller.state.value.theme)
        compose.onNodeWithText("Download speed").performScrollTo().assertIsDisplayed()
        // Reveal the whole row vertically before scrolling its choices
        // horizontally. Scrolling the heading alone can leave the row below it.
        compose.onNode(hasScrollAction() and hasAnyChild(hasText("Slow"))).performScrollTo()
        compose.onNodeWithText("Slow").performScrollTo().assertIsDisplayed()
        wholeLabel("Slow")
        compose.onNodeWithText("Slow").performClick().assertIsSelected()
        assertEquals("slow", controller.state.value.settings?.speed)
        assertEquals(0, controller.goCalls)
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

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun aboutUsesInstalledIdentityAndHelpCanRetryWithoutStartingSaving() {
        val app = RuntimeEnvironment.getApplication()
        val apk = java.io.File(app.cacheDir, "installed-identity.apk").apply { writeText("abc") }
        // Preserve the activity registration while replacing only installed identity facts.
        val pkg = shadowOf(app.packageManager).getInternalMutablePackageInfo(app.packageName)
        val opened = mutableListOf<String>()
        compose.setContent {
            CompositionLocalProvider(
                LocalDensity provides Density(LocalDensity.current.density, 2f),
                LocalUriHandler provides object : UriHandler {
                    override fun openUri(uri: String) {
                        opened.add(uri)
                        if (opened.size == 1) throw IllegalStateException("private-browser-detail")
                    }
                },
            ) {
                MaterialTheme {
                    ComicArchiverAppRoot(controller, ExportPanelStateUi(ExportPhaseUi.NOT_PICKED), {}, {}, {})
                }
            }
        }
        compose.runOnIdle {
            pkg.versionName = "installed-fixture"
            pkg.setLongVersionCode(4_294_967_333L)
            pkg.applicationInfo!!.sourceDir = apk.path
        }
        compose.onNodeWithText("About").performScrollTo().performClick()
        compose.waitUntil(5_000) {
            compose.onAllNodes(hasText("APK fingerprint: ba7816bf8f01cfea")).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("Version installed-fixture (build 4294967333)").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("APK fingerprint: ba7816bf8f01cfea").performScrollTo().assertIsDisplayed()
        compose.onNode(hasText("· Uninstalling removes app-private libraries and sign-ins. Files already copied " +
            "to your chosen export folder or verified in your B2 bucket stay there.")).performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Read the official Tapas closure FAQ").performScrollTo().performClick()
        compose.onNodeWithText("Could not open the FAQ. Check that a browser is available and try again.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("private-browser-detail").assertDoesNotExist()
        compose.onNodeWithText("Read the official Tapas closure FAQ").performScrollTo().performClick()
        compose.onNodeWithText("Could not open the FAQ. Check that a browser is available and try again.")
            .assertDoesNotExist()
        assertEquals(listOf("https://tapas.io/newsfeed/271", "https://tapas.io/newsfeed/271"), opened)
        compose.onNodeWithText("Sign out of Tapas").assertIsDisplayed()
        compose.onNodeWithText("Saving").performScrollTo().performClick()
        compose.onNodeWithText("Go").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls)
        assertEquals(0, controller.signOutCalls)
    }

    @Test
    @Config(sdk = [24])
    @Suppress("DEPRECATION")
    fun aboutKeepsVersionWhenApkCannotBeReadOnTheOldestSupportedApi() {
        val app = RuntimeEnvironment.getApplication()
        val pkg = shadowOf(app.packageManager).getInternalMutablePackageInfo(app.packageName)
        val absent = java.io.File(app.cacheDir, "unreadable-private-apk.apk")
        show()
        compose.runOnIdle {
            pkg.versionName = "older-api-fixture"
            pkg.versionCode = 37
            pkg.applicationInfo!!.sourceDir = absent.path
        }
        compose.onNodeWithText("About").performClick()
        compose.waitUntil(5_000) {
            compose.onAllNodes(hasText("APK fingerprint unavailable")).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("Version older-api-fixture (build 37)").assertIsDisplayed()
        compose.onNodeWithText("APK fingerprint unavailable").assertIsDisplayed()
        compose.onNodeWithText(absent.path).assertDoesNotExist()
        compose.onNodeWithText("Saving").performClick()
        compose.onNodeWithText("Go").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
        assertEquals(0, controller.signOutCalls)
    }
}

private fun catalogFixture() = CatalogState(page = SavedFilePage(
    rows = listOf(SavedFileItem("a".repeat(64), "Synthetic series", "Chapter one", "chapter.cbz",
        "Comic", "4 B", "This device", null, false, "a".repeat(64))),
    total = 101, offset = 0, limit = 50, summary = "101 saved files", range = "Showing 1–50 of 101",
    notice = "", locations = listOf(CatalogChoice("all", "All storage")),
    kinds = listOf(CatalogChoice("episodes", "Comics and novels")),
    sorts = listOf(CatalogChoice("saved_desc", "Newest saved"), CatalogChoice("series", "Series A–Z")),
    pageSizes = listOf(50, 100, 200),
))

private class ScreenController : AppController {
    override val state = MutableStateFlow(UiFixtures.coldStart.copy(
        signIn = SignInState.SignedIn("tapas"),
        settings = DownloadSettings(true, "medium", listOf(
            SpeedChoice("fast", "Fast", false), SpeedChoice("medium", "Medium", true),
            SpeedChoice("slow", "Slow", true)), "wifi", false, true),
        library = listOf(
            LibraryEntry("z", "Zulu Comic", LibraryKind.COMIC, 8, 2),
            LibraryEntry("b", "Beta Novel", LibraryKind.NOVELS, 5, 3),
            LibraryEntry("a", "Alpha Comic", LibraryKind.COMIC, 9, 8),
        ),
    ))
    var settingsRefreshes = 0
    var goCalls = 0
    var pauseCalls = 0
    var signOutCalls = 0
    val catalogRequests = mutableListOf<Pair<CatalogQuery, Boolean>>()
    val openRequests = mutableListOf<String>()
    val cloudRequests = mutableListOf<CloudRequest>()
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
    override fun loadCatalog(query: CatalogQuery, refresh: Boolean) {
        catalogRequests.add(query to refresh)
        state.value = state.value.copy(catalog = state.value.catalog.copy(query = query))
    }
    override fun openSavedFile(id: String) { openRequests.add(id) }
    override fun setTheme(value: AppTheme) { state.value = state.value.copy(theme = value) }
    override fun refreshCloud() {}
    override fun cloudAction(request: CloudRequest) { cloudRequests.add(request) }
    override fun refreshSettings() { settingsRefreshes++ }
    override fun setWifiOnly(value: Boolean) {
        state.value = state.value.copy(settings = state.value.settings?.copy(wifiOnly = value))
    }
    override fun setDownloadSpeed(value: String) {
        state.value = state.value.copy(settings = state.value.settings?.copy(speed = value))
    }
    override fun requestExport(folderUri: String) {}
    override fun cancelExport() {}
    override fun openNotice(notice: Notice) {}
}
