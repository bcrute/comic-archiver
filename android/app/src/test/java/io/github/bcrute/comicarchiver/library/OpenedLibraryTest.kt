package io.github.bcrute.comicarchiver.library

import android.app.Application
import android.net.Uri
import android.os.Looper
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import java.io.ByteArrayInputStream

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class OpenedLibraryTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val tree = Uri.parse("content://fixture.documents/tree/root")
    private val docs = FakePortableDocuments().apply { simple() }
    private val operations = CopyOnWriteArrayList<String>()
    private val bridge = object : PythonBridge {
        override fun dispatch(op: String, argsJson: String): String {
            operations.add(op)
            check(op == "portable_catalog") { "Folder browsing must never call Go, workspace, session or cloud operations" }
            val source = JSONObject(File(JSONObject(argsJson).getString("manifest_path")).readText())
            val files = source.getJSONArray("folders").getJSONObject(0).getJSONArray("files")
            val rows = JSONArray()
            for (index in 0 until files.length()) {
                val fact = files.getJSONObject(index)
                rows.put(JSONObject().put("row_id", fact.getString("key")).put("open", fact.getString("key"))
                    .put("series", "Series").put("title", "One").put("filename", fact.getString("name"))
                    .put("type", "Comic").put("size", "20 B").put("storage", "Opened folder")
                    .put("saved_epoch", JSONObject.NULL).put("file_time", true))
            }
            return JSONObject().put("ok", true).put("result", JSONObject().put("rows", rows)
                .put("total", rows.length()).put("offset", 0).put("limit", 50).put("summary", "Folder files")
                .put("range", "Showing folder files").put("notice", "Loaded")
                .put("locations", JSONArray().put(JSONObject().put("id", "device").put("label", "Opened folder")))
                .put("kinds", JSONArray().put(JSONObject().put("key", "episodes").put("label", "Comics and novels")))
                .put("sorts", JSONArray().put(JSONObject().put("key", "series").put("label", "Series")))
                .put("page_sizes", JSONArray(listOf(50, 100, 200)))).toString()
        }
    }
    @Before fun cleanPreferences() { context.getSharedPreferences("comicarchiver_opened_library", 0).edit().clear().commit() }
    @After fun stopScope() { scope.cancel() }
    private fun runtime() = OpenedLibrary(context, bridge, scope, docs)
    private fun await(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
        while (System.nanoTime() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (condition()) return
            Thread.sleep(10)
        }
        fail("Folder operation did not settle")
    }
    private fun ready(runtime: OpenedLibrary) = await { !runtime.state.value.catalog.loading &&
        runtime.state.value.catalog.page != null }

    @Test fun selectionAndColdReopenUseReadOnlyCatalogWithoutGoOrWorkingLibraryChanges() {
        val first = runtime(); first.select(tree); ready(first)
        assertEquals(2, first.state.value.catalog.page!!.rows.size)
        assertEquals(tree.toString(), context.getSharedPreferences("comicarchiver_opened_library", 0).getString("tree", null))
        val reopened = runtime(); reopened.ensureLoaded(); ready(reopened)
        assertTrue(reopened.state.value.selected)
        assertTrue(operations.all { it == "portable_catalog" })
        assertFalse(context.filesDir.resolve("workspace/Library").exists())
        assertTrue(docs.opened.isEmpty())
    }

    @Test fun failedFolderChangeKeepsThePreviouslyOpenedSourceAndCatalog() {
        val runtime = runtime(); runtime.select(tree); ready(runtime)
        val previous = runtime.state.value.catalog.page
        docs.allowed = false
        runtime.select(Uri.parse("content://unavailable.documents/tree/other"))
        await { !runtime.state.value.choosing }
        assertSame(previous, runtime.state.value.catalog.page)
        assertNotNull(runtime.state.value.message)
        assertEquals(tree.toString(), context.getSharedPreferences("comicarchiver_opened_library", 0).getString("tree", null))
    }

    @Test fun revokedAccessShowsRecoveryAndKeepsPreviouslyKnownFilesOutOfAnEmptySuccess() {
        val runtime = runtime(); runtime.select(tree); ready(runtime)
        docs.allowed = false
        runtime.load(CatalogQuery(), true)
        await { !runtime.state.value.catalog.loading }
        assertTrue(runtime.state.value.needsFolder)
        assertNotNull(runtime.state.value.catalog.error)
        assertEquals(2, runtime.state.value.catalog.page!!.rows.size)
    }

    @Test fun closeCancelsALateFolderSelectionAndCannotResurrectItsSavedGrantOrFiles() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        docs.beforeRoot = { entered.countDown(); check(release.await(10, TimeUnit.SECONDS)) }
        val runtime = runtime(); runtime.select(tree)
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        val oldTicket = runtime.pickerTicket()
        runtime.close(); release.countDown()
        await { !runtime.state.value.choosing }
        shadowOf(Looper.getMainLooper()).idle()
        assertFalse(runtime.state.value.selected)
        assertNull(runtime.state.value.catalog.page)
        assertNull(context.getSharedPreferences("comicarchiver_opened_library", 0).getString("tree", null))
        assertTrue(operations.isEmpty())
        runtime.select(tree, oldTicket)
        assertFalse(runtime.state.value.selected)
    }

    @Test fun onlyAnOfferedUnchangedDocumentCanBeOpenedAndMissingFilesGetFeedback() {
        val runtime = runtime(); runtime.select(tree); ready(runtime)
        runtime.open("f".repeat(64)); assertTrue(docs.opened.isEmpty())
        val id = runtime.state.value.catalog.page!!.rows.first().openId!!
        runtime.open(id)
        await { runtime.state.value.catalog.openingId == null }
        assertEquals(listOf("comic"), docs.opened.toList())
        docs.opened.add("changed")
        runtime.open(id)
        await { runtime.state.value.catalog.openingId == null }
        assertTrue(runtime.state.value.catalog.feedback!!.contains("changed"))
        assertEquals(2, docs.opened.size)
    }

    @Test fun builtInSelectionStagesActualBytesWithoutLaunchingAnExternalAppOrGo() {
        val workspace = context.filesDir.canonicalFile.resolve("selected-reader-workspace")
        val copies = mutableListOf<PreparedPortableReading>()
        val source = object : PortableDocuments by docs {
            override fun prepareReading(tree: Uri, document: PortableDocument, workspace: File,
                checkActive: () -> Unit): PreparedPortableReading {
                val file = PreparedPortableReading.copyStore(workspace)
                    .stage(document.name, 20, { ByteArrayInputStream(ByteArray(20) { 4 }) }, checkActive)
                return PreparedPortableReading.captured(workspace, file)
            }
        }
        val runtime = OpenedLibrary(context, bridge, scope, source)
        runtime.select(tree); ready(runtime)
        runtime.readHere("f".repeat(64), workspace) { fail("Unlisted file admitted"); false }
        val id = runtime.state.value.catalog.page!!.rows.first().openId!!
        runtime.readHere(id, workspace) { copies.add(it); true }
        await { runtime.state.value.catalog.openingId == null }
        assertEquals(1, copies.size)
        assertArrayEquals(ByteArray(20) { 4 }, copies.single().file.readBytes())
        assertTrue(docs.opened.isEmpty())
        assertTrue(operations.all { it == "portable_catalog" })
        copies.single().discard()
        docs.allowed = false
        runtime.readHere(id, workspace) { fail("Lost grant admitted"); false }
        await { runtime.state.value.catalog.openingId == null }
        assertTrue(runtime.state.value.catalog.feedback!!.contains("could not be prepared"))
    }

    @Test fun closingDuringPreparedReadKeepsTheCopyUntilActualIoReturnsThenDiscardsIt() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        val prepared = AtomicReference<PreparedPortableReading>()
        val admissions = AtomicInteger()
        val workspace = context.filesDir.canonicalFile.resolve("cancelled-reader-workspace")
        val source = object : PortableDocuments by docs {
            override fun prepareReading(tree: Uri, document: PortableDocument, workspace: File,
                checkActive: () -> Unit): PreparedPortableReading {
                val file = PreparedPortableReading.copyStore(workspace)
                    .stage(document.name, 20, { ByteArrayInputStream(ByteArray(20) { 5 }) }, checkActive)
                val value = PreparedPortableReading.captured(workspace, file)
                prepared.set(value); entered.countDown()
                check(release.await(10, TimeUnit.SECONDS))
                return value
            }
        }
        val runtime = OpenedLibrary(context, bridge, scope, source)
        runtime.select(tree); ready(runtime)
        val id = runtime.state.value.catalog.page!!.rows.first().openId!!
        runtime.readHere(id, workspace) { admissions.incrementAndGet(); true }
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        try {
            runtime.close()
            assertFalse(runtime.state.value.selected)
            assertTrue(prepared.get().file.isFile)
        } finally { release.countDown() }
        await { !prepared.get().file.exists() }
        assertEquals(0, admissions.get())
        assertFalse(runtime.state.value.selected)
    }
}
