package io.github.bcrute.comicarchiver.reader

import android.app.Application
import android.os.Looper
import io.github.bcrute.comicarchiver.library.PreparedPortableReading
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Real private copy/serial controller/native decoding; JNI replies are held95 fixtures.
 * Actual core/bridge decoding and real Android SAF have separate acceptance. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class SelectedReaderControllerTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val root = RuntimeEnvironment.getApplication().filesDir.canonicalFile.resolve("selected-" + UUID.randomUUID())
    private var current: File? = root
    private val calls = CopyOnWriteArrayList<String>()
    private var failOpen = false
    private var activeKind = "cbz"
    private var holdOpen: Pair<CountDownLatch, CountDownLatch>? = null
    private var holdClose: Pair<CountDownLatch, CountDownLatch>? = null
    private val bridge = object : PythonBridge {
        override fun dispatch(op: String, argsJson: String): String {
            calls.add(op)
            val args = JSONObject(argsJson); val id = args.getString("reader_id")
            val value = when (op) {
                "reader_open" -> {
                    check(args.has("selected") && !args.has("id"))
                    val file = root.resolve("Library").resolve(args.getString("selected"))
                    check(file.isFile)
                    activeKind = file.extension
                    holdOpen?.let { it.first.countDown(); check(it.second.await(10, TimeUnit.SECONDS)) }
                    if (failOpen) {
                        failOpen = false
                        return JSONObject().put("v", 1).put("ok", false).put("code", "unavailable")
                            .put("message", "Choose Read here again.").toString()
                    }
                    ReaderFixtures.book(id, activeKind)
                }
                "reader_page" -> ReaderFixtures.page(id, activeKind, args.getInt("unit"))
                "reader_close" -> {
                    holdClose?.let { it.first.countDown(); check(it.second.await(10, TimeUnit.SECONDS)) }
                    JSONObject().put("closed", true)
                }
                else -> error("Unexpected worker, session, export, cloud or reader operation")
            }
            return JSONObject().put("v", 1).put("ok", true).put("result", value).toString()
        }
    }
    private val reader = LocalReaderController(bridge, scope) { current }
    private fun copied(workspace: File = root, kind: String = "cbz"): PreparedPortableReading {
        val source = ReaderFixtures.root.resolve("payload").resolve(ReaderFixtures.item(kind).getString("archive"))
        val target = PreparedPortableReading.copyStore(workspace)
            .stage(source.name, source.length(), { source.inputStream() })
        return PreparedPortableReading.captured(workspace, target)
    }
    private fun await(condition: () -> Boolean) {
        val until = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
        while (System.nanoTime() < until) {
            shadowOf(Looper.getMainLooper()).idle()
            if (condition()) return
            Thread.sleep(10)
        }
        fail("Selected reader did not settle")
    }
    @After fun retire() {
        holdOpen?.second?.countDown(); holdClose?.second?.countDown()
        reader.dispose(); scope.cancel()
    }
    @Test fun repeatedAdmissionAndSlowCloseKeepTheRealCopyUntilTheSerialCloseCompletes() {
        val first = copied(); val second = copied()
        assertTrue(reader.openPrepared(first))
        await { reader.state.value.document != null && !reader.state.value.busy }
        assertFalse(reader.openPrepared(second)); second.discard()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        holdClose = entered to release
        reader.close()
        try {
            await { entered.count == 0L }
            assertTrue(reader.state.value.closing)
            assertFalse(reader.state.value.open)
            assertTrue(first.file.isFile)
        } finally { release.countDown() }
        await { !reader.state.value.closing }
        assertFalse(first.file.exists())
        assertEquals(1, calls.count { it == "reader_open" })
        assertEquals(1, calls.count { it == "reader_close" })
    }
    @Test fun retryOfInitialFailureReusesTheOwnedCopyAndNeverFallsBackToARegisteredId() {
        val source = copied(); failOpen = true
        assertTrue(reader.openPrepared(source))
        await { !reader.state.value.loading && reader.state.value.error != null }
        assertTrue(source.file.isFile)
        reader.reload()
        await { reader.state.value.document != null && !reader.state.value.busy }
        assertTrue(source.file.isFile)
        assertEquals(2, calls.count { it == "reader_open" })
        reader.close(); await { !reader.state.value.closing }
        assertFalse(source.file.exists())
    }
    @Test fun bothSelectedFormatsReachTheirNativeDecoderAndRetireOnlyTheirPrivateCopy() {
        for (kind in listOf("cbz", "epub")) {
            val original = ReaderFixtures.root.resolve("payload").resolve(ReaderFixtures.item(kind).getString("archive"))
            val bytes = original.readBytes()
            val source = copied(kind = kind)
            assertTrue(reader.openPrepared(source))
            await { reader.state.value.document != null && !reader.state.value.busy }
            assertEquals(kind, reader.state.value.book!!.record.kind)
            if (kind == "cbz") assertTrue(reader.state.value.document is ReaderDocument.Comic)
            else assertTrue((reader.state.value.document as ReaderDocument.Novel).text.isNotBlank())
            assertArrayEquals(bytes, source.file.readBytes())
            reader.close(); await { !reader.state.value.closing }
            assertFalse(source.file.exists())
            assertArrayEquals(bytes, original.readBytes())
        }
        assertEquals(2, calls.count { it == "reader_open" })
        assertEquals(2, calls.count { it == "reader_close" })
    }
    @Test fun cachePressureCannotRetireTheCopyBeforeItsBlockedReaderCloseCompletes() {
        val source = copied()
        assertTrue(reader.openPrepared(source))
        await { reader.state.value.document != null && !reader.state.value.busy }
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        holdClose = entered to release
        reader.close()
        try {
            await { entered.count == 0L }
            val archive = ReaderFixtures.root.resolve("payload").resolve(ReaderFixtures.item("cbz").getString("archive"))
            val bytes = archive.readBytes()
            // These are unowned staging leftovers, as after interrupted opens.
            // New staging must retain the actual owner while bounding leftovers.
            val store = PreparedPortableReading.copyStore(root)
            repeat(6) {
                store.stage(archive.name, archive.length(), { archive.inputStream() })
                assertTrue(source.file.isFile)
                assertArrayEquals(bytes, source.file.readBytes())
            }
            assertTrue(reader.state.value.closing)
            assertEquals(4, source.file.parentFile!!.parentFile!!.listFiles()!!.size)
        } finally { release.countDown() }
        await { !reader.state.value.closing }
        assertFalse(source.file.exists())
        assertEquals(1, calls.count { it == "reader_close" })
    }
    @Test fun workspaceChangeDuringOpenCannotPaintALatePageOrDiscardBeforeClose() {
        val other = root.parentFile.resolve("other-" + UUID.randomUUID())
        val refused = copied(other)
        assertFalse(reader.openPrepared(refused)); assertTrue(calls.isEmpty()); refused.discard()
        val source = copied()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        holdOpen = entered to release
        assertTrue(reader.openPrepared(source))
        await { entered.count == 0L }
        try {
            current = other; reader.contextChanged(other)
            assertTrue(reader.state.value.closing)
            assertNull(reader.state.value.document)
            assertTrue(source.file.isFile)
        } finally { release.countDown() }
        await { !reader.state.value.closing }
        assertNull(reader.state.value.document)
        assertFalse(source.file.exists())
    }
}
