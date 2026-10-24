package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.Density
import io.github.bcrute.comicarchiver.reader.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [34], qualifiers = "w320dp-h480dp")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class LocalReaderScreenTest {
    @get:Rule val compose = createComposeRule()
    // String semantics rebuilds the paragraph with parent constraints in Compose
    // 1.7.5. Synthetic overflow flags cannot qualify the drawn label. Check every
    // complete, unellipsized line against the actual measured text box instead.
    private fun fitsWholeText(layout: TextLayoutResult): Boolean =
        layout.lineCount > 0 && layout.getLineStart(0) == 0 &&
            layout.getLineEnd(layout.lineCount - 1, visibleEnd = true) == layout.layoutInput.text.length &&
            (0 until layout.lineCount).all { line ->
                !layout.isLineEllipsized(line) && layout.getLineLeft(line) >= 0f &&
                    layout.getLineRight(line) <= layout.size.width.toFloat() &&
                    layout.getLineTop(line) >= 0f && layout.getLineBottom(line) <= layout.size.height.toFloat()
            }

    @Test fun wholeTextOracleAcceptsCompleteLinesAndRejectsWidthAndHeightClipping() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme {
                    Column {
                        Text("Reader options", Modifier.testTag("natural"))
                        Text("Reader options", Modifier.width(100.dp).testTag("wrapped"))
                        Text("Reader options", Modifier.width(20.dp).testTag("clipped-width"),
                            maxLines = 1, softWrap = false)
                        Text("Reader options", Modifier.height(5.dp).testTag("clipped-height"))
                    }
                }
            }
        }
        fun fits(tag: String): Boolean {
            val layouts = mutableListOf<TextLayoutResult>()
            compose.onNodeWithTag(tag, useUnmergedTree = true)
                .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
            assertEquals(1, layouts.size)
            return fitsWholeText(layouts.single())
        }
        assertTrue(fits("natural")); assertTrue(fits("wrapped"))
        assertFalse(fits("clipped-width")); assertFalse(fits("clipped-height"))
    }

    @Test fun realLargeTextKeepsReaderCloseFitBookmarksAndReloadWholeAndReachable() {
        val id = "a".repeat(32)
        val book = ReaderProtocol.book(ReaderFixtures.book(id, "cbz"), id)
        val document = LocalReaderMedia.decode(ReaderFixtures.page(id, "cbz", 1), book, 1)
        val state = mutableStateOf(LocalReaderState(id = id, book = book, document = document,
            position = book.record.position, preferences = book.record.preferences))
        var closed = 0; var bookmarks = 0; var reloads = 0
        val actions = LocalReaderActions(close = { closed++ }, bookmark = { bookmarks++ }, reload = { reloads++ },
            preferences = { state.value = state.value.copy(preferences = it) })
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 2f)) {
                MaterialTheme { LocalReaderScreen(state.value, actions) }
            }
        }
        fun whole(label: String) {
            val layouts = mutableListOf<TextLayoutResult>()
            compose.onNodeWithText(label, useUnmergedTree = true).performScrollTo()
                .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
            assertTrue("The entire $label label must fit: " + layouts.joinToString {
                "size=${it.size}, lines=${it.lineCount}, constraints=${it.layoutInput.constraints}, " +
                    "bounds=" + (0 until it.lineCount).joinToString { line ->
                        "${it.getLineLeft(line)}..${it.getLineRight(line)} x ${it.getLineTop(line)}..${it.getLineBottom(line)}"
                    }
            }, layouts.isNotEmpty() && layouts.all(::fitsWholeText))
            compose.onNodeWithText(label).assertIsDisplayed()
        }
        whole("Close reader")
        compose.onNodeWithText("Reader options").performScrollTo().performClick()
        whole("Close options"); whole("Fit width")
        compose.onNodeWithText("Fit width").performClick()
        assertEquals("width", state.value.preferences.fit)
        whole("Remove bookmark")
        compose.onNodeWithText("Remove bookmark").performClick()
        whole("Reload saved progress")
        compose.onNodeWithText("Reload saved progress").performClick()
        compose.onNodeWithText("Close options").performScrollTo().performClick()
        compose.onNodeWithText("Close reader").performScrollTo().performClick()
        assertEquals(1, closed); assertEquals(1, bookmarks); assertEquals(1, reloads)
    }
}
