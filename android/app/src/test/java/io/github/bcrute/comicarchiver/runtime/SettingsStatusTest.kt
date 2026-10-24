package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SettingsStatus
import org.json.JSONObject
import org.json.JSONArray
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
        val settings = SettingsStatus.fromEnvelope(bad)!!
        assertEquals("fast", settings.speed)
        assertEquals(false, settings.speeds.any { it.key == settings.speed && it.available })
    }

    @Test fun otherSiteSpeedsPreserveAnUnavailableChoiceAndRejectAmbiguousTargets() {
        val value = envelope()
        val row = JSONObject().put("key", "webtoon").put("name", "Webtoon").put("speed", "fast")
            .put("enabled", true).put("speeds", value.getJSONObject("result").getJSONArray("speeds"))
        value.getJSONObject("result").put("other_sites", JSONArray().put(row))
        val settings = SettingsStatus.fromEnvelope(value)!!
        assertEquals("medium", settings.speed)
        assertEquals("fast", settings.otherSites.single().speed)
        assertEquals(false, settings.otherSites.single().speeds.first().available)
        value.getJSONObject("result").put("other_sites", JSONArray().put(row).put(row))
        assertNull(SettingsStatus.fromEnvelope(value))
        value.getJSONObject("result").put("other_sites", JSONArray().put(row.put("key", "../escape")))
        assertNull(SettingsStatus.fromEnvelope(value))
        row.put("key", "webtoon").put("speed", JSONObject.NULL).put("enabled", false)
        assertEquals(null, SettingsStatus.fromEnvelope(value)!!.otherSites.single().speed)
        row.put("enabled", true)
        assertNull(SettingsStatus.fromEnvelope(value))
    }
}
