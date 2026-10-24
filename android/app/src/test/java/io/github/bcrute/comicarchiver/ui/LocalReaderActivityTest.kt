package io.github.bcrute.comicarchiver.ui

import android.os.Looper
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import io.github.bcrute.comicarchiver.App
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.reader.ReaderFixtures
import io.github.bcrute.comicarchiver.reader.ReadingPosition
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.SiteWorkspace
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.After
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Actual MainActivity/controller/Compose/native image/text; only the JNI replies are fixtures.
 * Python bridge tests and the later phone sitting separately qualify actual JNI/core/files. */
@RunWith(RobolectricTestRunner::class)
@Config(application = LocalReaderTestApp::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class LocalReaderActivityTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val app get() = compose.activity.application as LocalReaderTestApp
    @After fun retireReader() { compose.runOnIdle { app.localReader.dispose() } }
    private fun await(condition: () -> Boolean) = compose.waitUntil(10_000) {
        shadowOf(Looper.getMainLooper()).idle(); condition()
    }
    private fun files() {
        await { app.controller.state.value.workspaceReady && !app.controller.state.value.siteBusy }
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
        compose.onNodeWithText("Files").performClick()
        await { app.controller.state.value.catalog.page?.rows?.size == 2 && !app.controller.state.value.catalog.loading }
    }
    private fun read(kind: String = "cbz") {
        files()
        val description = if (kind == "cbz") "Read One, Test Comic. one.cbz. This device"
            else "Read Novel, Test Comic. novel.epub. This device"
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
        compose.onNode(hasContentDescription(description)).performClick()
        await { !app.localReader.state.value.busy && app.localReader.state.value.document != null }
        compose.onNodeWithTag("local-reader").assertIsDisplayed()
    }
    private fun options() = compose.onNodeWithText("Reader options").performClick()

    @Test fun filesReadHereNavigatesBookmarksClosesAndReopensWithoutGoOrAnExternalReader() {
        read()
        compose.onNodeWithText("Page 2 of 2").assertIsDisplayed()
        compose.onNodeWithText("Previous").performClick()
        await { !app.localReader.state.value.busy && app.localReader.state.value.position.unit == 0 }
        compose.onNodeWithText("Page 1 of 2").assertIsDisplayed()
        options()
        compose.onNodeWithText("Add bookmark").performScrollTo().performClick()
        await { !app.localReader.state.value.busy && ReadingPosition() in app.localReader.state.value.book!!.record.bookmarks }
        compose.onNodeWithText("Close options").performScrollTo().performClick()
        compose.onNodeWithText("Close reader").performClick()
        await { !app.localReader.state.value.closing }
        compose.onNodeWithText("Saved files").performScrollTo().assertIsDisplayed()
        val id = app.fake.persisted("cbz")
        assertEquals(0, id.getJSONObject("position").getInt("unit"))
        assertEquals(0, app.fake.delegate.count("go")); assertEquals(0, app.fake.delegate.count("worker_start"))
        assertEquals(0, app.fake.delegate.count("file_open"))
        read()
        compose.onNodeWithText("Page 1 of 2").assertIsDisplayed()
        assertTrue(ReadingPosition() in app.localReader.state.value.book!!.record.bookmarks)
    }
    @Test fun novelTextScaleBookmarkAndReloadUseTheRealOptionsAndNativeTextView() {
        read("epub")
        compose.onNodeWithText("Chapter 1 of 1").assertIsDisplayed()
        options()
        compose.onNodeWithText("Larger text").performScrollTo().performClick()
        await { !app.localReader.state.value.busy && app.localReader.state.value.preferences.textScale == 1.5 }
        compose.onNodeWithText("Text 150%").assertIsDisplayed()
        compose.onNodeWithText("Remove bookmark").performScrollTo().performClick()
        await { !app.localReader.state.value.busy && app.localReader.state.value.book!!.record.bookmarks.isEmpty() }
        compose.onNodeWithText("Reload saved progress").performScrollTo().performClick()
        await { !app.localReader.state.value.busy }
        assertEquals(1.5, app.localReader.state.value.preferences.textScale, 0.0)
        assertEquals(1.5, app.fake.persisted("epub").getJSONObject("preferences").getDouble("text_scale"), 0.0)
        assertEquals(0, app.fake.delegate.count("go"))
    }
    @Test fun activityRecreationKeepsOneReaderAndReturnsToTheFilesScreen() {
        read()
        val id = app.localReader.state.value.id
        compose.activityRule.scenario.recreate()
        await { !app.localReader.state.value.busy }
        compose.onNodeWithText("Page 2 of 2").assertIsDisplayed()
        assertEquals(id, app.localReader.state.value.id)
        assertEquals(1, app.fake.count("reader_open"))
        compose.runOnIdle { compose.activity.onBackPressedDispatcher.onBackPressed() }
        await { !app.localReader.state.value.closing }
        compose.onNodeWithText("Saved files").performScrollTo().assertIsDisplayed()
        assertEquals(0, app.fake.delegate.count("worker_start"))
    }
    @Test fun closingDuringSlowOpenReturnsNavigationBeforeIoRetiresAndNeverPaintsTheLatePage() {
        files()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        app.fake.holdOpen = entered to release
        val description = "Read One, Test Comic. one.cbz. This device"
        compose.onNodeWithTag("saved-files-list").performScrollToNode(hasContentDescription(description))
        compose.onNode(hasContentDescription(description)).performClick()
        await { entered.count == 0L }
        try {
            compose.onNodeWithText("Close reader").performClick()
            assertFalse(app.localReader.state.value.open)
            assertTrue(app.localReader.state.value.closing)
            compose.onNode(hasText("Saving") and hasClickAction()).performClick()
            compose.onNodeWithText("Nothing downloads until you press Go.").assertIsDisplayed()
        } finally { release.countDown() }
        await { !app.localReader.state.value.closing }
        assertNull(app.fake.activeReader)
        assertFalse(app.localReader.state.value.open)
        assertEquals(0, app.fake.delegate.count("go"))
    }
    @Test fun unavailableProgressKeepsReadingVisibleAndExplicitReloadRecovers() {
        app.fake.damagedProgress = true
        read()
        assertFalse(app.localReader.state.value.book!!.canSave)
        assertNotNull(app.localReader.state.value.book!!.warning)
        options()
        app.fake.damagedProgress = false
        compose.onNodeWithText("Reload saved progress").performScrollTo().performClick()
        await { !app.localReader.state.value.busy && app.localReader.state.value.book!!.canSave }
        assertNull(app.localReader.state.value.book!!.warning)
        assertNotNull(app.localReader.state.value.document)
        assertEquals(0, app.fake.delegate.count("go"))
    }
    @Test fun contextInvalidationRetiresTheReaderAndKeepsTheHeldApplicationUsable() {
        read()
        compose.runOnIdle { app.localReader.contextChanged(null) }
        await { !app.localReader.state.value.open && !app.localReader.state.value.closing }
        assertNull(app.fake.activeReader)
        compose.onNode(hasText("Saving") and hasClickAction()).performClick()
        compose.onNodeWithText("Nothing downloads until you press Go.").assertIsDisplayed()
        assertEquals(0, app.fake.delegate.count("worker_start"))
    }
}

class LocalReaderTestApp : App() {
    val fake = ReaderLifecycleBridge()
    override val bridge: PythonBridge get() = fake
    override val signInSites get() = WebViewSignInEngine.BETA_SITES
    override fun startRuntime() {}
    override fun selectRuntimeCookieStore(store: WebViewCookieStore, siteKey: String) {}
    override fun startNetworkWriter(target: SiteWorkspace) {}
    override fun deactivateRuntimeCookieStore() {}
}

class ReaderLifecycleBridge : PythonBridge {
    val delegate = LifecycleBridge()
    private val counts = ConcurrentHashMap<String, AtomicInteger>()
    private val records = mutableMapOf("cbz" to ReaderFixtures.record("cbz").toString(),
        "epub" to ReaderFixtures.record("epub").toString())
    @Volatile var holdOpen: Pair<CountDownLatch, CountDownLatch>? = null
    @Volatile var activeReader: String? = null
    @Volatile var damagedProgress = false
    private var kind = "cbz"
    fun count(op: String) = counts[op]?.get() ?: 0
    @Synchronized fun persisted(kind: String) = JSONObject(records.getValue(kind))
    private fun envelope(value: JSONObject) = JSONObject().put("v", 1).put("ok", true).put("result", value).toString()
    private fun book() = ReaderFixtures.book(requireNotNull(activeReader), kind, persisted(kind)).apply {
        if (damagedProgress) put("can_save", false).put("warning", "Saved reading progress is unavailable. Reading still works; the existing record is retained.")
    }
    override fun dispatch(op: String, argsJson: String): String {
        if (op == "file_catalog") {
            val value = JSONObject(delegate.dispatch(op, argsJson))
            val result = value.getJSONObject("result")
            val rows = result.getJSONArray("rows")
            rows.put(JSONObject(rows.getJSONObject(0).toString()).put("row_id", "b".repeat(64)).put("open", "b".repeat(64))
                .put("title", "Novel").put("filename", "novel.epub").put("type", "Novel"))
            result.put("total", 2).put("summary", "Two saved episodes").put("range", "Showing 1–2 of 2 files.")
            return value.toString()
        }
        if (!op.startsWith("reader_")) return delegate.dispatch(op, argsJson)
        counts.computeIfAbsent(op) { AtomicInteger() }.incrementAndGet()
        val args = JSONObject(argsJson)
        if (op == "reader_open") {
            holdOpen?.also { holdOpen = null }?.let { (entered, release) ->
                entered.countDown(); check(release.await(20, TimeUnit.SECONDS))
            }
            activeReader = args.getString("reader_id")
            kind = if (args.getString("id") == "b".repeat(64)) "epub" else "cbz"
        }
        if (op == "reader_close") {
            val matched = activeReader == args.getString("reader_id")
            if (matched) activeReader = null
            return envelope(JSONObject().put("closed", matched))
        }
        check(activeReader == args.getString("reader_id"))
        if (op == "reader_page") return envelope(ReaderFixtures.page(requireNotNull(activeReader), kind, args.getInt("unit")))
        if (op == "reader_save" && !damagedProgress) synchronized(this) {
            val value = persisted(kind)
            val position = args.getJSONObject("position")
            if (args.getBoolean("bookmark")) {
                val bookmarks = value.getJSONArray("bookmarks")
                val match = (0 until bookmarks.length()).firstOrNull { index ->
                    val row = bookmarks.getJSONObject(index)
                    row.getInt("unit") == position.getInt("unit") && row.getInt("offset") == position.getInt("offset")
                }
                if (match == null) bookmarks.put(JSONObject(position.toString())) else bookmarks.remove(match)
            }
            value.put("position", position).put("preferences", args.getJSONObject("preferences"))
                .put("revision", value.getLong("revision") + 1)
            records[kind] = value.toString()
        }
        return envelope(book())
    }
}
