package io.github.bcrute.comicarchiver.ui

import android.net.Uri
import android.os.Looper
import android.provider.DocumentsContract.Document
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.runtime.*
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Actual activity/coordinator/capture/filesystem, with synthetic documents and preparation.
 * These fixtures are not archives. Real SAF, JNI/core verification and cookie binding remain native gates. */
@RunWith(RobolectricTestRunner::class)
@Config(application = WorkingFlowTestApp::class, sdk = [34], qualifiers = "w393dp-h1000dp",
    shadows = [WorkingLibraryFileSystemShadow::class])
class WorkingLibraryActivityTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val app get() = compose.activity.application as WorkingFlowTestApp
    private val source = Uri.parse("content://fixture.documents/tree/root")
    private fun await(condition: () -> Boolean) = compose.waitUntil(10_000) { shadowOf(Looper.getMainLooper()).idle(); condition() }
    private fun ready() = await { !app.controller.state.value.siteBusy && !app.workingLibraries.state.value.loading &&
        app.controller.state.value.signIn == SignInState.SignedIn("tapas") }
    private fun enterPreview() {
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
        compose.onNodeWithText("Change working library").performClick()
        compose.onNodeWithText("Working libraries").assertIsDisplayed()
        compose.runOnIdle {
            val ticket = app.workingLibraries.requestPicker()!!
            app.workingLibraries.picked(source, ticket)
        }
        await { app.workingLibraries.state.value.phase == WorkingLibraryPhase.PREVIEW }
    }
    private fun confirm() {
        compose.onNodeWithText("Copy, verify and use").performScrollTo().performClick()
        await { !app.controller.state.value.libraryBusy && app.workingLibraries.state.value.phase == WorkingLibraryPhase.IDLE }
    }

    @Test fun previewNeedsConfirmationThenUsesANewHeldLibraryAndReturnsToTheOriginalWithoutGo() {
        ready()
        val original = app.workspace
        val sentinel = original.library.resolve("Original/keep.cbz").apply { parentFile!!.mkdirs(); writeText("retained original") }
        val exports = app.exports
        val surface = app.surfaces.last()
        enterPreview()
        assertEquals(0, app.sourceDocuments.opens.get()); assertNull(app.libraryLocations.pending())
        assertEquals(original.libraryId, app.controller.state.value.libraryId)
        compose.runOnIdle { app.controller.go() }
        assertEquals(0, app.wire.count("go"))
        confirm()
        val selected = app.workspace
        assertNotEquals(original.libraryId, selected.libraryId)
        assertEquals(selected.libraryId, app.libraryLocations.activeId())
        assertArrayEquals(app.sourceDocuments.chapter, selected.library.resolve("Series/chapter.cbz").readBytes())
        assertEquals("retained original", sentinel.readText())
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertNotSame(exports, app.exports)
        assertNotSame(surface, app.surfaces.last())
        assertEquals(0, app.wire.count("go")); assertEquals(0, app.wire.count("worker_start"))
        compose.runOnIdle { app.workingLibraries.select("default") }
        await { app.controller.state.value.libraryId == "default" && !app.controller.state.value.libraryBusy }
        assertSame(exports, app.exports); assertEquals("retained original", sentinel.readText())
        assertTrue(selected.library.resolve("Series/chapter.cbz").isFile)
    }

    @Test fun cancellingAPreviewReleasesAdmissionAndAStalePickerCannotCreateACopy() {
        ready(); enterPreview()
        compose.onNodeWithText("Cancel").performScrollTo().performClick()
        await { !app.controller.state.value.libraryBusy }
        assertEquals("default", app.controller.state.value.libraryId)
        assertNull(app.libraryLocations.pending()); assertEquals(1, app.libraryLocations.libraries().size)
        assertEquals(0, app.sourceDocuments.opens.get())
        compose.runOnIdle {
            val old = app.workingLibraries.requestPicker()!!
            app.workingLibraries.picked(null, old)
            app.workingLibraries.picked(source, old)
        }
        assertFalse(app.controller.state.value.libraryBusy)
        assertNull(app.libraryLocations.pending()); assertEquals(0, app.wire.count("go"))
    }

    @Test fun aFailedRuntimeSwitchKeepsThePreviousRootAndTheVerifiedCopyCanBeOpenedLater() {
        ready(); val original = app.workspace; val exports = app.exports
        enterPreview(); app.refuseManagedRoot = true; confirm()
        assertEquals(original.libraryId, app.workspace.libraryId)
        assertEquals("default", app.libraryLocations.activeId()); assertSame(exports, app.exports)
        assertTrue(app.controller.state.value.workspaceReady)
        val readyCopy = app.libraryLocations.libraries().single { it.id != "default" }
        assertTrue(app.libraryLocations.root(readyCopy.id).resolve("Library/Series/chapter.cbz").isFile)
        app.refuseManagedRoot = false
        compose.runOnIdle { app.workingLibraries.select(readyCopy.id) }
        await { app.controller.state.value.libraryId == readyCopy.id && !app.controller.state.value.libraryBusy }
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.wire.count("go"))
    }

    @Test fun sourceRefreshCreatesANewCopyAndKeepsPhoneOnlyFilesInThePreviousOne() {
        ready(); enterPreview(); confirm()
        val previous = app.workspace
        val oldBytes = previous.library.resolve("Series/chapter.cbz").readBytes()
        val phoneOnly = previous.library.resolve("Phone only/keep.cbz").apply { parentFile!!.mkdirs(); writeText("phone only") }
        app.sourceDocuments.updateChapter("refreshed source bytes".toByteArray())
        compose.onNodeWithText("Refresh from source folder").performScrollTo().performClick()
        await { app.workingLibraries.state.value.phase == WorkingLibraryPhase.PREVIEW }
        confirm()
        assertNotEquals(previous.libraryId, app.workspace.libraryId)
        assertArrayEquals(oldBytes, previous.library.resolve("Series/chapter.cbz").readBytes())
        assertEquals("phone only", phoneOnly.readText())
        assertArrayEquals(app.sourceDocuments.chapter, app.workspace.library.resolve("Series/chapter.cbz").readBytes())
        assertEquals(3, app.libraryLocations.libraries().size)
        assertEquals(0, app.wire.count("go"))
    }

    @Test @Config(application = MissingWorkingFlowTestApp::class)
    fun aMissingSelectedRootShowsRecoveryWithoutCreatingAnEmptyReplacement() {
        val missing = app as MissingWorkingFlowTestApp
        await { !app.controller.state.value.siteBusy && !app.workingLibraries.state.value.loading }
        assertFalse(app.controller.state.value.workspaceReady)
        assertFalse(missing.missing.exists()); assertEquals(0, app.wire.count("prepare_workspace"))
        compose.onNode(hasText("Library") and hasClickAction()).performClick()
        compose.onNodeWithText("Change working library").performClick()
        compose.onNodeWithText("Working libraries").assertIsDisplayed()
        compose.runOnIdle { app.workingLibraries.select("default") }
        await { app.controller.state.value.libraryId == "default" && app.controller.state.value.workspaceReady && !app.controller.state.value.libraryBusy }
        assertFalse(missing.missing.exists())
        assertEquals("original", app.workspace.library.resolve("Original/keep.cbz").readText())
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals(0, app.wire.count("go"))
    }

    @Test fun aLateCatalogFailureCannotReplaceTheSameProvidersNewLibraryState() {
        ready()
        val sourceIdentity = WorkingLibraryCapture.source(source)
        val pending = (app.libraryLocations.begin("Ready copy", sourceIdentity) as WorkingLibraryStart.Copy).pending
        app.libraryLocations.verified(pending.id, "b".repeat(64)); val ready = app.libraryLocations.complete(pending.id)
        app.holdCatalog = true
        compose.runOnIdle { app.controller.loadCatalog(io.github.bcrute.comicarchiver.controller.CatalogQuery()) }
        assertTrue(app.catalogEntered.await(5, TimeUnit.SECONDS))
        try {
            compose.runOnIdle { app.workingLibraries.select(ready.id) }
            await { app.controller.state.value.libraryId == ready.id && !app.controller.state.value.libraryBusy }
        } finally { app.catalogRelease.countDown() }
        assertTrue(app.catalogFinished.await(5, TimeUnit.SECONDS))
        compose.runOnIdle { assertNull(app.controller.state.value.catalog.error) }
        assertEquals(0, app.wire.count("go"))
    }
}

open class WorkingFlowTestApp : SwitchTestApp() {
    internal val sourceDocuments = WorkingFlowDocuments()
    @Volatile var refuseManagedRoot = false
    @Volatile var holdCatalog = false
    val catalogEntered = CountDownLatch(1)
    val catalogRelease = CountDownLatch(1)
    val catalogFinished = CountDownLatch(1)
    override val bridge: PythonBridge = object : PythonBridge {
        override fun dispatch(op: String, argsJson: String): String {
            if (op == "file_catalog" && holdCatalog) {
                catalogEntered.countDown(); check(catalogRelease.await(5, TimeUnit.SECONDS)); catalogFinished.countDown()
                return """{"ok":false,"code":"refused","message":"Old library response"}"""
            }
            if (op == "prepare_workspace" && refuseManagedRoot && JSONObject(argsJson).getString("root").contains("working-libraries"))
                return """{"ok":false,"code":"refused","message":"The selected working library is unavailable."}"""
            return wire.dispatch(op, argsJson)
        }
    }
    internal override fun createWorkingLibraryDocuments(): WorkingLibraryDocuments = sourceDocuments
    internal override fun createWorkingLibraryPreparation() = WorkingLibraryPreparation(signInSites.keys) { call, control ->
        control.check()
        val ready = call.payload.resolve(".fixture-prepared")
        if (call.recover && !ready.isFile) JSONObject().put("v", 1).put("ok", true).put("result", JSONObject.NULL).toString()
        else {
            if (!call.recover) {
                check(call.captured.copyRecursively(call.payload, overwrite = false))
                ready.writeText("fixture proof, not archive verification")
            }
            JSONObject().put("v", 1).put("ok", true).put("result", JSONObject().put("digest", "b".repeat(64))
                .put("sites", JSONArray().put(JSONObject().put("key", "tapas").put("series", 1).put("episodes", 1)
                    .put("cloud_only", 0).put("account_bound", false)))).toString()
        }
    }
}

class MissingWorkingFlowTestApp : WorkingFlowTestApp() {
    lateinit var missing: java.io.File
    override fun onCreate() {
        super.onCreate()
        val pending = (libraryLocations.begin("Missing copy", WorkingLibraryCapture.source(
            Uri.parse("content://fixture.documents/tree/root"))) as WorkingLibraryStart.Copy).pending
        libraryLocations.verified(pending.id, "b".repeat(64)); val ready = libraryLocations.complete(pending.id)
        libraryLocations.select(ready.id); missing = libraryLocations.root(ready.id); missing.deleteRecursively()
        filesDir.resolve("workspace/Library/Original/keep.cbz").apply { parentFile!!.mkdirs(); writeText("original") }
    }
}

internal class WorkingFlowDocuments : WorkingLibraryDocuments {
    private val values = mutableMapOf("metadata" to "{}".toByteArray(), "episode" to "captured source bytes".toByteArray())
    private var revision = 1L
    val opens = AtomicInteger()
    val chapter: ByteArray get() = values.getValue("episode").copyOf()
    fun updateChapter(bytes: ByteArray) { values["episode"] = bytes.copyOf(); revision++ }
    private fun record(id: String): WorkingLibraryDocument {
        val name = when (id) { "root" -> "Source project"; "library" -> "Library"; "series" -> "Series"; "metadata" -> "series.json"; else -> "chapter.cbz" }
        return WorkingLibraryDocument(id, name, if (id in values) "application/octet-stream" else Document.MIME_TYPE_DIR,
            values[id]?.size?.toLong(), if (id in values) revision else null)
    }
    override fun retain(tree: Uri) {}
    override fun readable(tree: Uri) = true
    override fun root(tree: Uri, cancellation: LibraryCaptureCancellation) = record("root")
    override fun children(tree: Uri, parent: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation) =
        when (parent.id) { "root" -> listOf("library"); "library" -> listOf("series"); "series" -> listOf("metadata", "episode"); else -> emptyList() }.map(::record)
    override fun current(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation) = document == record(document.id)
    override fun open(tree: Uri, document: WorkingLibraryDocument, cancellation: LibraryCaptureCancellation): InputStream {
        cancellation.check(); opens.incrementAndGet(); return ByteArrayInputStream(values.getValue(document.id))
    }
}
