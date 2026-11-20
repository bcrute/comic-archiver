package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.SupportWords
import io.github.bcrute.comicarchiver.controller.SupportNote
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
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.hasAnyChild
import androidx.compose.ui.test.hasAnyDescendant
import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextReplacement
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
import io.github.bcrute.comicarchiver.controller.CardRow
import io.github.bcrute.comicarchiver.controller.IssueCount
import io.github.bcrute.comicarchiver.controller.TypeCount
import io.github.bcrute.comicarchiver.controller.SiteCard
import io.github.bcrute.comicarchiver.controller.SpeedOption
import io.github.bcrute.comicarchiver.controller.AllSites
import io.github.bcrute.comicarchiver.controller.DownloadObservations
import io.github.bcrute.comicarchiver.controller.ObservedWorker
import io.github.bcrute.comicarchiver.controller.LibraryOverview
import io.github.bcrute.comicarchiver.controller.LibraryMetric
import io.github.bcrute.comicarchiver.controller.LibraryPlace
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.DownloadSettings
import io.github.bcrute.comicarchiver.controller.DownloadTypeChoice
import io.github.bcrute.comicarchiver.controller.EpisodeList
import io.github.bcrute.comicarchiver.controller.EpisodeRow
import io.github.bcrute.comicarchiver.controller.SiteChoice
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

    private fun library() {
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Series").performClick()
    }
    private fun export() {
        compose.onNodeWithContentDescription("Library options").performClick()
        compose.onNodeWithText("Export files").performClick()
    }
    private fun overview() {
        compose.onNodeWithTag("nav-settings").performClick()
        if (compose.onAllNodes(hasText("All settings") and hasClickAction()).fetchSemanticsNodes().isNotEmpty())
            compose.onNode(hasText("All settings") and hasClickAction()).performClick()
    }
    private fun preferences() {
        overview(); compose.onNodeWithText("Download preferences").performScrollTo().performClick()
    }
    private fun about() {
        overview(); compose.onNodeWithText("About").performScrollTo().performClick()
    }
    private fun support() {
        overview(); compose.onNodeWithText("Support").performScrollTo().performClick()
    }


    private fun dashboardCloud() = cloudFixture(true).copy(overview = LibraryOverview(true,
        listOf(LibraryMetric("55", "Saved episodes"), LibraryMetric("112", "Saved files")),
        listOf(LibraryPlace("device", "This device", "53 episodes · 108 files"),
            LibraryPlace("b2", "Backblaze B2", "2 episodes · 4 files · Not connected")),
        "55 saved episodes. Choose a storage location below."))

    @Test fun threePrimaryDestinationsKeepHelpInSettingsWithoutStartingDownloads() {
        show()
        compose.onNodeWithTag("nav-saving").assertIsSelected().assertIsDisplayed()
        compose.onNodeWithTag("nav-library").assertIsDisplayed()
        compose.onNodeWithTag("nav-settings").assertIsDisplayed()
        compose.onNodeWithText("App information, licenses and sign out").assertDoesNotExist()
        overview()
        compose.onNodeWithText("Download preferences").assertIsDisplayed()
        compose.onNodeWithText("Storage & destination").assertIsDisplayed()
        compose.onNodeWithText("Support").performScrollTo().performClick()
        compose.onNodeWithTag("nav-settings").assertIsSelected()
        compose.onNodeWithTag("support-email").assertIsDisplayed()
        compose.onNode(hasText("All settings") and hasClickAction()).performClick()
        compose.onNodeWithText("About").performScrollTo().performClick()
        compose.onNodeWithTag("nav-settings").assertIsSelected()
        compose.onNodeWithText("Sign out of Tapas").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test fun supportIsItsOwnSettingsPageWithOneEmailButtonAndAboutHasNoSupportItems() {
        show()
        about()
        compose.onNodeWithTag("support-email").assertDoesNotExist()
        compose.onNodeWithText("Copy details for support").assertDoesNotExist()
        support()
        compose.onNodeWithText("Need help?").assertIsDisplayed()
        compose.onNodeWithText("Email support opens a new email to ben@comicarchiver.com with the app\u2019s log attached. " +
            "Say what happened and send it.").assertIsDisplayed()
        compose.onNodeWithText("no titles, folder names or sign-in details", substring = true).assertIsDisplayed()
        compose.onNodeWithTag("support-note").assertDoesNotExist()
        compose.onNode(hasTestTag("support-email") and hasText("Email support")).performScrollTo().performClick()
        compose.runOnIdle { assertEquals(1, controller.supportEmails) }
        controller.state.value = controller.state.value.copy(supportNote = SupportNote(SupportWords.EMAIL_OPENED))
        compose.onNode(hasTestTag("support-note") and hasText("Your email app opened with the log attached."))
            .performScrollTo().assertIsDisplayed()
        // While a log is being made it cannot be asked for again; a log that went to Downloads is named.
        controller.state.value = controller.state.value.copy(supportBusy = true,
            supportNote = SupportNote(SupportWords.noEmailApp("Comic Archiver log 2026-11-11 09.30.05.txt"), failed = true))
        compose.onNodeWithTag("support-email").assertIsNotEnabled()
        compose.onNodeWithText("No email app was found, so the log is saved in your Downloads folder as " +
            "\u201cComic Archiver log 2026-11-11 09.30.05.txt\u201d. Send it to ben@comicarchiver.com from any email account.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("No email app? Email support then saves the log", substring = true).performScrollTo().assertIsDisplayed()
        compose.onNode(hasText("All settings") and hasClickAction()).performClick()
        compose.onNodeWithText("Download preferences").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test fun aboutNamesTheAuthorAndOpensEveryLicenseTheApkCarries() {
        show()
        about()
        compose.onNodeWithText(LICENSE_SUMMARY).performScrollTo().assertIsDisplayed()
        compose.onNode(hasText("Licenses") and hasClickAction()).performScrollTo().performClick()
        compose.onNodeWithTag("nav-settings").assertIsSelected()
        compose.waitUntil(5_000) { compose.onAllNodesWithText("Comic Archiver (MIT license)").fetchSemanticsNodes().isNotEmpty() }
        // The texts come from the APK's own assets, as packed by the build.
        for (title in listOf("Third-party notices", "Apache-2.0", "chaquopy", "python-3.13", "httpx: LICENSE.md", "certifi: LICENSE")) {
            compose.onNodeWithText(title).performScrollTo().assertIsDisplayed()
        }
        compose.onNodeWithText("Comic Archiver (MIT license)").performScrollTo().performClick()
        compose.waitUntil(5_000) {
            compose.onAllNodes(hasText("Copyright (c) 2026 Ben Crute", substring = true)).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNode(hasText("About") and hasClickAction()).performClick()
        compose.onNodeWithText(LICENSE_SUMMARY).performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test fun libraryOpensToFilesAndKeepsExportBehindItsOptions() {
        controller.state.value = controller.state.value.copy(catalog = catalogFixture())
        var picks = 0
        show(pick = { picks++ })
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Files").assertIsSelected()
        compose.onNodeWithText("Find saved files").assertIsDisplayed()
        compose.onNodeWithText("Choose export folder").assertDoesNotExist()
        export()
        compose.onNodeWithText("Choose export folder").performScrollTo().performClick()
        assertEquals(1, picks)
        assertEquals(0, controller.goCalls)
    }

    @Test fun aCardsButtonNamesWhatItDoesNowAndNavigationNeverRestartsIt() {
        show()
        compose.onNodeWithText("Start").assertIsEnabled().performClick()
        compose.onNodeWithText("Pause").assertIsEnabled().performClick()
        compose.onNodeWithText("Continue").assertIsEnabled()
        overview()
        compose.onNodeWithTag("nav-saving").performClick()
        compose.onNodeWithText("Continue").assertIsEnabled()
        assertEquals(1, controller.goCalls)
        assertEquals(1, controller.pauseCalls)
    }

    @Test fun connectionEntryWorksWithoutOpeningTheOptionalB2Guide() {
        controller.state.value = controller.state.value.copy(cloud = cloudFixture())
        show(); overview()
        compose.onNodeWithText("Backblaze B2").performScrollTo().performClick()
        compose.onNodeWithText("Bucket name").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("1. Use your own Backblaze account").assertDoesNotExist()
        compose.onNodeWithText("keyID").performScrollTo().performTextInput("FIXTURE_ID123")
        compose.onNodeWithText("applicationKey").performScrollTo().performTextInput("FIXTURE_KEY123")
        compose.onNodeWithText("Connect B2").performScrollTo().assertIsEnabled().performClick()
        val request = controller.cloudRequests.single() as CloudRequest.Connect
        assertEquals("fixture-library", request.bucket)
        assertEquals("FIXTURE_ID123", request.keyId)
        assertEquals("FIXTURE_KEY123", request.applicationKey)
        assertEquals(0, controller.goCalls)
    }

    @Test fun everySiteHasTheDesktopsCardWithItsOwnButtonsUnderTheSavedLibrary() {
        val rows = listOf(CardRow("Status", "Webtoon asked the app to slow down", true), CardRow("Saved", "12 episodes", false),
            CardRow("Time left", "~2 hours", false), CardRow("Space", "~1.5 GB", false), CardRow("Download speed", "1.2 MB/s", false))
        val speeds = listOf(SpeedOption("fast", "Fast"), SpeedOption("medium", "Medium"), SpeedOption("slow", "Slow"))
        controller.state.value = controller.state.value.copy(
            sites = listOf(SiteChoice("tapas", "Tapas", true), SiteChoice("webtoon", "Webtoon", true),
                SiteChoice("tappytoon", "Tappytoon", true)),
            allSites = AllSites(listOf("tapas", "webtoon"), 1210, 1300),
            siteCards = listOf(
                SiteCard("webtoon", "Webtoon", "Your download is paused", "Paused", "paused", "", rows, pill = "Paused",
                    action = "go", actionText = "Continue", actionEnabled = true, signOut = true, signOutEnabled = true,
                    speed = "medium", speedEnabled = true, speeds = speeds, selection = "Download: Paid series"),
                SiteCard("tapas", "Tapas", "Ready to save", "Ready", "neutral", "", listOf(CardRow("Status", "Ready to save", false)),
                    status = "Could not refresh progress.", statusError = true, action = "go", actionText = "Start",
                    actionEnabled = true, signOut = true, signOutEnabled = true)))
        show()
        // The saved library over every site comes first, with the two buttons that reach them all.
        compose.onNodeWithText("1,210").assertIsDisplayed()
        compose.onNodeWithText("Open library folder").assertIsDisplayed()
        compose.onNodeWithText("Start all").assertIsEnabled()
        compose.onNodeWithText("Pause all").assertDoesNotExist()
        // One card for each site, in the sites' order: no site is chosen first.
        val tops = listOf("library-overview", "site-card-tapas", "site-card-webtoon", "site-card-tappytoon")
            .map { compose.onNodeWithTag(it).getUnclippedBoundsInRoot().top }
        assertEquals(tops.sorted(), tops)
        compose.onNodeWithText("Could not refresh progress.").performScrollTo().assertIsDisplayed()
        for (text in listOf("12", "~2 hours", "~1.5 GB", "1.2 MB/s", "Webtoon asked the app to slow down", "Download: Paid series"))
            compose.onNodeWithText(text).performScrollTo().assertIsDisplayed()
        compose.onNode(hasTestTag("card-action-webtoon") and hasText("Continue")).performScrollTo().assertIsEnabled().performClick()
        assertEquals(listOf("webtoon"), controller.goSites)
        compose.onNodeWithTag("card-speed-webtoon").performScrollTo().performClick()
        compose.onNodeWithText("Slow").performClick()
        assertEquals(listOf("webtoon" to "slow"), controller.speedChanges)
        // Sign out of a site that is not the one shown shows the site first; nothing is signed out before its question.
        compose.onNodeWithTag("card-signout-webtoon").performScrollTo().performClick()
        assertEquals(listOf("webtoon"), controller.selected)
        assertEquals(0, controller.signOutCalls)
        // A site never opened has the desktop's empty card, and signs in from it.
        compose.onNodeWithText("Not signed in").performScrollTo().assertIsDisplayed()
        compose.onNodeWithTag("card-signout-tappytoon").assertDoesNotExist()
        compose.onNode(hasTestTag("card-action-tappytoon") and hasText("Sign in")).performScrollTo().performClick()
        assertEquals(listOf("tappytoon"), controller.signIns)
        assertEquals(0, controller.goCalls)
    }

    @Test fun aCardsEpisodeTypesAreChosenFromTheCardAndTheSignInPageTakesTheTabOnlyWhileOpen() {
        show()
        compose.onNodeWithTag("card-types-tapas").performScrollTo().performClick()
        compose.onNodeWithText("Download choices for Tapas").assertIsDisplayed()
        compose.onNodeWithContentDescription("Free").performClick()
        compose.onNodeWithText("Save choices").performClick()
        assertEquals(listOf(mapOf("bought" to true, "free" to true)), controller.typeChanges)
        compose.onNodeWithText("Download choices for Tapas").assertDoesNotExist()
        // Signed out, the dashboard stays: the card offers Sign in and says what the site answered.
        compose.runOnIdle { controller.state.value = controller.state.value.copy(signIn = SignInState.Failed("login_required")) }
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Sign in")).performScrollTo().assertIsEnabled()
        compose.onNodeWithTag("card-message-tapas").assertIsDisplayed()
        compose.onNodeWithTag("card-signout-tapas").assertDoesNotExist()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(signIn = SignInState.Page("https://example.invalid/")) }
        compose.onNodeWithTag("site-card-tapas").assertDoesNotExist()
        compose.onNodeWithText("Opening the Tapas sign-in page…").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test fun busyDashboardKeepsEveryCardAndStartAllDisabled() {
        controller.state.value = controller.state.value.copy(libraryBusy = true)
        show()
        compose.onNodeWithTag("card-action-tapas").assertIsNotEnabled()
        compose.onNodeWithTag("card-types-tapas").assertIsNotEnabled()
        compose.onNodeWithTag("start-all").assertIsNotEnabled()
        assertEquals(0, controller.goCalls)
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
        compose.onNodeWithTag("card-action-tapas").performScrollTo().assertIsEnabled().performClick()
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
        compose.onNodeWithTag("card-action-tapas").performScrollTo().assertIsEnabled()
    }

    @Test fun aRecordedNetworkHoldDoesNotClaimTheReconnectedRouteIsMeteredOrAutoResume() {
        controller.state.value = controller.state.value.copy(
            download = DownloadState.Paused(BetaCodes.METERED_HOLD),
            settings = controller.state.value.settings?.copy(connection = "wifi", metered = false, allowed = true))
        show()
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Continue")).assertIsEnabled()
        compose.onNodeWithText("Pause").assertDoesNotExist()
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
        val catalogBeforeFolder = controller.catalogRequests.toList()
        compose.onNodeWithText("Folder").performScrollTo().performClick()
        compose.onNodeWithText("Open library folder").performScrollTo().performClick()
        assertEquals(1, folder)
        assertEquals(1, load)
        assertEquals(0, export)
        assertEquals(0, controller.goCalls)
        assertEquals(catalogBeforeFolder, controller.catalogRequests)
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
        compose.onNodeWithText("Pause").assertDoesNotExist()
        compose.onNodeWithTag("card-action-tapas").performClick()
        compose.onNodeWithText("Pause all").assertIsDisplayed()
        compose.onNodeWithText("Pause").assertIsEnabled().performClick()
        compose.onNodeWithText("Pause all").assertDoesNotExist()
        compose.onNodeWithTag("card-action-tapas").assertIsEnabled()
        assertEquals(1, controller.goCalls)
        assertEquals(1, controller.pauseCalls)
    }

    @Test fun realProgressAndFailureReachTheRenderedScreen() {
        show()
        compose.runOnIdle {
            controller.state.value = controller.state.value.copy(download = DownloadState.Running(
                0, 0, "Downloading episodes…", "37 episodes saved · 41 image pages in this batch"))
        }
        compose.onNodeWithText("Pause").assertIsEnabled()
        compose.runOnIdle {
            controller.state.value = controller.state.value.copy(download = DownloadState.Failed("job_failed"))
        }
        compose.onNodeWithTag("card-action-tapas").assertIsEnabled()
        compose.onNodeWithText("Pause").assertDoesNotExist()
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
        compose.onNode(hasText("Downloads") and hasClickAction()).performClick()
        library()
        compose.onNodeWithText("1 of 3 series").assertIsDisplayed()
        restoration.emulateSavedInstanceStateRestore()
        compose.onNodeWithText("1 of 3 series").assertIsDisplayed()
        compose.onNodeWithText("Beta Novel").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls) // Navigation/restoration must never admit work.
    }

    @Test fun exportFolderPickerIsReachableWithoutStartingDownloads() {
        var picks = 0
        show(pick = { picks++ }); library(); export()
        compose.onNodeWithText("Choose export folder").performScrollTo().performClick()
        assertEquals(1, picks)
        assertEquals(0, controller.goCalls)
    }

    @Test fun retryExportCallsTheExporterAndDoesNotClaimSuccess() {
        var retries = 0
        show(ExportPhaseUi.FINISHED_WITH_FAILURES, start = { retries++ }); library(); export()
        compose.onNodeWithText("Retry failed files").performScrollTo().performClick()
        assertEquals(1, retries)
        compose.onNodeWithText("Last export verified.").assertDoesNotExist()
    }

    @Test fun runningExportCanBeStopped() {
        var stops = 0
        show(ExportPhaseUi.RUNNING, stop = { stops++ }); library(); export()
        compose.onNodeWithText("Stop export").performScrollTo().performClick()
        assertEquals(1, stops)
    }

    @Test fun aCompletedBatchCanExportNewFilesOrChangeFolderWithoutGo() {
        var starts = 0; var picks = 0
        show(ExportPhaseUi.ALL_VERIFIED, start = { starts++ }, pick = { picks++ }); library(); export()
        compose.onNodeWithText("Last export verified.").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Export all").performScrollTo().performClick()
        compose.onNodeWithText("Change export folder").performScrollTo().performClick()
        assertEquals(1, starts)
        assertEquals(1, picks)
        assertEquals(0, controller.goCalls)
    }

    @Test fun settingsWriteExplicitPolicyAndPacingWithoutGo() {
        show()
        preferences()
        val mobile = compose.onNodeWithContentDescription("Also save on metered and mobile connections")
        mobile.assertIsOff().performClick()
        mobile.assertIsOn()
        compose.onNodeWithText("Slow").performScrollTo().performClick().assertIsSelected()
        compose.onNodeWithText("Fast").performScrollTo().assertIsNotEnabled()
        assertEquals(false, controller.state.value.settings?.wifiOnly)
        assertEquals("slow", controller.state.value.settings?.speed)
        assertEquals(0, controller.goCalls)
    }

    @Test fun downloadPreferencesOpenWithTheShownSitesEpisodeTypesAndDownloadIssues() {
        show()
        preferences()
        // Before the site's card is read, nothing is claimed.
        compose.onNodeWithText("Download issues").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Not checked").assertIsDisplayed()
        compose.onNodeWithText("Tapas episodes").assertDoesNotExist()
        val card = SiteCard("tapas", "Tapas", "Ready to save", "Ready", "neutral", "", emptyList(),
            types = listOf(TypeCount("Free", "1,200", "7"), TypeCount("Bought / unlocked", "4", "—")))
        controller.state.value = controller.state.value.copy(siteCards = listOf(
            SiteCard("webtoon", "Webtoon", "t", "Ready", "neutral", "", emptyList(),
                issues = listOf(IssueCount("Incomplete content", "9"))), card))
        compose.onNodeWithText("Tapas episodes").performScrollTo().assertIsDisplayed()
        compose.onNode(hasText("Free") and hasText("1,200") and hasText("7")).assertExists()
        compose.onNode(hasText("Bought / unlocked") and hasText("4") and hasText("—")).assertExists()
        compose.onNodeWithText("No recorded issues").assertExists()
        // Another site's issues are not this one's.
        compose.onNodeWithText("Incomplete content").assertDoesNotExist()
        controller.state.value = controller.state.value.copy(siteCards = listOf(card.copy(issues = listOf(
            IssueCount("Download errors", "2"), IssueCount("Unsupported format", "1,204")))))
        compose.onNodeWithText("No recorded issues").assertDoesNotExist()
        compose.onNode(hasText("Download errors") and hasText("2")).assertExists()
        compose.onNode(hasText("Unsupported format") and hasText("1,204")).assertExists()
        assertEquals(0, controller.goCalls)
    }

    private fun episodes() {
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithTag("library-views").performScrollToNode(hasText("Episodes"))
        compose.onNodeWithText("Episodes").performClick()
    }

    @Test fun recordedEpisodesOpenBySiteThenSeriesInTheDesktopsWords() {
        controller.state.value = controller.state.value.copy(sites = listOf(
            SiteChoice("tapas", "Tapas", true), SiteChoice("webtoon", "Webtoon", true),
            SiteChoice("tappytoon", "Tappytoon", false, "Not in this build")))
        show()
        episodes()
        compose.onNodeWithText("Recorded episodes · expand a provider and series").assertIsDisplayed()
        compose.onNodeWithText("Saved can include cloud storage. This list does not check files on this device.")
            .assertIsDisplayed()
        // Nothing is read until a site is opened, and a site this build cannot open is not listed.
        assertEquals(emptyList<Pair<String, String?>>(), controller.episodeLoads)
        compose.onNodeWithText("Tappytoon").assertDoesNotExist()
        compose.onNode(hasTestTag("episodes-webtoon") and hasText("Expand to view")).assertExists()
        compose.onNodeWithTag("episodes-tapas").performClick()
        assertEquals(listOf<Pair<String, String?>>("tapas" to null), controller.episodeLoads)
        compose.onNode(hasTestTag("episodes-tapas") and hasText("Loading…")).assertExists()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(episodes = mapOf(
            "tapas" to EpisodeList(listOf(EpisodeRow("42", "Alpha Series", "1 / 3 saved", "CBZ", "Mixed")),
                nextOffset = 200, loaded = true))) }
        compose.onNode(hasTestTag("episodes-tapas") and hasText("Recorded list")).assertExists()
        compose.onNode(hasTestTag("episodes-tapas/42") and hasText("1 / 3 saved") and
            hasText("CBZ · Mixed")).assertExists()
        compose.onNodeWithText("More series…").performClick()
        compose.onNodeWithTag("episodes-tapas/42").performClick()
        assertEquals(listOf("tapas" to null, "tapas" to null, "tapas" to "42"), controller.episodeLoads)
        compose.runOnIdle { controller.state.value = controller.state.value.copy(episodes = controller.state.value.episodes +
            ("tapas/42" to EpisodeList(listOf(EpisodeRow("81", "1 · Episode 1", "Saved", "CBZ", "Free"),
                EpisodeRow("82", "2 · Episode 2", "Pending", "Unknown", "Unknown type")), loaded = true))) }
        compose.onNode(hasTestTag("episode-tapas/42/81") and hasAnyDescendant(hasText("1 · Episode 1")) and
            hasAnyDescendant(hasText("Saved")) and hasAnyDescendant(hasText("CBZ · Free"))).assertExists()
        compose.onNode(hasTestTag("episode-tapas/42/82") and hasAnyDescendant(hasText("Pending")) and
            hasAnyDescendant(hasText("Unknown · Unknown type"))).assertExists()
        compose.onNodeWithText("More episodes…").assertDoesNotExist()
        // Closing a series hides its episodes without reading anything again; opening it again shows them.
        compose.onNodeWithTag("episodes-tapas/42").performClick()
        compose.onNodeWithTag("episode-tapas/42/81").assertDoesNotExist()
        compose.onNodeWithTag("episodes-tapas/42").performClick()
        compose.onNodeWithTag("episode-tapas/42/81").assertExists()
        assertEquals(3, controller.episodeLoads.size)
        assertEquals(0, controller.goCalls)
    }

    @Test fun comicCheckboxAndCheckAllOnlyChangeFutureDownloads() {
        controller.state.value = controller.state.value.copy(
            sites = listOf(SiteChoice("tapas", "Tapas", true)),
            episodes = mapOf("tapas" to EpisodeList(listOf(
                EpisodeRow("42", "Chosen Comic", "1 / 2 saved", "CBZ", "Free", selected = true)), loaded = true)))
        show()
        episodes()
        compose.onNodeWithTag("episodes-tapas").performClick()
        compose.onNodeWithTag("select-episodes-tapas/42").assertIsOn().performClick()
        assertEquals(listOf(Triple(false, "tapas", "42")), controller.comicChanges)
        compose.onNodeWithTag("select-episodes-tapas/42").assertIsOff()
        compose.onNodeWithTag("comics-check-all").performClick()
        compose.onNodeWithTag("select-episodes-tapas/42").assertIsOn()
        compose.onNodeWithTag("comics-uncheck-all").performClick()
        compose.onNodeWithTag("select-episodes-tapas/42").assertIsOff()
        compose.onNodeWithText("1 / 2 saved").assertExists()
        assertEquals(0, controller.goCalls)
        controller.state.value = controller.state.value.copy(settingsBusy = true)
        compose.onNodeWithTag("comics-check-all").assertIsNotEnabled()
        compose.onNodeWithTag("select-episodes-tapas/42").assertIsNotEnabled()
    }

    @Test fun anEmptyOrUnreadableEpisodeListSaysSoAndRefreshStartsAgain() {
        controller.state.value = controller.state.value.copy(
            sites = listOf(SiteChoice("tapas", "Tapas", true), SiteChoice("webtoon", "Webtoon", true)),
            episodes = mapOf("tapas" to EpisodeList(loaded = true, empty = "Not indexed yet"),
                "webtoon" to EpisodeList(failed = true)))
        show()
        episodes()
        compose.onNode(hasTestTag("episodes-tapas") and hasText("Not indexed yet")).assertExists()
        compose.onNode(hasTestTag("episodes-webtoon") and hasText("Could not read · retry")).assertExists()
        // What was read is not read again by opening it; what failed is.
        compose.onNodeWithTag("episodes-tapas").performClick()
        assertEquals(emptyList<Pair<String, String?>>(), controller.episodeLoads)
        compose.onNodeWithTag("episodes-webtoon").performClick()
        assertEquals(listOf<Pair<String, String?>>("webtoon" to null), controller.episodeLoads)
        compose.onNodeWithText("Refresh").performClick()
        assertEquals(1, controller.episodeRefreshes)
        compose.onNode(hasTestTag("episodes-tapas") and hasText("Expand to view")).assertExists()
        compose.onNodeWithTag("episodes-tapas").performClick()
        assertEquals(listOf<Pair<String, String?>>("webtoon" to null, "tapas" to null), controller.episodeLoads)
    }

    @Test fun speedCanBeChangedWhileSavingAsOnTheDesktop() {
        controller.state.value = controller.state.value.copy(download = DownloadState.Planning)
        show()
        preferences()
        compose.onNodeWithText("A running download pauses to apply the change, then continues.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Slow").performScrollTo().assertIsEnabled().performClick()
        assertEquals("slow", controller.state.value.settings?.speed)
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertIsEnabled()
        assertEquals(0, controller.goCalls)
    }

    @Test fun episodeTypesAreChosenInTheDesktopsWordsAndSavedOnlyWithOneLeftChosen() {
        show()
        preferences()
        compose.onNodeWithText("Episode types on Tapas").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Free ones.").assertExists()
        compose.onNodeWithContentDescription("Paid / unlocked").performScrollTo().assertIsOn()
        compose.onNodeWithContentDescription("Free").assertIsOff()
        // Nothing to save until a choice differs, and never with every type cleared.
        compose.onNodeWithText("Save choices").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithContentDescription("Paid / unlocked").performScrollTo().performClick()
        compose.onNodeWithText("Save choices").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithContentDescription("Free").performScrollTo().performClick()
        compose.onNodeWithText("Save choices").performScrollTo().assertIsEnabled().performClick()
        assertEquals(listOf(mapOf("bought" to false, "free" to true)), controller.typeChanges)
        compose.onNodeWithContentDescription("Free").performScrollTo().assertIsOn()
        compose.onNodeWithText("Save choices").performScrollTo().assertIsNotEnabled()
        assertEquals(0, controller.goCalls)
    }

    @Test fun unreadableSettingsDoNotOfferAnUnverifiedMobileDataToggle() {
        controller.state.value = controller.state.value.copy(settings = null,
            settingsNotice = Notice("job_failed", "Download settings could not be read."))
        show()
        preferences()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertDoesNotExist()
        compose.onNodeWithText("Retry settings").performClick()
        assertEquals(2, controller.settingsRefreshes)
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
        preferences()
        compose.onNodeWithText("Dark").performClick().assertIsSelected()
        compose.runOnIdle { assertEquals(Color(0xFF111614), background) }
        compose.onNodeWithText("Light").performClick().assertIsSelected()
        compose.runOnIdle { assertEquals(Color(0xFFFAFAF6), background) }
        assertEquals(0, controller.goCalls)
    }

    @Test fun signedOutCanBrowseSavedLibraryWithoutOfferingGo() {
        controller.state.value = controller.state.value.copy(signIn = SignInState.SignedOut)
        show()
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Sign in")).assertIsDisplayed()
        compose.onNodeWithText("Start").assertDoesNotExist()
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
        compose.onNodeWithText("Filters & sort").performClick()
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
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasTestTag("saved-file-metadata"))
        compose.onNodeWithTag("file-details-metadata").performClick()
        compose.onNodeWithText("chapter.json").performScrollTo().assertIsDisplayed()
        compose.onNodeWithContentDescription("chapter.json", substring = true).assertDoesNotExist()
        assertEquals(listOf(comic.openId, novel.openId), controller.openRequests)
        assertEquals(0, controller.goCalls)
    }

    @Test
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    fun cloudCopyBrowsesBackblazeWithRetryAndNeverOffersLocalReaderActions() {
        val row = catalogFixture().page!!.rows.single().copy(openId = null, b2Copy = true,
            storage = "Backblaze B2 (previous connection 1)")
        val catalog = catalogFixture().let { it.copy(page = it.page!!.copy(rows = listOf(row))) }
        val opened = mutableListOf<String>()
        compose.setContent {
            CompositionLocalProvider(LocalUriHandler provides object : UriHandler {
                override fun openUri(uri: String) {
                    opened.add(uri)
                    if (opened.size == 1) throw IllegalStateException("private-browser-detail")
                }
            }) {
                MaterialTheme { SavedFilesScreen(catalog, { _, _ -> }, controller::openSavedFile) {} }
            }
        }
        val description = "Browse Chapter one, Synthetic series in Backblaze. chapter.cbz"
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
        compose.onNodeWithContentDescription(description).performClick()
        val failure = "Could not open Backblaze. Check that a browser is available and try again."
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText(failure))
        compose.onNodeWithText(failure).assertIsDisplayed()
        compose.onNodeWithText("Open").assertDoesNotExist()
        compose.onNodeWithText("Read here").assertDoesNotExist()
        compose.onNodeWithText("private-browser-detail").assertDoesNotExist()
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
        compose.onNodeWithContentDescription(description).performClick()
        compose.onNodeWithText(failure).assertDoesNotExist()
        assertEquals(listOf(B2Page.BROWSE.url, B2Page.BROWSE.url), opened)
        assertTrue(controller.openRequests.isEmpty())
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

    @Test
    @GraphicsMode(GraphicsMode.Mode.NATIVE)
    // Explicit width/height qualifiers loop for editable dialogs: Robolectric #8460.
    @Config(qualifiers = "mdpi")
    fun sameNamedWorkingLibrariesHaveDistinctActionsAndCanBeNamedWithoutSelection() {
        val selected = mutableListOf<String>()
        val renamed = mutableListOf<Pair<String, String>>()
        val current = mutableStateOf(WorkingLibrariesState(enabled = true, choices = listOf(
            WorkingLibraryChoice("default", "Original", true, true, false),
            WorkingLibraryChoice("frozen", "Library", false, true, true),
            WorkingLibraryChoice("illustrated", "Library", false, true, true),
        )))
        compose.setContent { MaterialTheme { WorkingLibrariesScreen(current.value,
            WorkingLibraryActions(select = { selected.add(it) }, rename = { id, name ->
                renamed.add(id to name)
                current.value = current.value.copy(renamedId = id, choices = current.value.choices.map {
                    if (it.id == id) it.copy(label = name) else it
                })
            })) } }
        // Scroll the lazy container so each item is located after composition;
        // repeatedly scrolling a retained child can chase its recycled bounds.
        for (description in listOf("Use working library: Library · working copy 2",
            "Use working library: Library · working copy 3")) {
            compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(description))
            compose.onNodeWithContentDescription(description).assertIsDisplayed()
        }
        val rename = "Rename working library: Library · working copy 3"
        compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(rename))
        compose.onNodeWithContentDescription(rename).performClick()
        compose.onNodeWithText("Library name").performTextReplacement("Cancelled")
        compose.onNodeWithText("Cancel").performClick()
        assertTrue(renamed.isEmpty())
        compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(rename))
        compose.onNodeWithContentDescription(rename).performClick()
        compose.onNodeWithText("Library name").performTextReplacement("original")
        compose.onNodeWithText("Save name").assertIsNotEnabled()
        compose.onNodeWithText("Library name").performTextReplacement("Illustrated reader")
        compose.onNodeWithText("Save name").performClick()
        compose.onNodeWithText("Rename library").assertDoesNotExist()
        val renamedChoice = "Use working library: Illustrated reader"
        compose.onNodeWithTag("working-libraries").performScrollToNode(hasContentDescription(renamedChoice))
        compose.onNodeWithContentDescription(renamedChoice).performClick()
        assertEquals(listOf("illustrated" to "Illustrated reader"), renamed)
        assertEquals(listOf("illustrated"), selected)
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
        compose.onNodeWithText("One chapter").assertDoesNotExist()
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
        compose.onNodeWithTag("open-library").performScrollTo().performClick()
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
        for (label in listOf("Save", "Library", "Settings"))
            compose.onNodeWithText(label, useUnmergedTree = true)
                .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
        assertTrue(layouts.size >= 3)
        assertTrue("Tab text layouts: " + layouts.joinToString { "lines=${it.lineCount}, " +
            "overflow=${it.didOverflowWidth}, size=${it.size}, paragraphWidth=${it.multiParagraph.width}, " +
            "intrinsic=${it.multiParagraph.maxIntrinsicWidth}, constraints=${it.layoutInput.constraints}, " +
            "lineBounds=${it.getLineLeft(0)}..${it.getLineRight(0)}, density=${it.layoutInput.density}" },
            layouts.all(::fitsSingleLine))
        about()
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
        export()
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
        overview()
        compose.onNodeWithText("Backblaze B2").performScrollTo().performClick()
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
        compose.onNodeWithText("All settings").assertIsDisplayed()
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
        preferences()
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
        compose.onNodeWithText("Download speed on Tapas").performScrollTo().assertIsDisplayed()
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
    fun onlyTheOpenSitesSpeedIsOfferedAndAnotherSiteIsChosenAtTheTop() {
        show()
        preferences()
        compose.onNodeWithText("Download speed on Tapas").performScrollTo().assertIsDisplayed()
        assertTrue(compose.onAllNodes(hasText("Other sites")).fetchSemanticsNodes().isEmpty())
        assertTrue(compose.onAllNodes(hasText("Webtoon", substring = true)).fetchSemanticsNodes().isEmpty())
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun signOutRemainsReachableOnASmallPhone() {
        show()
        about()
        compose.onNodeWithText("Sign out of Tapas").assertIsDisplayed().performClick()
        compose.onNodeWithText("Sign out of Tapas?").assertIsDisplayed()
        assertEquals(0, controller.signOutCalls)
        compose.onNodeWithText("Sign out").performClick()
        assertEquals(1, controller.signOutCalls)
        compose.onNodeWithTag("nav-saving").performClick()
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Sign in")).performScrollTo().assertIsDisplayed()
        compose.onNodeWithTag("card-signout-tapas").assertDoesNotExist()
    }

    @Test
    fun signOutCancellationKeepsTheSessionAndDoesNotStopSaving() {
        show()
        about()
        compose.onNodeWithText("Sign out of Tapas").performClick()
        compose.onNodeWithText("Sign out of Tapas?").assertIsDisplayed()
        compose.onNodeWithText("Cancel").performClick()
        compose.onNodeWithText("Sign out of Tapas?").assertDoesNotExist()
        assertEquals(0, controller.signOutCalls)
        assertEquals(0, controller.pauseCalls)
        compose.onNodeWithTag("nav-saving").performClick()
        compose.onNodeWithTag("card-action-tapas").assertIsDisplayed()
        assertEquals(0, controller.goCalls)
    }

    @Test
    fun signOutWaitsForADownloadToBePausedAsOnTheDesktop() {
        controller.state.value = controller.state.value.copy(
            download = DownloadState.Running(0, 0, "Downloading episodes…", null))
        show()
        about()
        compose.onNodeWithText("Pause the download to sign out.").assertIsDisplayed()
        compose.onNodeWithText("Sign out of Tapas").assertIsNotEnabled()
        overview()
        compose.onNodeWithText("Sign out of Tapas").performScrollTo().assertIsNotEnabled()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(download = DownloadState.Paused("user")) }
        compose.onNodeWithText("Pause the download to sign out.").assertDoesNotExist()
        compose.onNodeWithText("Sign out of Tapas").performScrollTo().assertIsEnabled().performClick()
        compose.onNodeWithText("Sign out of Tapas?").assertIsDisplayed()
        // A sign-out that did not finish stays retryable whatever the download says.
        compose.onNodeWithText("Cancel").performClick()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(signOutPending = true,
            download = DownloadState.Planning) }
        // Offered at the top of the page and beside the account, and both can be pressed.
        compose.onAllNodes(hasText("Retry sign-out") and hasClickAction()).fetchSemanticsNodes().let { offered ->
            assertEquals(2, offered.size)
            assertTrue(offered.none { it.config.contains(SemanticsProperties.Disabled) })
        }
        assertEquals(0, controller.signOutCalls)
    }

    @Test
    fun aConfirmationForThePreviousWorkingLibraryCannotSignOutTheNewOne() {
        show()
        about()
        compose.onNodeWithText("Sign out of Tapas").performClick()
        compose.onNodeWithText("Sign out of Tapas?").assertIsDisplayed()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(libraryId = "other-copy") }
        compose.onNodeWithText("Sign out of Tapas?").assertDoesNotExist()
        compose.runOnIdle { controller.state.value = controller.state.value.copy(libraryId = "default") }
        compose.onNodeWithText("Sign out of Tapas?").assertDoesNotExist()
        assertEquals(0, controller.signOutCalls)
        compose.onNodeWithText("Sign out of Tapas").performClick()
        compose.onNodeWithText("Sign out").performClick()
        assertEquals(1, controller.signOutCalls)
    }

    @Test
    @Config(qualifiers = "w360dp-h640dp")
    fun supportOpensTheSubredditAndSaysWhereItIsWhenNoBrowserOpens() {
        val opened = mutableListOf<String>()
        compose.setContent {
            CompositionLocalProvider(LocalUriHandler provides object : UriHandler {
                override fun openUri(uri: String) {
                    opened.add(uri)
                    if (opened.size == 1) throw IllegalStateException("private-browser-detail")
                }
            }) {
                MaterialTheme { SupportScreen() }
            }
        }
        compose.onNodeWithText("Questions and ideas are welcome on the Comic Archiver subreddit too", substring = true)
            .performScrollTo().assertIsDisplayed()
        compose.onNode(hasTestTag("support-subreddit") and hasText("Open r/ComicArchiver")).performScrollTo().performClick()
        compose.onNodeWithText("The browser could not be opened. The subreddit is at reddit.com/r/ComicArchiver.")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("private-browser-detail").assertDoesNotExist()
        compose.onNodeWithTag("support-subreddit").performScrollTo().performClick()
        compose.onNodeWithText("The browser could not be opened. The subreddit is at reddit.com/r/ComicArchiver.")
            .assertDoesNotExist()
        assertEquals(listOf("https://www.reddit.com/r/ComicArchiver/", "https://www.reddit.com/r/ComicArchiver/"), opened)
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
        about()
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
        compose.onNodeWithTag("nav-saving").performClick()
        compose.onNodeWithTag("card-action-tapas").performScrollTo().assertIsDisplayed()
        assertEquals(0, controller.goCalls)
        assertEquals(0, controller.signOutCalls)
    }

    @Test
    @Config(sdk = [29])
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
        about()
        compose.waitUntil(5_000) {
            compose.onAllNodes(hasText("APK fingerprint unavailable")).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("Version older-api-fixture (build 37)").assertIsDisplayed()
        compose.onNodeWithText("APK fingerprint unavailable").assertIsDisplayed()
        compose.onNodeWithText(absent.path).assertDoesNotExist()
        compose.onNodeWithTag("nav-saving").performClick()
        compose.onNodeWithTag("card-action-tapas").assertIsDisplayed()
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
            SpeedChoice("slow", "Slow", true)), "wifi", false, true, listOf(
            DownloadTypeChoice("bought", "Paid / unlocked", "Bought ones.", true),
            DownloadTypeChoice("free", "Free", "Free ones.", false))),
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
    val selected = mutableListOf<String>()
    val signIns = mutableListOf<String>()
    val goSites = mutableListOf<String>()
    val speedChanges = mutableListOf<Pair<String, String>>()
    override fun selectSite(key: String) { selected.add(key) }
    override fun signInSite(key: String) { signIns.add(key) }
    override fun goSite(key: String) { if (key == state.value.site) go() else goSites.add(key) }
    override fun pauseSite(key: String) { if (key == state.value.site) pause() }
    override fun speedSite(key: String, value: String) { speedChanges.add(key to value) }
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
    val episodeLoads = mutableListOf<Pair<String, String?>>()
    val comicChanges = mutableListOf<Triple<Boolean, String?, String?>>()
    override fun chooseComics(chosen: Boolean, site: String?, seriesId: String?) {
        comicChanges.add(Triple(chosen, site, seriesId))
        state.value = state.value.copy(episodes = state.value.episodes.mapValues { (key, list) ->
            if (key.contains('/') || site != null && site != key) list
            else list.copy(rows = list.rows.map { row ->
                if (seriesId == null || row.id == seriesId) row.copy(selected = chosen) else row
            })
        })
    }
    var episodeRefreshes = 0
    var supportEmails = 0
    override fun emailSupport() { supportEmails++ }
    override fun loadEpisodes(site: String, seriesId: String?) {
        episodeLoads.add(site to seriesId)
        val key = EpisodeList.key(site, seriesId)
        val before = state.value.episodes[key] ?: EpisodeList()
        state.value = state.value.copy(episodes = state.value.episodes + (key to before.copy(loading = true, failed = false)))
    }
    override fun refreshEpisodes() { episodeRefreshes++; state.value = state.value.copy(episodes = emptyMap()) }
    val typeChanges = mutableListOf<Map<String, Boolean>>()
    override fun setDownloadTypes(value: Map<String, Boolean>) {
        typeChanges.add(value)
        state.value = state.value.copy(settings = state.value.settings?.let { kept ->
            kept.copy(downloadTypes = kept.downloadTypes.map { it.copy(chosen = value[it.key] == true) })
        })
    }
    override fun requestExport(folderUri: String) {}
    override fun cancelExport() {}
    override fun openNotice(notice: Notice) {}
}
