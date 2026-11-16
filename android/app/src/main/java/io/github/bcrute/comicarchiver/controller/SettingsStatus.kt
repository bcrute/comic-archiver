package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject
import org.json.JSONArray

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
        val speeds = speedChoices(value.optJSONArray("speeds") ?: return null) ?: return null
        val speed = value.optString("speed")
        if (wifiOnly && allowed &&
            (connection !in setOf("wifi", "wired") || metered != false)) return null
        if (speeds.map { it.key }.distinct().size != speeds.size ||
            speeds.none { it.key == speed }) return null
        val types = typeChoices(value.optJSONArray("download_types") ?: return null) ?: return null
        // A site that shows no extras sends an empty list; a list that cannot be read is no settings at all.
        val extras = value.optJSONArray("download_extras")?.let { rows ->
            if (rows.length() == 0) emptyList() else typeChoices(rows) ?: return null
        } ?: emptyList()
        if (extras.any { extra -> types.any { it.key == extra.key } }) return null
        return DownloadSettings(wifiOnly, speed, speeds, connection, metered, allowed, types, extras)
    }

    /** Null when a row cannot be read: a choice that is not known is never shown as made. */
    private fun typeChoices(rows: JSONArray): List<DownloadTypeChoice>? {
        return (0 until rows.length()).map { index ->
            val row = rows.optJSONObject(index) ?: return null
            val key = row.optString("key")
            val label = row.optString("label")
            val chosen = row.opt("chosen") as? Boolean ?: return null
            if (key.isBlank() || label.isBlank()) return null
            DownloadTypeChoice(key, label, row.optString("help"), chosen)
        }.takeIf { choices -> choices.isNotEmpty() && choices.map { it.key }.distinct().size == choices.size }
    }

    private fun speedChoices(rows: JSONArray): List<SpeedChoice>? {
        return (0 until rows.length()).map { index ->
            val row = rows.optJSONObject(index) ?: return null
            val key = row.optString("key")
            val label = row.optString("label")
            val available = row.opt("available") as? Boolean ?: return null
            if (key.isBlank() || label.isBlank()) return null
            SpeedChoice(key, label, available)
        }.takeIf { choices -> choices.isNotEmpty() && choices.map { it.key }.distinct().size == choices.size }
    }
}
