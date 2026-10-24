package io.github.bcrute.comicarchiver.reader

import android.graphics.Bitmap
import android.graphics.drawable.BitmapDrawable
import android.text.style.ImageSpan
import android.text.style.URLSpan
import android.util.Base64
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.zip.ZipFile

/** The exact first producer set is shared with Python, not rebuilt from today's model. */
internal object ReaderFixtures {
    val root: File get() = generateSequence(File(System.getProperty("user.dir"))) { it.parentFile }
        .map { it.resolve("tests/fixtures/local-reading-v1") }.first { it.resolve("manifest.json").isFile }
    fun item(kind: String): JSONObject {
        val records = JSONObject(root.resolve("manifest.json").readText()).getJSONArray("records")
        return (0 until records.length()).map { records.getJSONObject(it) }.single { it.getString("kind") == kind }
    }
    fun record(kind: String) = JSONObject(root.resolve("payload").resolve(item(kind).getString("state")).readText())
    fun book(id: String, kind: String, value: JSONObject = record(kind)): JSONObject = JSONObject()
        .put("reader_id", id).put("title", "Saved chapter").put("kind", kind)
        .put("units", value.getInt("units")).put("content_sha256", value.getString("content_sha256"))
        .put("record", value).put("can_save", true).put("warning", JSONObject.NULL)
    fun page(id: String, kind: String, unit: Int): JSONObject {
        val result = JSONObject().put("reader_id", id).put("kind", kind).put("unit", unit)
        ZipFile(root.resolve("payload").resolve(item(kind).getString("archive"))).use { zip ->
            if (kind == "cbz") {
                val name = "%04d.png".format(java.util.Locale.ROOT, unit + 1)
                val bytes = zip.getInputStream(zip.getEntry(name)).use { it.readBytes() }
                result.put("data", Base64.encodeToString(bytes, Base64.NO_WRAP))
            } else result.put("html", zip.getInputStream(zip.getEntry("EPUB/chapter.xhtml")).use {
                it.readBytes().toString(Charsets.UTF_8)
            }).put("images", JSONObject())
        }
        return result
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [34])
class LocalReaderProtocolTest {
    private val id = "a".repeat(32)
    private fun refused(action: () -> Unit) {
        try { action(); fail("Invalid reader data was accepted") }
        catch (_: IllegalArgumentException) { }
    }

    @Test fun bothFirstProducerRecordsRetainEveryStoredMeaningInKotlin() {
        for (kind in listOf("cbz", "epub")) {
            val raw = ReaderFixtures.record(kind)
            val parsed = ReaderProtocol.record(raw)
            assertEquals(raw.getString("content_sha256"), parsed.content)
            assertEquals(raw.getString("kind"), parsed.kind)
            assertEquals(raw.getInt("units"), parsed.units)
            assertEquals(raw.getLong("revision"), parsed.revision)
            assertEquals(raw.getJSONObject("position").getInt("unit"), parsed.position.unit)
            assertEquals(raw.getJSONObject("position").getInt("offset"), parsed.position.offset)
            val bookmark = raw.getJSONArray("bookmarks").getJSONObject(0)
            assertEquals(listOf(ReadingPosition(bookmark.getInt("unit"), bookmark.getInt("offset"))), parsed.bookmarks)
            assertEquals(raw.getJSONObject("preferences").getString("fit"), parsed.preferences.fit)
            assertEquals(raw.getJSONObject("preferences").getDouble("text_scale"), parsed.preferences.textScale, 0.0)
            assertEquals(parsed, ReaderProtocol.book(ReaderFixtures.book(id, kind), id).record)
        }
    }
    @Test fun optionalV1FieldsTakeTheSameDefaultsAndUnknownOrFutureFieldsRefuse() {
        val raw = ReaderFixtures.record("cbz")
        listOf("revision", "position", "bookmarks", "preferences").forEach { raw.remove(it) }
        val parsed = ReaderProtocol.record(raw)
        assertEquals(0L, parsed.revision); assertEquals(ReadingPosition(), parsed.position)
        assertTrue(parsed.bookmarks.isEmpty()); assertEquals(ReadingPreferences(), parsed.preferences)
        refused { ReaderProtocol.record(JSONObject(raw.toString()).put("v", 2)) }
        refused { ReaderProtocol.record(JSONObject(raw.toString()).put("future", "retained")) }
    }
    @Test fun numbersBooleansRangesAndDuplicateBookmarksCannotChangeRecordMeaning() {
        for (raw in listOf(ReaderFixtures.record("cbz").put("units", true),
            ReaderFixtures.record("cbz").put("revision", "1"),
            ReaderFixtures.record("cbz").put("position", JSONObject().put("unit", 1.5).put("offset", 0)),
            ReaderFixtures.record("cbz").put("position", JSONObject().put("unit", 0).put("offset", 10001)),
            ReaderFixtures.record("cbz").put("preferences", JSONObject().put("text_scale", 9)))) {
            refused { ReaderProtocol.record(raw) }
        }
        val raw = ReaderFixtures.record("cbz")
        raw.getJSONArray("bookmarks").put(JSONObject(raw.getJSONArray("bookmarks").getJSONObject(0).toString()))
        refused { ReaderProtocol.record(raw) }
    }
    @Test fun ticketContentKindAndUnitMustMatchTheOpenedBook() {
        refused { ReaderProtocol.book(ReaderFixtures.book(id, "cbz"), "b".repeat(32)) }
        refused { ReaderProtocol.book(ReaderFixtures.book(id, "cbz").put("content_sha256", "b".repeat(64)), id) }
        refused { ReaderProtocol.book(ReaderFixtures.book(id, "cbz").put("kind", "epub"), id) }
        refused { ReaderProtocol.book(ReaderFixtures.book(id, "cbz").put("units", 3), id) }
    }
    @Test fun realFrozenComicPixelsAndNovelWordsDecodeWithTheDeclaredIdentity() {
        val comic = ReaderProtocol.book(ReaderFixtures.book(id, "cbz"), id)
        val page = LocalReaderMedia.decode(ReaderFixtures.page(id, "cbz", 1), comic, 1) as ReaderDocument.Comic
        assertEquals(480, page.bitmap.width); assertEquals(960, page.bitmap.height)
        assertEquals(android.graphics.Color.rgb(160, 40, 80), page.bitmap.getPixel(100, 100))
        val novel = ReaderProtocol.book(ReaderFixtures.book(id, "epub"), id)
        val text = LocalReaderMedia.decode(ReaderFixtures.page(id, "epub", 0), novel, 0) as ReaderDocument.Novel
        assertTrue(text.text.toString().contains("Readable words & punctuation"))
        assertEquals(0, text.text.getSpans(0, text.text.length, URLSpan::class.java).size)
    }
    @Test fun illustrationsUseOnlyVerifiedBytesAndLinksRemainInert() {
        val book = ReaderProtocol.book(ReaderFixtures.book(id, "epub"), id)
        val image = ReaderFixtures.page(id, "cbz", 0).getString("data")
        val value = JSONObject().put("reader_id", id).put("unit", 0).put("kind", "epub")
            .put("html", "<p>Illustrated <a href='file:/PRIVATE'>words</a></p><img src='images/one.png'><img src='https://invalid.test/PRIVATE'>")
            .put("images", JSONObject().put("reader:/EPUB/images/one.png", image))
        val text = (LocalReaderMedia.decode(value, book, 0) as ReaderDocument.Novel).text
        val spans = text.getSpans(0, text.length, ImageSpan::class.java)
        assertEquals(2, spans.size)
        assertEquals(1, spans.count { it.drawable is BitmapDrawable })
        assertEquals(0, text.getSpans(0, text.length, URLSpan::class.java).size)
    }
    @Test fun oversizedEncodedDataExtremeDimensionsAndWrongUnitRefuseBeforeDisplay() {
        val book = ReaderProtocol.book(ReaderFixtures.book(id, "cbz"), id)
        refused { LocalReaderMedia.decode(ReaderFixtures.page(id, "cbz", 0), book, 1) }
        refused { LocalReaderMedia.decode(JSONObject().put("reader_id", id).put("unit", 0).put("kind", "cbz")
            .put("data", "A".repeat(12 * 1024 * 1024)), book, 0) }
        val bitmap = Bitmap.createBitmap(1, 32768, Bitmap.Config.ARGB_8888)
        val stream = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, stream); bitmap.recycle()
        refused { LocalReaderMedia.decode(JSONObject().put("reader_id", id).put("unit", 0).put("kind", "cbz")
            .put("data", Base64.encodeToString(stream.toByteArray(), Base64.NO_WRAP)), book, 0) }
    }
}
