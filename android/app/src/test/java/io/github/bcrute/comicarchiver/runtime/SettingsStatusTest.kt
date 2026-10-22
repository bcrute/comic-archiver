package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SettingsStatus
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SettingsStatusTest {
    private fun envelope() = JSONObject("""{"ok":true,"result":{
        "wifi_only":true,"speed":"medium","connection":"unknown","metered":null,"allowed":false,
        "speeds":[{"key":"fast","label":"Fast","available":false},
        {"key":"medium","label":"Medium","available":true},
        {"key":"slow","label":"Slow","available":true}]}}""")

    @Test fun unknownNetworkKeepsItsHeldMeaningAndUnavailableModeIsPreserved() {
        val settings = SettingsStatus.fromEnvelope(envelope())!!
        assertEquals(true, settings.wifiOnly)
        assertEquals(false, settings.allowed)
        assertNull(settings.metered)
        assertEquals(false, settings.speeds.first().available)
    }

    @Test fun incompleteOrMistypedPolicyCannotBecomeMobileConsent() {
        for (key in listOf("wifi_only", "allowed")) {
            val bad = envelope()
            bad.getJSONObject("result").remove(key)
            assertNull(SettingsStatus.fromEnvelope(bad))
            bad.getJSONObject("result").put(key, "false")
            assertNull(SettingsStatus.fromEnvelope(bad))
        }
    }

    @Test fun unverifiedConnectionCannotBeReportedAsAllowedUnderDefaultPolicy() {
        val bad = envelope()
        bad.getJSONObject("result").put("allowed", true)
        assertNull(SettingsStatus.fromEnvelope(bad))
    }

    @Test fun unsupportedSelectedSpeedCannotBePresentedAsApplied() {
        val bad = envelope()
        bad.getJSONObject("result").put("speed", "fast")
        assertNull(SettingsStatus.fromEnvelope(bad))
    }
}
