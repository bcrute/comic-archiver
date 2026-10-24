package io.github.bcrute.comicarchiver.reader

import android.content.Context
import android.graphics.drawable.Drawable
import android.text.SpannableStringBuilder
import android.text.style.ImageSpan
import android.util.TypedValue
import android.view.View
import android.view.ViewGroup
import android.view.ViewOutlineProvider
import android.widget.ImageView
import android.widget.ScrollView
import android.widget.TextView
import kotlin.math.min
import kotlin.math.roundToInt

/** Native scrolling content, with restoration separated from observed user scroll. */
internal class ReaderViewport(context: Context) : ScrollView(context) {
    private class ComicPageView(context: Context) : ImageView(context) {
        var readingHeight = 1
        override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
            super.onMeasure(widthMeasureSpec, heightMeasureSpec)
            setMeasuredDimension(measuredWidth, readingHeight)
        }
    }
    private var document: ReaderDocument? = null
    private var prefs = ReadingPreferences()
    private var generation = -1L
    private var offset = 0
    private var restoring = true
    private var restorePending = false
    private var callback: ((Int) -> Unit)? = null
    private var content: View? = null
    private var needsSizing = true
    private var sizedWidth = 0
    private var sizedHeight = 0
    init {
        isFillViewport = false
        // AndroidView does not clip native drawing to its Compose layout bounds.
        // A restored scroll must never paint the chapter over the reader controls.
        outlineProvider = ViewOutlineProvider.BOUNDS
        clipToOutline = true
        setOnScrollChangeListener { _, _, y, _, _ ->
            if (!restoring && height > 0) {
                val extent = ((content?.height ?: 0) - height).coerceAtLeast(0)
                if (extent > 0) {
                    offset = ((y.toLong() * 10_000) / extent).toInt().coerceIn(0, 10_000)
                    callback?.invoke(offset)
                }
            }
        }
    }
    fun show(value: ReaderDocument, position: ReadingPosition, preferences: ReadingPreferences,
             version: Long, textColor: Int, onPosition: (Int) -> Unit) {
        callback = onPosition
        offset = position.offset
        val changed = document !== value || prefs != preferences || generation != version
        if (changed) {
            restoring = true
            needsSizing = true
            generation = version; prefs = preferences
            if (document !== value) {
                (content as? ImageView)?.setImageDrawable(null)
                removeAllViews()
                content = when (value) {
                    is ReaderDocument.Comic -> ComicPageView(context).apply {
                        setImageBitmap(value.bitmap)
                        scaleType = ImageView.ScaleType.FIT_CENTER
                        contentDescription = "Comic page ${position.unit + 1}"
                    }
                    is ReaderDocument.Novel -> TextView(context).apply {
                        setTextIsSelectable(true)
                        autoLinkMask = 0
                        linksClickable = false
                        movementMethod = null
                        val padding = (16 * resources.displayMetrics.density).roundToInt()
                        setPadding(padding, padding, padding, padding)
                        setLineSpacing(0f, 1.25f)
                    }
                }
                addView(content, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
                document = value
            }
            restore()
        }
        (content as? TextView)?.setTextColor(textColor)
    }
    private fun fitImage(drawable: Drawable, available: Int) {
        val originalWidth = drawable.intrinsicWidth.coerceAtLeast(1)
        val originalHeight = drawable.intrinsicHeight.coerceAtLeast(1)
        val shownWidth = min(available, originalWidth).coerceAtLeast(1)
        val shownHeight = (shownWidth.toLong() * originalHeight / originalWidth).toInt().coerceAtLeast(1)
        drawable.setBounds(0, 0, shownWidth, shownHeight)
    }
    private fun sizeContent(availableWidth: Int, availableHeight: Int) {
        if (availableWidth <= 0 || availableHeight <= 0) return
        if (!needsSizing && sizedWidth == availableWidth && sizedHeight == availableHeight) return
        needsSizing = false; sizedWidth = availableWidth; sizedHeight = availableHeight
        when (val value = document) {
            is ReaderDocument.Comic -> {
                val image = content as ComicPageView
                val widthHeight = availableWidth.toLong() * value.bitmap.height / value.bitmap.width
                val shown = if (prefs.fit == "page") min(availableHeight.toLong(), widthHeight) else widthHeight
                image.readingHeight = shown.coerceIn(1, 16_000_000).toInt()
            }
            is ReaderDocument.Novel -> {
                val text = content as TextView
                text.setTextSize(TypedValue.COMPLEX_UNIT_SP, (18 * prefs.textScale).toFloat())
                val styled = SpannableStringBuilder(value.text)
                styled.getSpans(0, styled.length, ImageSpan::class.java).forEach { span ->
                    span.drawable?.let { fitImage(it, availableWidth - text.paddingLeft - text.paddingRight) }
                }
                text.text = styled
            }
            null -> Unit
        }
    }
    private fun restore() {
        restoring = true
        restorePending = true
        requestLayout()
    }
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        sizeContent(MeasureSpec.getSize(widthMeasureSpec), MeasureSpec.getSize(heightMeasureSpec))
        super.onMeasure(widthMeasureSpec, heightMeasureSpec)
    }
    override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
        super.onLayout(changed, left, top, right, bottom)
        if (restorePending) {
            val extent = ((content?.height ?: 0) - height).coerceAtLeast(0)
            scrollTo(0, (extent.toLong() * offset / 10_000).toInt())
            restorePending = false
            restoring = false
        }
    }
    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        restoring = true
        super.onSizeChanged(w, h, oldw, oldh)
        restore()
    }
    override fun onDetachedFromWindow() {
        restoring = true; callback = null
        super.onDetachedFromWindow()
    }
}
