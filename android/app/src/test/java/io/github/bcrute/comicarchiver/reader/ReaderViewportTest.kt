package io.github.bcrute.comicarchiver.reader

import android.app.Activity
import android.graphics.Bitmap
import android.os.Looper
import android.text.SpannableString
import android.view.View
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import kotlin.math.abs

@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ReaderViewportTest {
    private fun layout(view: View, width: Int = 400, height: Int = 400) {
        repeat(2) {
            view.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
                View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
            view.layout(0, 0, width, height)
        }
        shadowOf(Looper.getMainLooper()).idle()
    }
    @Test fun comicFitWidthAndRestoredOffsetUseActualGeometryWithoutSavingProgrammaticScroll() {
        val owner = Robolectric.buildActivity(Activity::class.java).setup()
        val view = ReaderViewport(owner.get())
        val positions = mutableListOf<Int>()
        val document = ReaderDocument.Comic(Bitmap.createBitmap(80, 800, Bitmap.Config.ARGB_8888))
        view.show(document, ReadingPosition(1, 6500), ReadingPreferences(), 1, 0xff000000.toInt()) { positions.add(it) }
        layout(view)
        assertEquals(4000, view.getChildAt(0).height)
        assertTrue(abs(view.scrollY - 2340) <= 2)
        assertTrue(positions.isEmpty())
        view.scrollTo(0, 900)
        assertEquals(2500, positions.last())
        owner.pause().stop().destroy()
    }
    @Test fun fitPageAndViewportResizeDoNotTurnRestorationIntoUserProgress() {
        val owner = Robolectric.buildActivity(Activity::class.java).setup()
        val view = ReaderViewport(owner.get())
        val positions = mutableListOf<Int>()
        val document = ReaderDocument.Comic(Bitmap.createBitmap(80, 800, Bitmap.Config.ARGB_8888))
        view.show(document, ReadingPosition(0, 5000), ReadingPreferences("page"), 1, 0xff000000.toInt()) { positions.add(it) }
        layout(view)
        assertEquals(400, view.getChildAt(0).height)
        assertEquals(0, view.scrollY)
        view.show(document, ReadingPosition(0, 5000), ReadingPreferences(), 2, 0xff000000.toInt()) { positions.add(it) }
        layout(view, 300, 400)
        assertEquals(3000, view.getChildAt(0).height)
        assertTrue(abs(view.scrollY - 1300) <= 2)
        assertTrue(positions.isEmpty())
        owner.pause().stop().destroy()
    }
    @Test fun novelReflowPreservesProportionalPositionWithoutWritingAnArtificialScroll() {
        val owner = Robolectric.buildActivity(Activity::class.java).setup()
        val view = ReaderViewport(owner.get())
        val positions = mutableListOf<Int>()
        val document = ReaderDocument.Novel(SpannableString("A readable line of text.\n".repeat(300)))
        view.show(document, ReadingPosition(0, 5000), ReadingPreferences(), 1, 0xff000000.toInt()) { positions.add(it) }
        layout(view)
        val first = view.getChildAt(0).height
        assertTrue(first > 400)
        assertTrue(abs(view.scrollY.toLong() * 10_000 / (first - 400) - 5000) < 30)
        view.show(document, ReadingPosition(0, 5000), ReadingPreferences(textScale = 2.0), 2,
            0xffffffff.toInt()) { positions.add(it) }
        layout(view)
        val second = view.getChildAt(0).height
        assertTrue(second > first)
        assertTrue(abs(view.scrollY.toLong() * 10_000 / (second - 400) - 5000) < 30)
        assertTrue(positions.isEmpty())
        owner.pause().stop().destroy()
    }
}
