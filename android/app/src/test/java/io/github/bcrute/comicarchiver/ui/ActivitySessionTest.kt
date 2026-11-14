package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.runtime.SupportLog
import io.github.bcrute.comicarchiver.controller.SupportNote
import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.provider.Settings
import android.os.Looper
import androidx.compose.ui.test.ComposeTimeoutException
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.lifecycle.Lifecycle
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasAnyDescendant
import androidx.compose.ui.test.assert
import io.github.bcrute.comicarchiver.App
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows.shadowOf
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Actual activity/application/controller/engine wiring. The bridge is synthetic, so
 * this tests lifecycle/admission and UI restoration, not source HTTP or archive bytes. */
@RunWith(RobolectricTestRunner::class)
@Config(application = LifecycleTestApp::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class ActivitySessionTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val app get() = compose.activity.application as LifecycleTestApp

    private fun awaitState(condition: () -> Boolean) {
        try {
            compose.waitUntil(10_000) {
                // IO bridge replies use the real Android main Handler, separate from
                // Compose's frame clock. Robolectric's paused looper must be driven.
                shadowOf(Looper.getMainLooper()).idle()
                condition()
            }
        } catch (failure: ComposeTimeoutException) {
            throw AssertionError("Controller did not settle: download=${app.controller.state.value.download}, " +
                "restore=${app.fake.count("signin_restore")}, worker=${app.fake.count("worker_start")}, " +
                "go=${app.fake.count("go")}, status=${app.fake.count("download_status")}", failure)
        }
    }

    private fun ready() {
        awaitState { app.controller.state.value.signIn is SignInState.SignedIn &&
            app.controller.state.value.library.isNotEmpty() && app.controller.state.value.settings != null }
    }

    private fun notificationPermissionIsOff() {
        ready()
        compose.runOnIdle {
            shadowOf(app).denyPermissions(Manifest.permission.POST_NOTIFICATIONS)
            shadowOf(compose.activity.getSystemService(NotificationManager::class.java)).setNotificationsEnabled(true)
            compose.activity.getSharedPreferences("comicarchiver_notifications", Context.MODE_PRIVATE)
                .edit().clear().commit()
        }
        compose.activityRule.scenario.recreate()
        ready()
    }

    @Test fun notificationGrantAndRecreationDoNotStartAWorkerOrAdmitGo() {
        notificationPermissionIsOff()
        assertNull(shadowOf(compose.activity).lastRequestedPermission)
        compose.onNodeWithText("Allow download notifications").performScrollTo().performClick()
        val request = requireNotNull(shadowOf(compose.activity).lastRequestedPermission)
        assertArrayEquals(arrayOf(Manifest.permission.POST_NOTIFICATIONS), request.requestedPermissions)
        compose.runOnIdle {
            shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
            compose.activity.onRequestPermissionsResult(request.requestCode, request.requestedPermissions,
                intArrayOf(PackageManager.PERMISSION_GRANTED))
        }
        compose.onNodeWithText("Allow download notifications").assertDoesNotExist()
        compose.activityRule.scenario.recreate()
        ready()
        compose.onNodeWithText("Allow download notifications").assertDoesNotExist()
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.fake.count("worker_start"))
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun deniedNotificationPermissionOffersSettingsAndResumeOnlyRefreshesAccess() {
        notificationPermissionIsOff()
        compose.onNodeWithText("Allow download notifications").performScrollTo().performClick()
        val request = requireNotNull(shadowOf(compose.activity).lastRequestedPermission)
        compose.runOnIdle {
            compose.activity.onRequestPermissionsResult(request.requestCode, request.requestedPermissions,
                intArrayOf(PackageManager.PERMISSION_DENIED))
        }
        compose.onNodeWithText("Notification settings").performScrollTo().performClick()
        assertEquals(Settings.ACTION_APP_NOTIFICATION_SETTINGS, shadowOf(compose.activity).nextStartedActivity.action)
        compose.activityRule.scenario.moveToState(Lifecycle.State.STARTED)
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
        compose.onNodeWithText("Notification settings").assertDoesNotExist()
        compose.onNodeWithTag("card-action-tapas").performScrollTo().assertIsDisplayed()
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.fake.count("worker_start"))
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun coldProcessRestoresVerifiedSessionAndLibraryWithoutAdmittingDownloads() {
        ready()
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("worker_start"))
        assertEquals(1, app.fake.count("signin_restore"))
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Series").performClick()
        compose.onNodeWithText("1 of 1 series").assertIsDisplayed()
    }

    @Test fun aStoredUnavailableSpeedBlocksGoAndOffersSettingsRecoveryWithoutStartingAWorker() {
        ready()
        app.fake.speed = "fast"
        compose.runOnIdle { app.controller.refreshSettings() }
        awaitState { app.controller.state.value.settings?.speed == "fast" }
        compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
        awaitState { app.controller.state.value.notice?.message?.contains("stored speed") == true }
        assertEquals(0, app.fake.count("worker_start"))
        assertEquals(0, app.fake.count("go"))
        compose.onNodeWithTag("nav-settings").performClick()
        compose.onNodeWithText("Download preferences").performScrollTo().performClick()
        compose.onNodeWithText("Fast").performScrollTo().assertIsSelected().assertIsNotEnabled()
        compose.onNodeWithText("Medium").performScrollTo().performClick()
        awaitState { app.controller.state.value.settings?.speed == "medium" && !app.controller.state.value.settingsBusy }
        compose.onNode(hasText("Downloads") and hasClickAction()).performClick()
        compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
        awaitState { app.controller.state.value.download is DownloadState.Running }
        assertEquals(1, app.fake.count("go"))
    }

    @Test fun repeatedGoDuringAdmissionAndRunningStartsOneJobAndCompletionAllowsAnother() {
        ready()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        app.fake.workerStartGate = entered to release
        try {
            compose.runOnIdle { repeat(3) { app.controller.go() } }
            awaitState { entered.count == 0L }
            assertEquals(DownloadState.Planning, app.controller.state.value.download)
            release.countDown()
            awaitState { app.controller.state.value.download is DownloadState.Running }
            assertEquals(1, app.fake.count("go"))
            assertEquals(1, app.fake.count("worker_start"))
            compose.runOnIdle { repeat(3) { app.controller.go() } }
            awaitState { app.controller.state.value.download is DownloadState.Running }
            assertEquals(1, app.fake.count("go"))
            assertEquals(1, app.fake.count("worker_start"))
            app.fake.statusPayload = """{"worker":{"running":true,"state":"ready"},"account":{"status":"finished"}}"""
            awaitState { app.controller.state.value.download is DownloadState.Finished }
            app.fake.statusPayload = """{"worker":{"running":true,"state":"busy"},"account":{"status":"running","phase":"archive"}}"""
            compose.runOnIdle { repeat(3) { app.controller.go() } }
            awaitState { app.controller.state.value.download is DownloadState.Running }
            assertEquals(2, app.fake.count("go"))
            assertEquals(2, app.fake.count("worker_start"))
        } finally { release.countDown(); compose.runOnIdle { app.controller.pause() } }
    }

    @Test fun activityRecreationKeepsLiveAdmissionAndDoesNotRestartOrReauthenticate() {
        ready()
        val controller = app.controller
        compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
        awaitState { controller.state.value.download is DownloadState.Running }
        compose.activityRule.scenario.recreate()
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Pause")).performScrollTo().assertIsDisplayed()
        assertSame(controller, app.controller)
        assertTrue(controller.state.value.signIn is SignInState.SignedIn)
        assertEquals(1, app.fake.count("prepare_workspace"))
        assertEquals(1, app.fake.count("go"))
        assertEquals(1, app.fake.count("signin_restore"))
    }

    @Test fun settingsUseTheControllerBridgeAndSurviveRecreationWithoutGo() {
        ready()
        compose.onNodeWithTag("nav-settings").performClick()
        compose.onNodeWithText("Download preferences").performScrollTo().performClick()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").performClick()
        awaitState { app.controller.state.value.settings?.wifiOnly == false &&
            !app.controller.state.value.settingsBusy }
        compose.onNodeWithText("Slow").performScrollTo().performClick()
        awaitState { app.controller.state.value.settings?.speed == "slow" &&
            !app.controller.state.value.settingsBusy }
        compose.activityRule.scenario.recreate()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertIsOn()
        compose.onNodeWithText("Slow").performScrollTo().assertIsSelected()
        assertEquals(0, app.fake.count("go"))
        assertEquals(2, app.fake.count("configure_settings"))
    }

    @Test fun aSettingChangedWhileSavingLeavesTheDownloadRunningAsOnTheDesktop() {
        ready()
        val running = """{"worker":{"running":true,"state":"busy"},"account":{"status":"running","phase":"archive"}}"""
        val stopped = """{"worker":{"running":true,"state":"ready"},"account":{"status":"stopped"}}"""
        app.fake.statusPayload = running
        val controller = app.controller
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        try {
            compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
            awaitState { controller.state.value.download is DownloadState.Running }
            // The change pauses the download while it is applied; the phone reads that pause.
            app.fake.settingsGate = entered to release
            compose.runOnIdle { controller.setDownloadTypes(mapOf("bought" to true, "free" to false)) }
            awaitState { entered.count == 0L }
            app.fake.statusPayload = stopped
            val read = app.fake.count("download_status")
            awaitState { app.fake.count("download_status") >= read + 2 }
            assertTrue(controller.state.value.download is DownloadState.Running)
            app.fake.statusPayload = running
            release.countDown()
            awaitState { !controller.state.value.settingsBusy }
            assertEquals(listOf(true, false), controller.state.value.settings?.downloadTypes?.map { it.chosen })
            val after = app.fake.count("download_status")
            awaitState { app.fake.count("download_status") >= after + 2 }
            assertTrue(controller.state.value.download is DownloadState.Running)
            assertEquals(1, app.fake.count("go"))
            // A pause that is not a change's still ends it.
            app.fake.statusPayload = stopped
            awaitState { controller.state.value.download == DownloadState.Paused("user") }
        } finally { release.countDown(); compose.runOnIdle { controller.pause() } }
    }

    @Test fun aSiteThatIsSavingIsNotSignedOutUntilItIsPaused() {
        ready()
        app.fake.statusPayload = """{"worker":{"running":true,"state":"busy"},"account":{"status":"running","phase":"archive"}}"""
        val controller = app.controller
        try {
            compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
            awaitState { controller.state.value.download is DownloadState.Running }
            // A late tap or a restored dialog: the controller itself keeps the sign-in.
            compose.runOnIdle { controller.signOut() }
            awaitState { controller.state.value.notice?.message == "Pause the download to sign out." }
            assertTrue(controller.state.value.signIn is SignInState.SignedIn)
            assertTrue(controller.state.value.download is DownloadState.Running)
            assertEquals(false, controller.state.value.signOutPending)
            assertEquals(0, app.fake.count("signout_record"))
        } finally { compose.runOnIdle { controller.pause() } }
    }

    @Test fun supportDetailsAreCopiedAndTheLogIsHandedToDownloadsWithNothingLeftBehind() {
        ready()
        val controller = app.controller
        val copied = mutableListOf<String>()
        val handed = mutableListOf<String>()
        controller.copyText = { copied.add(it) }
        controller.saveToDownloads = { file -> handed.add(file.readText()); file.name }
        compose.runOnIdle { controller.copySupportDetails() }
        awaitState { controller.state.value.supportNote != null && !controller.state.value.supportBusy }
        assertEquals(listOf("Comic Archiver details"), copied)
        assertEquals(SupportNote("Copied. Paste it into your message or email."), controller.state.value.supportNote)
        val asked = JSONObject(app.fake.lastArgs.getValue("support_details"))
        assertEquals(setOf("base_root", "version", "system"), asked.keys().asSequence().toSet())
        assertTrue(Regex("Android [0-9A-Za-z ._-]*\\(API \\d+\\)").matches(asked.getString("system")))

        compose.runOnIdle { controller.saveSupportLog() }
        awaitState { controller.state.value.supportNote?.text?.startsWith("Saved") == true }
        assertEquals(listOf("the whole log"), handed)
        assertEquals(SupportNote("Saved in your Downloads folder as \u201cComic Archiver log fixture.txt\u201d."),
            controller.state.value.supportNote)
        val outbox = SupportLog.outbox(app)
        assertEquals(outbox.absolutePath, JSONObject(app.fake.lastArgs.getValue("support_save")).getString("out_root"))
        assertEquals(SupportLog.folder(app).absolutePath,
            JSONObject(app.fake.lastArgs.getValue("support_save")).getString("log_root"))
        assertTrue(outbox.listFiles().orEmpty().isEmpty())

        // Downloads refuses the file, the core cannot write it, or the clipboard is unavailable.
        controller.saveToDownloads = { null }
        compose.runOnIdle { controller.saveSupportLog() }
        awaitState { controller.state.value.supportNote?.failed == true }
        assertEquals("The log could not be saved. Check that your Downloads folder can be written to.",
            controller.state.value.supportNote?.text)
        assertTrue(outbox.listFiles().orEmpty().isEmpty())
        app.fake.supportReadable = false
        controller.saveToDownloads = { file -> handed.add("never"); file.name }
        compose.runOnIdle { controller.saveSupportLog() }
        awaitState { !controller.state.value.supportBusy && controller.state.value.supportNote?.failed == true }
        compose.runOnIdle { controller.copySupportDetails() }
        awaitState { controller.state.value.supportNote?.text == "The details could not be copied. Try again." }
        app.fake.supportReadable = true
        controller.copyText = { throw IllegalStateException("PRIVATE clipboard failure") }
        compose.runOnIdle { controller.copySupportDetails() }
        awaitState { !controller.state.value.supportBusy && controller.state.value.supportNote?.failed == true }
        assertEquals(listOf("Comic Archiver details"), copied)
        assertEquals(listOf("the whole log"), handed)
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun failedPolicyRefreshHidesStaleMobileConsentInsteadOfReusingIt() {
        ready()
        compose.onNodeWithTag("nav-settings").performClick()
        compose.onNodeWithText("Download preferences").performScrollTo().performClick()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").performClick()
        awaitState { app.controller.state.value.settings?.wifiOnly == false &&
            !app.controller.state.value.settingsBusy }
        compose.runOnIdle { app.fake.settingsReadable = false; app.controller.refreshSettings() }
        awaitState { app.controller.state.value.settings == null &&
            app.controller.state.value.settingsNotice != null }
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertDoesNotExist()
        compose.onNodeWithText("Download settings are unavailable.").assertIsDisplayed()
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun activityRecreationRetainsExportCoordinatorAndLibraryQuery() {
        ready()
        val exports = app.exports
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Series").performClick()
        compose.onNodeWithText("Find saved series").performTextInput("Test")
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Clear search").assertIsDisplayed()
        compose.onNodeWithText("1 of 1 series").assertIsDisplayed()
        assertSame(exports, app.exports)
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun filesUseTheControllerBridgeAndSurviveRecreationWithoutGo() {
        ready()
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Series").performClick()
        compose.onNodeWithText("Files").performClick()
        awaitState { app.controller.state.value.catalog.page != null &&
            !app.controller.state.value.catalog.loading }
        assertEquals(1, app.controller.state.value.catalog.page!!.total)
        compose.onNodeWithText("Find saved files").performTextInput("One")
        compose.onNodeWithText("Search").performClick()
        awaitState { !app.controller.state.value.catalog.loading }
        compose.activityRule.scenario.recreate()
        awaitState { !app.controller.state.value.catalog.loading }
        compose.onNodeWithText("Find saved files").assertIsDisplayed()
        assertEquals("One", app.controller.state.value.catalog.query.query)
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("worker_start"))
    }

    @Test fun appearancePersistsAndSurvivesActivityRecreationWithoutGo() {
        ready()
        compose.onNodeWithTag("nav-settings").performClick()
        compose.onNodeWithText("Download preferences").performScrollTo().performClick()
        compose.onNodeWithText("Dark").performClick().assertIsSelected()
        assertEquals("DARK", app.getSharedPreferences("comicarchiver_appearance", 0).getString("theme", null))
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Dark").assertIsSelected()
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("configure_settings"))
    }

    @Test fun openingFailureIsShownBesideItsFileRatherThanAboveTheWholeCatalog() {
        ready()
        compose.onNodeWithTag("nav-library").performClick()
        compose.onNodeWithText("Series").performClick()
        compose.onNodeWithText("Files").performClick()
        awaitState { app.controller.state.value.catalog.page != null }
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasText("Open"))
        compose.onNodeWithText("Open").performClick()
        val message = "This file could not be opened. Refresh files and try again."
        awaitState { app.controller.state.value.catalog.feedback == message &&
            app.controller.state.value.catalog.openingId == null }
        compose.onNodeWithText(message).performScrollTo().assertIsDisplayed()
        compose.onNodeWithTag("saved-file-${"a".repeat(64)}")
            .assert(hasAnyDescendant(hasText(message)) and hasAnyDescendant(hasText("Open")))
        assertEquals(1, app.fake.count("file_open"))
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun failedCatalogRefreshShowsFailureRatherThanAnEmptyOrStaleResult() {
        ready()
        compose.runOnIdle { app.controller.loadCatalog(io.github.bcrute.comicarchiver.controller.CatalogQuery()) }
        awaitState { app.controller.state.value.catalog.page != null }
        compose.runOnIdle {
            app.fake.catalogReadable = false
            app.controller.loadCatalog(app.controller.state.value.catalog.query, true)
        }
        awaitState { app.controller.state.value.catalog.error != null }
        assertEquals(1, app.controller.state.value.catalog.page!!.total)
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun heldStatusObservationsAndRecreationNeverAdmitGoOrStartAWorker() {
        ready()
        awaitState { app.controller.state.value.observations != null }
        val controller = app.controller
        assertEquals(DownloadState.Held, controller.state.value.download)
        compose.activityRule.scenario.recreate()
        ready()
        assertSame(controller, app.controller)
        assertEquals(DownloadState.Held, controller.state.value.download)
        compose.onNode(hasTestTag("card-action-tapas") and hasText("Start")).performScrollTo().assertIsDisplayed()
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("worker_start"))
    }

    @Test fun losingStatusAndLibraryReadsKeepsLivePauseAndRecoveryDoesNotReadmitGo() {
        ready()
        compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
        awaitState { app.controller.state.value.download is DownloadState.Running }
        app.fake.statusReadable = false
        app.fake.libraryReadable = false
        awaitState { app.controller.state.value.statusReadFailed }
        assertTrue(app.controller.state.value.download is DownloadState.Running)
        // Recover before Pause starts its new held poll; do not race the ten-second
        // held refresh interval against the test's observation timeout.
        app.fake.statusReadable = true
        app.fake.libraryReadable = true
        compose.onNodeWithText("Pause").performScrollTo().performClick()
        awaitState { app.controller.state.value.download is DownloadState.Paused }
        awaitState { !app.controller.state.value.statusReadFailed }
        assertTrue(app.controller.state.value.download is DownloadState.Paused)
        assertEquals(1, app.fake.count("go"))
        compose.onNodeWithTag("card-action-tapas").performScrollTo().assertIsDisplayed()
    }

    @Test fun aLateRunningObservationCannotReplaceTheNewerPausedAdmission() {
        ready()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        app.fake.statusPayload = """{"worker":{"running":true,"state":"busy"},
            "account":{"status":"running","phase":"archive"},"counts":{"saved_episodes":999}}"""
        app.fake.statusGate = entered to release
        try {
            compose.onNodeWithTag("card-action-tapas").performScrollTo().performClick()
            awaitState { entered.count == 0L }
            app.fake.statusPayload = """{"worker":{"running":true,"state":"ready"},
                "account":{"status":"stopped"},"counts":{"saved_episodes":7}}"""
            compose.onNodeWithText("Pause").performScrollTo().performClick()
            awaitState { app.controller.state.value.download is DownloadState.Paused &&
                app.controller.state.value.observations?.savedEpisodes == 7L }
            release.countDown()
            awaitState { app.fake.releasedStatus.get() == 1 }
            shadowOf(Looper.getMainLooper()).idle()
            assertTrue(app.controller.state.value.download is DownloadState.Paused)
            assertEquals(7L, app.controller.state.value.observations!!.savedEpisodes)
            assertEquals(1, app.fake.count("go"))
        } finally { release.countDown() }
    }

    @Test fun b2SetupIsReachableThroughTheActualActivityWithoutGoOrAnotherWorker() {
        ready()
        awaitState { app.controller.state.value.cloud != null }
        compose.onNodeWithTag("nav-settings").performClick()
        compose.onNodeWithText("Backblaze B2").performScrollTo().performClick()
        compose.onNodeWithText("Need a bucket or key? Setup guide").performScrollTo().performClick()
        compose.onNodeWithText("Open Buckets").performScrollTo().assertIsDisplayed()
        assertTrue(app.fake.count("cloud_status") >= 1)
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("worker_start"))
    }
}

/** A host seam, not a second controller or fake app UI. No Python/HTTP bootstrap. */
class LifecycleTestApp : App() {
    val fake = LifecycleBridge()
    override val bridge: PythonBridge get() = fake
    override val signInSites get() = WebViewSignInEngine.BETA_SITES
    override fun startRuntime() {}
    override fun selectRuntimeCookieStore(store: io.github.bcrute.comicarchiver.signin.WebViewCookieStore, siteKey: String) {}
    override fun startNetworkWriter(target: io.github.bcrute.comicarchiver.runtime.SiteWorkspace) {}
    override fun deactivateRuntimeCookieStore() {}
}

class LifecycleBridge : PythonBridge {
    @Volatile var settingsReadable = true
    @Volatile var catalogReadable = true
    @Volatile var statusReadable = true
    @Volatile var libraryReadable = true
    @Volatile var statusPayload = """{"worker":{"running":true},"account":{"status":"running","phase":"archive"}}"""
    @Volatile var statusGate: Pair<CountDownLatch, CountDownLatch>? = null
    @Volatile var workerStartGate: Pair<CountDownLatch, CountDownLatch>? = null
    val releasedStatus = AtomicInteger()
    @Volatile private var wifiOnly = true
    @Volatile var speed = "medium"
    @Volatile var types = mapOf("bought" to true, "free" to true)
    @Volatile var settingsGate: Pair<CountDownLatch, CountDownLatch>? = null
    @Volatile var supportReadable = true
    val lastArgs = ConcurrentHashMap<String, String>()
    private val calls = ConcurrentHashMap<String, AtomicInteger>()
    fun count(op: String) = calls[op]?.get() ?: 0
    override fun dispatch(op: String, argsJson: String): String {
        calls.computeIfAbsent(op) { AtomicInteger() }.incrementAndGet()
        lastArgs[op] = argsJson
        if (op in setOf("support_details", "support_save") && !supportReadable)
            return """{"ok":false,"code":"refused"}"""
        if (op == "support_save") {
            // The core writes the file where it was told to and answers its name only.
            val out = java.io.File(JSONObject(argsJson).getString("out_root")).apply { mkdirs() }
            java.io.File(out, "Comic Archiver log fixture.txt").writeText("the whole log")
        }
        if (op == "worker_start") {
            val startGate = synchronized(this) { workerStartGate.also { workerStartGate = null } }
            startGate?.let {
                it.first.countDown()
                check(it.second.await(20, TimeUnit.SECONDS)) { "Fixture worker start was not released" }
            }
        }
        if (op == "download_status" && !statusReadable) return """{"ok":false,"code":"job_failed"}"""
        if (op == "library_listing" && !libraryReadable) throw IllegalStateException("PRIVATE library observation failure")
        val statusSnapshot = if (op == "download_status") statusPayload else null
        val gate = if (op == "download_status") synchronized(this) { statusGate.also { statusGate = null } } else null
        gate?.let {
            it.first.countDown()
            check(it.second.await(20, TimeUnit.SECONDS)) { "Fixture response was not released" }
            releasedStatus.incrementAndGet()
        }
        if (op in setOf("settings", "configure_settings") && !settingsReadable)
            return """{"ok":false,"code":"job_failed"}"""
        if (op == "file_catalog" && !catalogReadable) return """{"ok":false,"code":"job_failed"}"""
        if (op == "configure_settings") {
            val args = JSONObject(argsJson)
            if (args.has("wifi_only")) wifiOnly = args.getBoolean("wifi_only")
            if (args.has("speed")) speed = args.getString("speed")
            args.optJSONObject("download_types")?.let { chosen ->
                types = chosen.keys().asSequence().associateWith { chosen.getBoolean(it) }
            }
            val held = synchronized(this) { settingsGate.also { settingsGate = null } }
            held?.let {
                it.first.countDown()
                check(it.second.await(20, TimeUnit.SECONDS)) { "Fixture setting change was not released" }
            }
        }
        val result = when (op) {
            "cloud_status", "cloud_action" -> return cloudEnvelope()
            "file_catalog" -> """{"rows":[{"row_id":"${"a".repeat(64)}","open":"${"a".repeat(64)}",
                "series":"Test Comic","title":"One","filename":"one.cbz","type":"Comic","size":"4 B",
                "storage":"This device","saved_epoch":null,"file_time":false}],"total":1,"offset":0,
                "limit":50,"summary":"One saved episode","range":"Showing 1 of 1","notice":"Loaded",
                "locations":[{"id":"all","label":"All storage"}],"kinds":[{"key":"episodes","label":"Comics and novels"}],
                "sorts":[{"key":"saved_desc","label":"Newest saved first"}],"page_sizes":[50,100,200]}"""
            "settings", "configure_settings" -> """{"wifi_only":$wifiOnly,"speed":"$speed",
                "download_types":[{"key":"bought","label":"Paid / unlocked","help":"Bought ones.","chosen":${types["bought"]}},
                {"key":"free","label":"Free","help":"Free ones.","chosen":${types["free"]}}],
                "connection":"wifi","metered":false,"allowed":true,"speeds":[
                {"key":"fast","label":"Fast","available":false},
                {"key":"medium","label":"Medium","available":true},
                {"key":"slow","label":"Slow","available":true}]}"""
            "signin_restore", "signin_commit" -> """{"signed_in":true,"verified":true}"""
            "worker_start" -> """{"running":true,"stop_requested":false}"""
            "support_details" -> """{"text":"Comic Archiver details"}"""
            "support_save" -> """{"name":"Comic Archiver log fixture.txt"}"""
            "download_status" -> statusSnapshot!!
            "library_listing" -> """[{"seriesId":"test","title":"Test Comic","kind":"comics","episodeCount":4,"savedCount":4}]"""
            else -> "{}"
        }
        return """{"ok":true,"result":$result}"""
    }
}
