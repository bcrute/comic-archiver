package io.github.bcrute.comicarchiver.reader

import org.json.JSONObject

data class ReadingPosition(val unit: Int = 0, val offset: Int = 0) {
    fun json() = JSONObject().put("unit", unit).put("offset", offset)
}
data class ReadingPreferences(val fit: String = "width", val textScale: Double = 1.0) {
    fun json() = JSONObject().put("fit", fit).put("text_scale", textScale)
}
data class ReadingRecord(val content: String, val kind: String, val units: Int, val revision: Long,
    val position: ReadingPosition, val bookmarks: List<ReadingPosition>, val preferences: ReadingPreferences)
data class ReaderBook(val id: String, val title: String, val record: ReadingRecord,
    val canSave: Boolean, val warning: String?)
data class LocalReaderState(val id: String? = null, val book: ReaderBook? = null,
    val document: ReaderDocument? = null, val position: ReadingPosition = ReadingPosition(),
    val preferences: ReadingPreferences = ReadingPreferences(), val generation: Long = 0,
    val loading: Boolean = false, val saving: Boolean = false, val closing: Boolean = false,
    val error: String? = null, val feedback: String? = null) {
    val open get() = id != null
    val busy get() = loading || saving || closing
}

internal object ReaderProtocol {
    private val digest = Regex("[a-f0-9]{64}")
    fun integer(value: JSONObject, key: String, low: Int, high: Int): Int {
        val raw = value.opt(key)
        require(raw is Int || raw is Long)
        return (raw as Number).toLong().also { require(it in low.toLong()..high.toLong()) }.toInt()
    }
    fun result(raw: String): JSONObject {
        require(raw.length <= 16 * 1024 * 1024)
        val envelope = JSONObject(raw)
        require(integer(envelope, "v", 1, 1) == 1)
        if (envelope.opt("ok") != true) {
            val message = envelope.optString("message").takeIf {
                envelope.optString("code") == "refused" && it.length in 1..600
            } ?: "Reading is unavailable. Close the reader and choose the file again."
            throw ReaderRefusal(message)
        }
        return envelope.getJSONObject("result")
    }
    private fun keys(value: JSONObject) = value.keys().asSequence().toSet()
    private fun position(value: JSONObject, units: Int): ReadingPosition {
        require(keys(value) == setOf("unit", "offset"))
        return ReadingPosition(integer(value, "unit", 0, units - 1), integer(value, "offset", 0, 10_000))
    }
    fun record(value: JSONObject): ReadingRecord {
        require(keys(value) - setOf("v", "content_sha256", "kind", "units", "revision", "position", "bookmarks", "preferences") == emptySet<String>())
        integer(value, "v", 1, 1)
        val content = value.getString("content_sha256").also { require(it.matches(digest)) }
        val kind = value.getString("kind").also { require(it in setOf("cbz", "epub")) }
        val units = integer(value, "units", 1, 20_000)
        val rawRevision = if (value.has("revision")) value.opt("revision") else 0
        require(rawRevision is Int || rawRevision is Long)
        val revision = (rawRevision as Number).toLong().also { require(it in 0..9_007_199_254_740_990L) }
        val current = value.optJSONObject("position")?.let { position(it, units) }
            ?: ReadingPosition().also { require(!value.has("position")) }
        val bookmarks = if (!value.has("bookmarks")) emptyList() else value.getJSONArray("bookmarks").let { array ->
            require(array.length() <= 100)
            (0 until array.length()).map { position(array.getJSONObject(it), units) }
                .also { require(it.distinct().size == it.size) }
        }
        val prefs = if (!value.has("preferences")) JSONObject() else value.getJSONObject("preferences")
        require(keys(prefs) - setOf("fit", "text_scale") == emptySet<String>())
        val fit = (if (prefs.has("fit")) prefs.getString("fit") else "width")
            .also { require(it in setOf("width", "page")) }
        val scale = (if (prefs.has("text_scale")) prefs.opt("text_scale") else 1.0).let {
            require(it is Number)
            it.toDouble().also { number -> require(number.isFinite() && number in .75..2.0) }
        }
        return ReadingRecord(content, kind, units, revision, current, bookmarks, ReadingPreferences(fit, scale))
    }
    fun book(value: JSONObject, expectedId: String): ReaderBook {
        require(value.getString("reader_id") == expectedId)
        val record = record(value.getJSONObject("record"))
        require(value.getString("content_sha256") == record.content && value.getString("kind") == record.kind)
        require(integer(value, "units", 1, 20_000) == record.units && value.opt("can_save") is Boolean)
        val title = value.getString("title").also { require(it.length <= 4096) }
        val warning = if (value.isNull("warning")) null else value.opt("warning").let {
            require(it is String && it.length <= 600)
            it
        }
        return ReaderBook(expectedId, title, record, value.getBoolean("can_save"), warning)
    }
}

internal class ReaderRefusal(message: String) : Exception(message)
