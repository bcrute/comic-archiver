package io.github.bcrute.comicarchiver.ui

import android.app.Application
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import io.github.bcrute.comicarchiver.controller.CloudCleanup
import io.github.bcrute.comicarchiver.controller.CloudDestination
import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.CloudStatus
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

internal fun cloudFixture(connected: Boolean = false) = CloudStatus(
    title = if (connected) "Connected, not checked yet" else "Not set up",
    explanation = "Your own Backblaze bucket.", status = "Uploads disabled.", auth = "", repair = "",
    bucket = if (connected) "fixture-library" else "", suggestion = "fixture-library",
    connectLabel = if (connected) "Replace B2 connection" else "Connect B2",
    canConnect = true, showCancel = false, canCancel = false, canTest = connected,
    canRetry = false, showSharing = true, canShare = true, shared = true,
    sharingHelp = "Each site keeps a folder of its own.", cleanups = emptyList(),
    destinations = if (connected) listOf(CloudDestination("device", "This device", true, true),
        CloudDestination("b2", "Backblaze B2", false, false)) else emptyList(),
    destination = "device", destinationHelp = "New episodes stay on this device.", destinationLock = "",
    deviceStored = "2 episodes stored", b2Stored = "", message = "", taskRunning = false,
    authStatus = if (connected) "connected" else "idle", cost = "", taskHeld = false, cleanupRemaining = 0,
)

/** Synthetic public bridge reply shared with the actual-activity wiring checks. */
internal fun cloudEnvelope(): String {
    val value = JSONObject().put("bucket_suggestion", "fixture-library").put("destination", "device")
        .put("destinations", JSONArray()).put("destination_help", "New episodes stay on this device.")
        .put("destination_lock", "").put("device_stored", "2 episodes stored").put("b2_stored", "")
        .put("message", "").put("task_running", false).put("task_held", false).put("auth_status", "idle").put("cost", "")
        .put("cleanup_remaining", 0)
    val b2 = JSONObject().put("pill", "Not set up").put("text", "Your own Backblaze bucket.")
        .put("status", "Uploads disabled.").put("auth", "").put("repair", "").put("bucket", JSONObject.NULL)
        .put("connect_text", "Connect B2").put("cleanup", JSONArray()).put("sharing_help", "Shared setup")
    for (key in listOf("connect_enabled", "sharing_visible", "sharing_enabled", "shared")) b2.put(key, true)
    for (key in listOf("cancel_visible", "cancel_enabled", "test_enabled", "retry_enabled")) b2.put(key, false)
    return JSONObject().put("ok", true).put("result", value.put("b2", b2)).toString()
}

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class CloudScreensTest {
    @get:Rule val compose = createComposeRule()

    @Test fun setupCleansPastedKeysAndClearsThemWhenConnectionStarts() {
        val requests = mutableListOf<CloudRequest>()
        compose.setContent { MaterialTheme {
            CloudScreen(cloudFixture(), false, null, true, true, {}, requests::add, {})
        } }
        compose.onNodeWithText("keyID").performScrollTo().performTextInput("fixture Key123 ")
        compose.onNodeWithText("applicationKey").performScrollTo().performTextInput("FIXTURE KEY123 ")
        compose.onNodeWithText("Connect B2").performScrollTo().assertIsEnabled().performClick()
        val request = requests.single() as CloudRequest.Connect
        assertEquals("fixtureKey123", request.keyId)
        assertEquals("FIXTUREKEY123", request.applicationKey)
        compose.onNodeWithText("Connect B2").assertIsNotEnabled()
        assertFalse(request.toString().contains(request.applicationKey))
    }

    @Test fun leavingSetupDiscardsDraftSecretsEvenWhenReturningToTheSameScreen() {
        var setup by mutableStateOf(true)
        compose.setContent { MaterialTheme {
            CloudScreen(cloudFixture(), false, null, setup, true, {}, {}, {})
        } }
        compose.onNodeWithText("keyID").performScrollTo().performTextInput("fixtureKey123")
        compose.onNodeWithText("applicationKey").performScrollTo().performTextInput("FIXTURE_KEY123")
        compose.onNodeWithText("Connect B2").performScrollTo().assertIsEnabled()
        compose.runOnIdle { setup = false }
        compose.waitForIdle()
        compose.runOnIdle { setup = true }
        compose.onNodeWithText("Connect B2").performScrollTo().assertIsNotEnabled()
    }

    @Test fun signedOutCloudTasksStayDisabledAndConnectionCancellationIsReachable() {
        val requests = mutableListOf<CloudRequest>()
        compose.setContent { MaterialTheme {
            CloudScreen(cloudFixture(true).copy(showCancel = true, canCancel = true, canConnect = false,
                canRetry = true, authStatus = "running"), false, null, true, false, {}, requests::add, {})
        } }
        compose.onNodeWithText("Cancel B2 sign-in").performScrollTo().performClick()
        assertEquals(listOf(CloudRequest.Cancel), requests)
        compose.onNodeWithText("Test cloud connection").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Retry unfinished uploads").performScrollTo().assertIsNotEnabled()
    }

    @Test fun storageOnlyOffersVerifiedDestinationAndDoesNotStartAccountGo() {
        val requests = mutableListOf<CloudRequest>()
        compose.setContent { MaterialTheme {
            CloudScreen(cloudFixture(true), false, null, false, true, {}, requests::add, {})
        } }
        compose.onNodeWithText("Backblaze B2").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("This device").performScrollTo().performClick()
        assertEquals(listOf(CloudRequest.Destination("device")), requests)
    }

    @Test fun heldCloudTaskHasExplicitContinueAndCleanupRetainsItsExactIdentity() {
        val requests = mutableListOf<CloudRequest>()
        val id = "12345678-1234-1234-1234-123456789abc"
        var state by mutableStateOf(cloudFixture(true).copy(taskRunning = true, taskHeld = true,
            cleanups = listOf(CloudCleanup(id, "Retry recorded test cleanup", false))))
        compose.setContent { MaterialTheme {
            CloudScreen(state, false, "A previous cloud request failed.", true, true, {}, {
                requests.add(it)
                if (it == CloudRequest.Resume) state = state.copy(taskRunning = false, taskHeld = false,
                    cleanups = listOf(CloudCleanup(id, "Retry recorded test cleanup", true)))
            }, {})
        } }
        compose.onNodeWithText("Cloud task held. Nothing restarts until you continue it.").assertIsDisplayed()
        compose.onNodeWithText("Continue cloud task").performClick()
        compose.onNodeWithText("Retry recorded test cleanup").performScrollTo().performClick()
        assertEquals(listOf(CloudRequest.Resume, CloudRequest.Cleanup(id)), requests)
    }

    @Test fun guidedBrowserLinksUseTheFixedOfficialDestination() {
        val pages = mutableListOf<B2Page>()
        compose.setContent { MaterialTheme {
            CloudScreen(cloudFixture(), false, null, true, true, {}, {}, pages::add)
        } }
        compose.onNodeWithText("Open Buckets").performScrollTo().performClick()
        assertEquals(listOf(B2Page.BUCKETS), pages)
        assertTrue(pages.single().url.startsWith("https://secure.backblaze.com/"))
    }
}
