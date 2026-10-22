package io.github.bcrute.comicarchiver.ui

import android.os.Looper
import androidx.compose.ui.test.ComposeTimeoutException
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
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
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows.shadowOf
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.ConcurrentHashMap
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

    @Test fun coldProcessRestoresVerifiedSessionAndLibraryWithoutAdmittingDownloads() {
        ready()
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.fake.count("go"))
        assertEquals(0, app.fake.count("worker_start"))
        assertEquals(1, app.fake.count("signin_restore"))
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
        compose.onNodeWithText("1 of 1 series").assertIsDisplayed()
    }

    @Test fun activityRecreationKeepsLiveAdmissionAndDoesNotRestartOrReauthenticate() {
        ready()
        val controller = app.controller
        compose.onNodeWithText("Go").performClick()
        awaitState { controller.state.value.download is DownloadState.Running }
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Running").assertIsNotEnabled()
        assertSame(controller, app.controller)
        assertTrue(controller.state.value.signIn is SignInState.SignedIn)
        assertEquals(1, app.fake.count("prepare_workspace"))
        assertEquals(1, app.fake.count("go"))
        assertEquals(1, app.fake.count("signin_restore"))
    }

    @Test fun settingsUseTheControllerBridgeAndSurviveRecreationWithoutGo() {
        ready()
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").performClick()
        awaitState { app.controller.state.value.settings?.wifiOnly == false &&
            !app.controller.state.value.settingsBusy }
        compose.onNodeWithText("Slow").performClick()
        awaitState { app.controller.state.value.settings?.speed == "slow" &&
            !app.controller.state.value.settingsBusy }
        compose.activityRule.scenario.recreate()
        compose.onNodeWithContentDescription("Also save on metered and mobile connections").assertIsOn()
        compose.onNodeWithText("Slow").assertIsSelected()
        assertEquals(0, app.fake.count("go"))
        assertEquals(2, app.fake.count("configure_settings"))
    }

    @Test fun failedPolicyRefreshHidesStaleMobileConsentInsteadOfReusingIt() {
        ready()
        compose.onNode(hasText("Settings") and hasClickAction()).performClick()
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
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
        compose.onNodeWithText("Find saved series").performTextInput("Test")
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Clear search").assertIsDisplayed()
        compose.onNodeWithText("1 of 1 series").assertIsDisplayed()
        assertSame(exports, app.exports)
        assertEquals(0, app.fake.count("go"))
    }

    @Test fun filesUseTheControllerBridgeAndSurviveRecreationWithoutGo() {
        ready()
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
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
}

/** A host seam, not a second controller or fake app UI. No Python/HTTP bootstrap. */
class LifecycleTestApp : App() {
    val fake = LifecycleBridge()
    override val bridge: PythonBridge get() = fake
    override val signInSites get() = WebViewSignInEngine.BETA_SITES
    override fun startRuntime() {}
}

class LifecycleBridge : PythonBridge {
    @Volatile var settingsReadable = true
    @Volatile var catalogReadable = true
    @Volatile private var wifiOnly = true
    @Volatile private var speed = "medium"
    private val calls = ConcurrentHashMap<String, AtomicInteger>()
    fun count(op: String) = calls[op]?.get() ?: 0
    override fun dispatch(op: String, argsJson: String): String {
        calls.computeIfAbsent(op) { AtomicInteger() }.incrementAndGet()
        if (op in setOf("settings", "configure_settings") && !settingsReadable)
            return """{"ok":false,"code":"job_failed"}"""
        if (op == "file_catalog" && !catalogReadable) return """{"ok":false,"code":"job_failed"}"""
        if (op == "configure_settings") {
            val args = JSONObject(argsJson)
            if (args.has("wifi_only")) wifiOnly = args.getBoolean("wifi_only")
            if (args.has("speed")) speed = args.getString("speed")
        }
        val result = when (op) {
            "file_catalog" -> """{"rows":[{"row_id":"${"a".repeat(64)}","open":"${"a".repeat(64)}",
                "series":"Test Comic","title":"One","filename":"one.cbz","type":"Comic","size":"4 B",
                "storage":"This device","saved_epoch":null,"file_time":false}],"total":1,"offset":0,
                "limit":50,"summary":"One saved episode","range":"Showing 1 of 1","notice":"Loaded",
                "locations":[{"id":"all","label":"All storage"}],"kinds":[{"key":"episodes","label":"Comics and novels"}],
                "sorts":[{"key":"saved_desc","label":"Newest saved first"}],"page_sizes":[50,100,200]}"""
            "settings", "configure_settings" -> """{"wifi_only":$wifiOnly,"speed":"$speed",
                "connection":"wifi","metered":false,"allowed":true,"speeds":[
                {"key":"fast","label":"Fast","available":false},
                {"key":"medium","label":"Medium","available":true},
                {"key":"slow","label":"Slow","available":true}]}"""
            "signin_restore" -> """{"signed_in":true,"verified":true}"""
            "worker_start" -> """{"running":true,"stop_requested":false}"""
            "download_status" -> """{"worker":{"running":true},"account":{"status":"running","phase":"archive"}}"""
            "library_listing" -> """[{"seriesId":"test","title":"Test Comic","kind":"comics","episodeCount":4,"savedCount":4}]"""
            else -> "{}"
        }
        return """{"ok":true,"result":$result}"""
    }
}
