package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject

/** Fail closed: an unreadable policy is not rendered as permission to use mobile data. */
object SettingsStatus {
    fun fromEnvelope(envelope: JSONObject): DownloadSettings? {
        if (!envelope.optBoolean("ok")) return null
        val value = envelope.optJSONObject("result") ?: return null
        val wifiOnly = value.opt("wifi_only") as? Boolean ?: return null
        val allowed = value.opt("allowed") as? Boolean ?: return null
        val connection = value.optString("connection")
        if (connection !in setOf("unknown", "wifi", "wired", "cellular")) return null
        val metered = when (val observed = value.opt("metered")) {
            null, JSONObject.NULL -> null
            is Boolean -> observed
            else -> return null
        }
        val choices = value.optJSONArray("speeds") ?: return null
        val speeds = (0 until choices.length()).map { index ->
            val row = choices.optJSONObject(index) ?: return null
            val key = row.optString("key")
            val label = row.optString("label")
            val available = row.opt("available") as? Boolean ?: return null
            if (key.isBlank() || label.isBlank()) return null
            SpeedChoice(key, label, available)
        }
        val speed = value.optString("speed")
        if (wifiOnly && allowed &&
            (connection !in setOf("wifi", "wired") || metered != false)) return null
        if (speeds.map { it.key }.distinct().size != speeds.size ||
            speeds.none { it.key == speed }) return null
        return DownloadSettings(wifiOnly, speed, speeds, connection, metered, allowed)
    }
}
