package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.CloudRequest
import io.github.bcrute.comicarchiver.controller.CloudStatus
import io.github.bcrute.comicarchiver.controller.json
import io.github.bcrute.comicarchiver.ui.cloudEnvelope
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CloudStatusTest {
    @Test fun unknownAccountRemainsUnconfiguredAndDisabled() {
        val state = CloudStatus.parse(cloudEnvelope())
        assertEquals("device", state.destination)
        assertFalse(state.canTest || state.canRetry || state.taskRunning)
        assertTrue(state.canConnect)
    }

    @Test fun malformedFlagsAndUnexpectedDestinationCannotEnableUploads() {
        for (key in listOf("connect_enabled", "test_enabled", "retry_enabled", "sharing_enabled")) {
            val bad = JSONObject(cloudEnvelope())
            bad.getJSONObject("result").getJSONObject("b2").put(key, "true")
            assertTrue(runCatching { CloudStatus.parse(bad.toString()) }.isFailure)
        }
        val bad = JSONObject(cloudEnvelope())
        bad.getJSONObject("result").put("destination", "external-remote")
        assertTrue(runCatching { CloudStatus.parse(bad.toString()) }.isFailure)
    }

    @Test fun credentialRequestHasFixedShapeAndRedactedDiagnosticText() {
        val request = CloudRequest.Connect("fixture-library", "fixtureKey123", "FIXTURE_KEY123")
        val json = request.json()
        assertEquals("connect", json.getString("action"))
        assertEquals(2, json.length())
        assertEquals("FIXTURE_KEY123", json.getJSONObject("credentials").getString("application_key"))
        assertFalse(request.toString().contains("FIXTURE_KEY123"))
        assertFalse(request.toString().contains("fixtureKey123"))
        assertEquals("resume", CloudRequest.Resume.json().getString("action"))
    }
}
