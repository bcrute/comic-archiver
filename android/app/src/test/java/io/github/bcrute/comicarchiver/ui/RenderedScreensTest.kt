package io.github.bcrute.comicarchiver.ui

import android.app.Application
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
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
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import io.github.bcrute.comicarchiver.controller.AppTheme
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.controller.CatalogChoice
import io.github.bcrute.comicarchiver.controller.CatalogState
import io.github.bcrute.comicarchiver.controller.SavedFilePage
import io.github.bcrute.comicarchiver.controller.SavedFileItem
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.DownloadSettings
import io.github.bcrute.comicarchiver.controller.SpeedChoice
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
    override fun cloudAction(request: CloudRequest) {}
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
