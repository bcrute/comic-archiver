package io.github.bcrute.comicarchiver.reader

import android.content.res.Resources
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.ColorDrawable
import android.text.Html
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.URLSpan
import android.text.style.ForegroundColorSpan
import android.text.style.BackgroundColorSpan
import android.util.Base64
import org.json.JSONObject

sealed class ReaderDocument {
    data class Comic(val bitmap: Bitmap) : ReaderDocument()
    data class Novel(val text: Spanned) : ReaderDocument()
}

/** Only verified bytes enter these decoders. No URI, network client or WebView exists here. */
internal object LocalReaderMedia {
    private const val MAX_BYTES = 8 * 1024 * 1024
    private const val MAX_SOURCE_PIXELS = 32_000_000L
    private val encoding = Regex("[A-Za-z0-9+/]*={0,2}")
    private fun bytes(value: String): ByteArray {
        require(value.length <= (MAX_BYTES + 2) / 3 * 4 && value.length % 4 == 0 && encoding.matches(value))
        return Base64.decode(value, Base64.NO_WRAP).also { require(it.isNotEmpty() && it.size <= MAX_BYTES) }
    }
    private fun image(raw: ByteArray, limit: Long): Bitmap {
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(raw, 0, raw.size, options)
        require(options.outWidth in 1..32768 && options.outHeight in 1..32768)
        require(options.outWidth.toLong() <= options.outHeight.toLong() * 256 &&
            options.outHeight.toLong() <= options.outWidth.toLong() * 256)
        val pixels = options.outWidth.toLong() * options.outHeight
        require(pixels <= MAX_SOURCE_PIXELS && limit > 0)
        var sample = 1
        while (((options.outWidth + sample - 1).toLong() / sample) *
            ((options.outHeight + sample - 1).toLong() / sample) > limit) sample *= 2
        options.inJustDecodeBounds = false
        options.inSampleSize = sample
        options.inPreferredConfig = Bitmap.Config.ARGB_8888
        return (BitmapFactory.decodeByteArray(raw, 0, raw.size, options)
            ?: throw ReaderRefusal("This image could not be displayed. Close the reader and use Open."))
            .also { require(it.width.toLong() * it.height <= limit) }
    }
    fun decode(value: JSONObject, book: ReaderBook, unit: Int): ReaderDocument {
        require(value.getString("reader_id") == book.id && value.getString("kind") == book.record.kind)
        require(ReaderProtocol.integer(value, "unit", 0, book.record.units - 1) == unit)
        val budget = (Runtime.getRuntime().maxMemory() / 32).coerceIn(1_000_000L, 8_000_000L)
        val allocated = mutableListOf<Bitmap>()
        try {
            if (book.record.kind == "cbz") return ReaderDocument.Comic(image(bytes(value.getString("data")), budget))
            val html = value.getString("html").also { require(it.toByteArray(Charsets.UTF_8).size <= 1024 * 1024) }
            val images = value.getJSONObject("images")
            require(images.length() <= 128)
            var remaining = budget
            var imagesLeft = images.length()
            var total = html.toByteArray(Charsets.UTF_8).size.toLong()
            val decoded = images.keys().asSequence().associateWith { name ->
                require(name.startsWith("reader:/EPUB/images/"))
                val raw = bytes(images.getString(name))
                total += raw.size
                require(total <= MAX_BYTES)
                image(raw, remaining / imagesLeft--).also { bitmap ->
                    allocated.add(bitmap)
                    remaining -= bitmap.width.toLong() * bitmap.height
                }
            }
            val styled = Html.fromHtml(html, Html.FROM_HTML_MODE_COMPACT, { source ->
                val key = if (source.startsWith("images/")) "reader:/EPUB/$source" else source
                decoded[key]?.let { bitmap ->
                    BitmapDrawable(Resources.getSystem(), bitmap).apply { setBounds(0, 0, bitmap.width, bitmap.height) }
                } ?: ColorDrawable(android.graphics.Color.TRANSPARENT).apply { setBounds(0, 0, 1, 1) }
            }, null)
            val inert = SpannableStringBuilder(styled)
            inert.getSpans(0, inert.length, URLSpan::class.java).forEach(inert::removeSpan)
            inert.getSpans(0, inert.length, ForegroundColorSpan::class.java).forEach(inert::removeSpan)
            inert.getSpans(0, inert.length, BackgroundColorSpan::class.java).forEach(inert::removeSpan)
            return ReaderDocument.Novel(inert)
        } catch (_: OutOfMemoryError) {
            allocated.forEach { it.recycle() }
            throw ReaderRefusal("This page exceeds the phone reader’s memory limits. Close the reader and use Open.")
        } catch (error: Exception) {
            allocated.forEach { it.recycle() }
            throw error
        }
    }
}
