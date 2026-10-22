package io.github.bcrute.comicarchiver.ui

import androidx.compose.ui.test.assertIsDisplayed
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
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
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

    @After fun terminateTestProcess() { compose.runOnIdle { app.onTerminate() } }

    private fun ready() {
        compose.waitUntil(10_000) { app.controller.state.value.signIn is SignInState.SignedIn &&
            app.controller.state.value.library.isNotEmpty() }
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
        compose.waitUntil(10_000) { controller.state.value.download is DownloadState.Running }
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Running").assertIsNotEnabled()
        assertSame(controller, app.controller)
        assertTrue(controller.state.value.signIn is SignInState.SignedIn)
        assertEquals(1, app.fake.count("prepare_workspace"))
        assertEquals(1, app.fake.count("go"))
        assertEquals(1, app.fake.count("signin_restore"))
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
}

/** A host seam, not a second controller or fake app UI. No Python/HTTP bootstrap. */
class LifecycleTestApp : App() {
    val fake = LifecycleBridge()
    override val bridge: PythonBridge get() = fake
    override val signInSites get() = WebViewSignInEngine.BETA_SITES
    override fun startRuntime() {}
}

class LifecycleBridge : PythonBridge {
    private val calls = ConcurrentHashMap<String, AtomicInteger>()
    fun count(op: String) = calls[op]?.get() ?: 0
    override fun dispatch(op: String, argsJson: String): String {
        calls.computeIfAbsent(op) { AtomicInteger() }.incrementAndGet()
        val result = when (op) {
            "signin_restore" -> """{"signed_in":true,"verified":true}"""
            "worker_start" -> """{"running":true,"stop_requested":false}"""
            "download_status" -> """{"worker":{"running":true},"account":{"status":"running","phase":"archive"}}"""
            "library_listing" -> """[{"seriesId":"test","title":"Test Comic","kind":"comics","episodeCount":4,"savedCount":4}]"""
            else -> "{}"
        }
        return """{"ok":true,"result":$result}"""
    }
}
