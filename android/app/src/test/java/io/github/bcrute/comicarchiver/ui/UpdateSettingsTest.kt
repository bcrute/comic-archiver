package io.github.bcrute.comicarchiver.ui

import android.app.Application
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

internal class FakeUpdateBackend : UpdateBackend {
    @Volatile var enabled = false
    @Volatile var requests = 0
    @Volatile var checks = 0
    @Volatile var fail = false
    override fun automatic(path: String): Boolean = enabled
    override fun choose(path: String, enabled: Boolean) { this.enabled = enabled }
    override fun check(current: String, path: String, force: Boolean): JSONObject {
        checks++
        if (fail) throw IllegalStateException("private diagnostic")
        if (force || enabled) requests++
        return JSONObject().put("status", if (force || enabled) "current" else "disabled")
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class UpdateSettingsTest {
    @get:Rule val compose = createComposeRule()


    private lateinit var settings: UpdateSettings
    private fun show(backend: FakeUpdateBackend) {
        compose.setContent {
            val context = LocalContext.current
            val scope = rememberCoroutineScope()
            settings = remember { UpdateSettings(context, scope, backend) }
            MaterialTheme { UpdateControls(settings, manual = true) }
        }
    }
    private fun settled(checks: Int, backend: FakeUpdateBackend) {
        compose.waitUntil(5_000) { backend.checks >= checks && !settings.busy }
        compose.waitForIdle()
    }

    @Test fun manualChecksLeaveAutomaticChecksOff() {
        val backend = FakeUpdateBackend(); show(backend)
        compose.runOnIdle { settings.load() }; settled(1, backend)
        assertFalse(settings.automatic); assertEquals(0, backend.requests)
        compose.onNodeWithText("Check for updates").performClick(); settled(2, backend)
        assertFalse(settings.automatic); assertFalse(backend.enabled)
        assertEquals(1, backend.requests)
        compose.onNodeWithText("You have the latest version.").assertExists()
        compose.runOnIdle { settings.load() }; settled(3, backend)
        assertEquals(1, backend.requests)
    }

    @Test fun optingInAndOutUsesTheSavedChoiceOnReload() {
        val backend = FakeUpdateBackend(); show(backend)
        compose.runOnIdle { settings.chooseAutomatic(true) }; settled(1, backend)
        assertTrue(settings.automatic); assertTrue(backend.enabled)
        compose.runOnIdle { settings.load() }; settled(2, backend)
        assertEquals(2, backend.requests)
        compose.runOnIdle { settings.chooseAutomatic(false) }; settled(3, backend)
        compose.runOnIdle { settings.load() }; settled(4, backend)
        assertFalse(settings.automatic); assertFalse(backend.enabled)
        assertEquals(2, backend.requests)
    }

    @Test fun aFailedCheckKeepsConsentOffAndCanBeRetriedWithoutRawDiagnostics() {
        val backend = FakeUpdateBackend(); backend.fail = true; show(backend)
        compose.onNodeWithText("Check for updates").performClick(); settled(1, backend)
        assertFalse(settings.automatic); assertFalse(backend.enabled)
        compose.onNodeWithText("Could not check or save update settings. Try again when connected.").assertExists()
        compose.onNodeWithText("private diagnostic").assertDoesNotExist()
        backend.fail = false
        compose.onNodeWithText("Check for updates").performClick(); settled(2, backend)
        compose.onNodeWithText("You have the latest version.").assertExists()
        assertFalse(settings.automatic)
    }
}
