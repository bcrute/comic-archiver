package io.github.bcrute.comicarchiver.export

import org.json.JSONArray
import org.json.JSONObject

/**
 * Parses the bridge's `export_manifest` response envelope into [ExportEntry] values.
 *
 * SCHEMA ASSUMPTION (published to the native line in ui-next-handoff.md, coordinated
 * before divergence): request args `{"root": "<workspace root>"}`; response envelope
 * `{"v":1,"ok":true,"result":…}` where the result carries the entry list either as a
 * bare JSON array or as `{"entries":[…]}`; each entry has exactly the frozen contract's
 * fields `seriesId, episodeId, kind, filename, bytes, sha256, relpath` — `kind` is
 * "CBZ" or "EPUB" (case-insensitive), `sha256` is 64 hex characters, and `relpath` is
 * relative to the workspace's Library folder. An unimplemented or refused manifest
 * arrives as ok=false and is surfaced as such — never as an empty success.
 *
 * Unparseable single entries are counted, not silently dropped: an entry the manifest
 * could not describe is never reported as exported.
 */
object ExportManifestParser {

    data class Parsed(
        val entries: List<ExportEntry>,
        /** Entries whose shape the parser refused; shown as failures, never as exports. */
        val invalid: Int,
        /** Envelope-level refusal code (public words set), or null when ok=true. */
        val refusedCode: String? = null,
        val refusedMessage: String? = null,
    ) {
        val refused: Boolean get() = refusedCode != null
    }

    fun parse(envelopeJson: String): Parsed {
        val envelope = JSONObject(envelopeJson)
        if (!envelope.optBoolean("ok", false)) {
            return Parsed(
                entries = emptyList(),
                invalid = 0,
                refusedCode = envelope.optString("code", "job_failed"),
                refusedMessage = envelope.optString("message", "The export list could not be read."),
            )
        }
        val result = envelope.opt("result")
        val array = when (result) {
            is JSONArray -> result
            is JSONObject -> result.optJSONArray("entries") ?: JSONArray()
            else -> JSONArray()
        }
        val entries = ArrayList<ExportEntry>(array.length())
        var invalid = 0
        for (i in 0 until array.length()) {
            val item = array.optJSONObject(i) ?: run { invalid++; continue }
            try {
                entries.add(
                    ExportEntry(
                        seriesId = item.getString("seriesId"),
                        episodeId = item.getString("episodeId"),
                        kind = parseKind(item.getString("kind")),
                        filename = item.getString("filename"),
                        bytes = item.getLong("bytes"),
                        sha256 = item.getString("sha256"),
                        relpath = item.getString("relpath"),
                    ),
                )
            } catch (_: IllegalArgumentException) {
                invalid++
            } catch (_: org.json.JSONException) {
                invalid++
            }
        }
        return Parsed(entries, invalid)
    }

    private fun parseKind(raw: String): ExportKind =
        when (raw.trim().uppercase()) {
            "CBZ" -> ExportKind.CBZ
            "EPUB" -> ExportKind.EPUB
            else -> throw IllegalArgumentException("unknown kind: $raw")
        }
}
