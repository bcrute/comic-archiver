package io.github.bcrute.comicarchiver.integration

import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import androidx.test.uiautomator.By
import androidx.test.uiautomator.BySelector
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.StaleObjectException
import androidx.test.uiautomator.UiObject2
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import androidx.core.content.FileProvider
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.export.FileJournalStore
import io.github.bcrute.comicarchiver.export.ExportManifestParser
import io.github.bcrute.comicarchiver.export.JournalState
import io.github.bcrute.comicarchiver.ui.ExportPhaseUi
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryPhase
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.regex.Pattern
import java.util.zip.CRC32
import java.util.zip.ZipFile

/** Two host-coordinated phases: the first is deliberately killed during a real provider write.
 * Only the second is expected to finish JUnit. Neither may run on the owner's dev phone. */
@RunWith(AndroidJUnit4::class)
class BetaJourneyTest {
    private val app get() = ApplicationProvider.getApplicationContext<BetaTestApplication>()
    private val device get() = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
    private val control = Uri.parse("content://io.github.bcrute.comicarchiver.test.control")
    private val evidence get() = app.filesDir.resolve("integration-code")
    private fun observe() = JSONObject(app.fixture().callAttr("observation").toString())
    private fun documents(action: String = "status"): Bundle =
        requireNotNull(app.contentResolver.call(control, action, null, null))

    private fun await(label: String, timeout: Long = 90_000,
                      terminal: () -> Boolean = { false }, condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeout
        while (SystemClock.elapsedRealtime() < deadline) {
            if (condition()) return
            if (terminal()) failure("Stopped before $label")
            SystemClock.sleep(100)
        }
        failure("Timed out: $label")
    }
    private fun captureFailure(label: String) {
        // This Application refuses real devices/accounts. Keep the genuine
        // controller/worker/network observation before instrumentation exits.
        save("timeout.json", JSONObject().put("waiting_for", label)
            .put("sign_in", app.controller.state.value.signIn.toString())
            .put("download", app.controller.state.value.download.toString())
            .put("settings", app.controller.state.value.settings.toString())
            .put("notice", app.controller.state.value.notice.toString())
            .put("observation", observe())
            .put("worker", JSONObject(app.bridge.dispatch("worker_status", JSONObject()
                .put("session_root", app.workspace.session.absolutePath).toString()))))
        device.takeScreenshot(evidence.resolve("timeout.png"))
        device.dumpWindowHierarchy(evidence.resolve("timeout-hierarchy.xml"))
    }
    private fun failure(label: String): Nothing {
        captureFailure(label)
        throw AssertionError(label)
    }
    private fun stage(name: String, details: JSONObject = JSONObject()) {
        save("checkpoint.json", details.put("phase", name).put("elapsed_ms", SystemClock.elapsedRealtime()))
    }
    private fun recreateObserved() {
        stage("recreating")
        val started = SystemClock.elapsedRealtime()
        val testThread = Thread.currentThread()
        val done = CountDownLatch(1)
        runCatching { app.fixture().callAttr("begin_recreation_watch") }
        val watcher = Thread({
            if (!done.await(15, TimeUnit.SECONDS)) runCatching {
                val rows = Thread.getAllStackTraces().entries.sortedBy { it.key.name }
                fun trace(row: Map.Entry<Thread, Array<StackTraceElement>>) =
                    row.key.name + " [" + row.key.state + "]\n" + row.value.joinToString("\n") + "\n\n"
                evidence.resolve("recreation-java.txt").writeText(rows.joinToString("") { trace(it) }.take(60_000))
                evidence.resolve("recreation-java-main.txt").writeText(rows.filter {
                    it.key.name == "main" || it.key === testThread
                }.joinToString("") { trace(it) }.take(12_000))
            }
        }, "fixture-recreation-watch").apply { isDaemon = true; start() }
        try {
            // ActivityScenario.recreate first waits indefinitely for UI idleness.
            // Exercise real framework recreation while saving is deliberately
            // active, then verify actual destruction and a new resumed instance.
            val instrumentation = InstrumentationRegistry.getInstrumentation()
            val previous = AtomicReference<MainActivity>()
            instrumentation.runOnMainSync {
                val current = ActivityLifecycleMonitorRegistry.getInstance()
                    .getActivitiesInStage(Stage.RESUMED).filterIsInstance<MainActivity>().single()
                previous.set(current)
                current.recreate()
            }
            await("native Activity destruction and resumed replacement", 30_000) {
                var replaced = false
                instrumentation.runOnMainSync {
                    val monitor = ActivityLifecycleMonitorRegistry.getInstance()
                    replaced = monitor.getLifecycleStageOf(previous.get()) == Stage.DESTROYED &&
                        monitor.getActivitiesInStage(Stage.RESUMED)
                            .filterIsInstance<MainActivity>().any { it !== previous.get() }
                }
                replaced
            }
        }
        finally {
            done.countDown()
            watcher.join(1_000)
            runCatching { app.fixture().callAttr("end_recreation_watch") }
        }
        stage("recreated", JSONObject().put("duration_ms", SystemClock.elapsedRealtime() - started))
    }
    private fun clickIfPresent(selector: BySelector, timeout: Long = 1_500,
                               before: (UiObject2) -> Unit = {}): Boolean {
        val deadline = SystemClock.elapsedRealtime() + timeout
        while (SystemClock.elapsedRealtime() < deadline) {
            try {
                val node = device.wait(Until.findObject(selector),
                    minOf(500L, maxOf(1L, deadline - SystemClock.elapsedRealtime()))) ?: continue
                assertTrue("Control disabled: $selector", node.isEnabled)
                before(node)
                node.click()
                return true
            } catch (_: StaleObjectException) {
                // DocumentsUI can replace a row between lookup and click.
                // Re-find that same selector, as the platform API requires;
                // do not retry a completed action or suppress other failures.
                SystemClock.sleep(100)
            }
        }
        return false
    }
    private fun nativeClick(selector: BySelector, label: String, timeout: Long = 15_000,
                            before: (UiObject2) -> Unit = {}) {
        if (!clickIfPresent(selector, timeout, before)) failure("Control not reachable: $label")
    }
    private fun click(text: String) {
        val destination = mapOf("Downloads" to "nav-saving", "Library" to "nav-library", "Settings" to "nav-settings")[text]
        val selector = if (destination != null) By.res(destination) else By.text(text)
        repeat(8) {
            // Wait for Compose's accessibility publication before scrolling.
            // A label may be a non-clickable child: click its real bounds.
            if (clickIfPresent(selector)) return
            try {
                device.findObjects(By.scrollable(true)).maxByOrNull { it.visibleBounds.height() }
                    ?.scroll(Direction.DOWN, 0.7f)
            } catch (_: StaleObjectException) {
                // Re-query the current scroll container on the next iteration.
            }
        }
        nativeClick(selector, text, 1_500) // Include the final scroll's result.
    }
    private fun startSaving() {
        nativeClick(By.res("saving-action"), "Start saving", 10_000) {
            assertTrue(device.hasObject(By.text("Start saving")) || device.hasObject(By.text("Resume")) || device.hasObject(By.text("Try again")))
        }
    }
    private fun openLibraryOptions() {
        nativeClick(By.desc("Library options"), "Library options", 10_000)
    }
    private fun openExport() { openLibraryOptions(); click("Export files") }
    private fun openSetting(name: String) {
        click("Settings")
        // Settings reopens on the page it last showed; start again from its list.
        if (device.wait(Until.hasObject(By.text("All settings")), 1_500)) click("All settings")
        click(name)
    }
    private fun allowFixtureConnectionIfHeld() {
        await("native connection settings") { app.controller.state.value.settings != null }
        if (app.controller.state.value.settings?.allowed == false) {
            // An emulator's real default route may be metered/unknown. Exercise
            // the user's normal explicit opt-in, never falsify platform facts.
            openSetting("Download preferences")
            nativeClick(By.desc("Also save on metered and mobile connections"), "Metered connection opt-in", 10_000) {
                assertFalse(it.isChecked)
            }
            await("explicit metered connection choice") { app.controller.state.value.settings?.allowed == true }
            click("Downloads")
        }
    }
    private fun save(name: String, value: JSONObject) {
        value.put("run_token", FixtureSafety.runToken)
        evidence.mkdirs()
        val scratch = evidence.resolve("$name.tmp")
        scratch.writeText(value.toString())
        check(scratch.renameTo(evidence.resolve(name)))
    }
    private fun archives() = app.workspace.root.resolve("Library").walkTopDown()
        .filter { it.isFile && it.extension in setOf("cbz", "epub") }.sortedBy { it.path }.toList()
    private fun hash(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes)
        .joinToString("") { "%02x".format(it) }
    private fun originals(root: File = app.workspace.root) = JSONArray(root.resolve("Library").walkTopDown()
        .filter { it.isFile && it.extension in setOf("cbz", "epub") }.sortedBy { it.path }.map { file -> JSONObject()
        .put("name", file.relativeTo(root).invariantSeparatorsPath)
        .put("bytes", file.length()).put("sha256", hash(file.readBytes())) }.toList())
    private fun sameOriginals(before: JSONArray) {
        val current = originals()
        assertEquals(before.toString(), current.toString())
    }
    private fun journal() = FileJournalStore(app.filesDir.resolve("export/journal-v1.txt")).load()
    private fun exportableCount(): Int {
        val manifest = ExportManifestParser.parse(app.bridge.dispatch("export_manifest",
            JSONObject().put("root", app.workspace.root.absolutePath).toString()))
        return if (manifest.refused || manifest.invalid != 0) -1 else manifest.entries.size
    }

    private fun verifyOutputs() {
        val files = archives()
        assertEquals(1, files.count { it.extension == "cbz" })
        assertEquals(1, files.count { it.extension == "epub" })
        assertTrue("Comic must exceed the provider pipe's capacity",
            files.single { it.extension == "cbz" }.length() > 1024 * 1024)
        val expected = observe().getJSONArray("page_hashes")
        ZipFile(files.single { it.extension == "cbz" }).use { zip ->
            val pages = zip.entries().asSequence().filter { it.name.endsWith(".png") }.toList()
            assertEquals(2, pages.size)
            pages.forEachIndexed { index, entry ->
                val bytes = zip.getInputStream(entry).use { it.readBytes() }
                assertEquals(expected.getString(index), hash(bytes))
                assertEquals(entry.crc, CRC32().apply { update(bytes) }.value)
            }
        }
        ZipFile(files.single { it.extension == "epub" }).use { zip ->
            assertEquals("application/epub+zip", zip.getInputStream(zip.getEntry("mimetype"))
                .bufferedReader().use { it.readText() })
            assertNotNull(zip.getEntry("META-INF/container.xml"))
            assertNotNull(zip.getEntry("EPUB/package.opf"))
            val chapter = zip.getInputStream(zip.getEntry("EPUB/chapter.xhtml")).bufferedReader().use { it.readText() }
            assertTrue(chapter.contains(observe().getString("novel_text")))
            assertFalse(chapter.contains("<script"))
        }
        val facts = observe()
        assertEquals(0, facts.getInt("asset_cookie_leaks"))
        assertEquals(0, facts.getInt("unexpected_requests"))
        assertFalse(facts.getBoolean("locked_reader_requested"))
        assertTrue(facts.getBoolean("browser_agent_seen"))
        assertEquals(2, facts.getJSONArray("subscribed_pages").length())
    }

    private fun openFixtureRoot() {
        val picker = By.pkg(Pattern.compile("com\\.(?:android|google\\.android)\\.documentsui"))
        await("system folder picker", 15_000) { device.hasObject(picker) }
        val title = "Archive integration fixture"
        if (!device.wait(Until.hasObject(By.text(title)), 1500)) {
            nativeClick(By.desc(Pattern.compile("(?i)(show roots|open navigation drawer|show navigation drawer)")),
                "System folder picker roots")
        }
        nativeClick(By.text(title), "Fixture root in system picker")
    }

    private fun pickTree() {
        documents() // Start the independent fixture provider before the picker queries roots.
        openExport()
        click("Choose export folder")
        openFixtureRoot()
        nativeClick(By.text(Pattern.compile("(?i)(use this folder|select)")), "Use this export folder")
        clickIfPresent(By.text(Pattern.compile("(?i)allow")), 2_000)
        await("persisted fixture tree", 15_000) { app.exports.panel.value.phase == ExportPhaseUi.READY }
        val uri = app.getSharedPreferences("comicarchiver_export", 0).getString("tree_uri", null)
        assertEquals("io.github.bcrute.comicarchiver.test.documents", Uri.parse(requireNotNull(uri)).authority)
        assertTrue(app.contentResolver.persistedUriPermissions.any { it.uri.toString() == uri && it.isReadPermission && it.isWritePermission })
    }

    /** Real system picker -> read-only capture -> JNI/shared verifier -> held root switch, with test-owned data. */
    private fun verifyWorkingLibraryChange(): JSONObject {
        val original = app.workspace
        val originalExportPhase = app.exports.panel.value.phase
        val originalFiles = originals()
        val binding = JSONObject(app.fixture().callAttr("working_library_facts", original.root.absolutePath).toString())
        assertEquals(2, binding.getInt("binding_fields"))
        val exportTree = Uri.parse(requireNotNull(app.getSharedPreferences("comicarchiver_export", 0).getString("tree_uri", null)))
        val destination = DocumentsContract.buildDocumentUriUsingTree(exportTree, DocumentsContract.getTreeDocumentId(exportTree))
        val made = mutableListOf<Uri>()
        val savedSource = mutableListOf<Pair<Uri, String>>()
        fun directory(parent: Uri, name: String) = requireNotNull(DocumentsContract.createDocument(
            app.contentResolver, parent, Document.MIME_TYPE_DIR, name)).also { made.add(it) }
        fun file(parent: Uri, name: String, bytes: ByteArray) {
            check(bytes.size <= 16 * 1024 * 1024)
            val document = requireNotNull(DocumentsContract.createDocument(app.contentResolver, parent, "application/octet-stream", name))
            made.add(document)
            requireNotNull(app.contentResolver.openOutputStream(document, "wt")).use { it.write(bytes) }
            savedSource.add(document to hash(bytes))
        }
        var newId: String? = null
        var imported: JSONArray? = null
        try {
            val source = directory(destination, "Integration working library")
            val library = directory(source, "Library")
            for (series in original.library.listFiles().orEmpty().filter { it.isDirectory && !it.name.startsWith('.') }) {
                val folder = directory(library, series.name)
                for (item in series.listFiles().orEmpty().filter { it.isFile }) file(folder, item.name, item.readBytes())
            }
            val state = directory(library, ".archive-state")
            for (name in listOf("inventory.sqlite3", "inventory.sqlite3-wal", "inventory.sqlite3-shm", "inventory.sqlite3-journal")) {
                val input = original.library.resolve(".archive-state/$name")
                if (input.isFile) file(state, name, input.readBytes())
            }
            val provenance = directory(state, "provenance")
            for (input in original.library.resolve(".archive-state/provenance").listFiles().orEmpty()) file(provenance, input.name, input.readBytes())
            file(directory(source, ".comic-session"), "import-secret.json", "SOURCE_COOKIE_MUST_STAY_HERE".toByteArray())
            file(directory(source, ".comic-automation"), "source-queue.json", "SOURCE_QUEUE_MUST_NOT_RUN".toByteArray())
            openLibraryOptions()
            click("Change working library")
            await("working choices") { !app.workingLibraries.state.value.loading }
            click("Choose working library folder")
            openFixtureRoot()
            nativeClick(By.text("Integration working library"), "Integration working library")
            nativeClick(By.text(Pattern.compile("(?i)(use this folder|select)")), "Use this working library folder")
            clickIfPresent(By.text(Pattern.compile("(?i)allow")), 2_000)
            await("working source preview", 30_000) { app.workingLibraries.state.value.phase == WorkingLibraryPhase.PREVIEW }
            assertEquals(original.libraryId, app.workspace.libraryId)
            click("Copy, verify and use")
            await("verified working copy selected", 60_000) { app.workspace.libraryId != original.libraryId &&
                !app.controller.state.value.libraryBusy && app.workingLibraries.state.value.phase == WorkingLibraryPhase.IDLE }
            newId = app.workspace.libraryId
            val copiedArchives = originals()
            imported = copiedArchives
            fun digests(rows: JSONArray) = (0 until rows.length()).map { rows.getJSONObject(it).getString("sha256") }.sorted()
            assertEquals(digests(originalFiles), digests(copiedArchives))
            for (archive in archives()) {
                val uri = FileProvider.getUriForFile(app, app.packageName + ".archives", archive)
                assertEquals("managed-libraries", uri.pathSegments.first())
                assertEquals(newId, uri.pathSegments[1])
                assertEquals(hash(archive.readBytes()), requireNotNull(app.contentResolver.openInputStream(uri)).use { hash(it.readBytes()) })
            }
            val facts = JSONObject(app.fixture().callAttr("working_library_facts", app.workspace.root.absolutePath).toString())
            assertEquals(binding.getString("binding_digest"), facts.getString("binding_digest"))
            assertEquals(2, facts.getInt("binding_fields")); assertEquals(2, facts.getInt("archived")); assertEquals(0, facts.getInt("not_archived"))
            assertFalse(app.workspace.root.resolve(".comic-session/import-secret.json").exists())
            assertFalse(app.workspace.root.resolve(".comic-automation/source-queue.json").exists())
            assertEquals(ExportPhaseUi.NOT_PICKED, app.exports.panel.value.phase)
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            // Read-only adoption excludes source session records. Restore needs
            // such a record; the existing browser cookie alone is not authority.
            // Verify the preserved binding through a new explicit UI sign-in.
            await("adopted library starts without a saved sign-in", 20_000) {
                app.controller.state.value.signIn == SignInState.SignedOut
            }
            assertEquals(0, observe().getInt("asset_requests"))
            click("Downloads")
            click("Sign in to Tapas")
            await("account verified for adopted binding", 30_000) {
                // The existing profile may verify immediately after explicit
                // entry, or present the site's normal sign-in button first.
                if (app.controller.state.value.signIn !is SignInState.SignedIn)
                    clickIfPresent(By.text("Sign in to fixture"), 500)
                app.controller.state.value.signIn is SignInState.SignedIn
            }
            val verifiedBinding = JSONObject(app.fixture().callAttr("working_library_facts", app.workspace.root.absolutePath).toString())
            assertEquals(binding.getString("binding_digest"), verifiedBinding.getString("binding_digest"))
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            assertEquals(0, observe().getInt("asset_requests"))
            assertEquals(originalFiles.toString(), originals(original.root).toString())
            savedSource.forEach { (uri, expected) ->
                val observed = requireNotNull(app.contentResolver.openInputStream(uri)).use { hash(it.readBytes()) }
                assertEquals(expected, observed)
            }
            openSetting("Working libraries")
            click("Use this working library") // Only the retained original is not current.
            await("original working library restored") { app.workspace.libraryId == original.libraryId && !app.controller.state.value.libraryBusy }
            await("original account restored without a new sign-in", 30_000) {
                app.controller.state.value.signIn is SignInState.SignedIn
            }
            assertEquals(originalFiles.toString(), originals().toString())
            assertEquals(originalExportPhase, app.exports.panel.value.phase)
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            assertEquals(0, observe().getInt("asset_requests"))
        } finally {
            // Explicit test teardown through the original writable fixture grant, after source-retention checks.
            made.asReversed().forEach { assertTrue(DocumentsContract.deleteDocument(app.contentResolver, it)) }
        }
        assertNotNull(newId); assertNotNull(imported)
        return JSONObject().put("working_library_adopted", true).put("library_binding_preserved", true)
            .put("library_originals_preserved", true).put("library_switch_held", true).put("managed_archive_grants", true)
            .put("adopted_library_id", newId).put("adopted_archives", imported)
    }

    /** Focused native discovery diagnostic; supplies no real-provider account evidence. */
    @Test fun nativeDiscoveryFollowsTheActualProvidedPagerAndFreeFilter() {
        FixtureSafety.requireEmulator()
        app.fixture().callAttr("enable_native_discovery_diagnostic")
        assertTrue("Required native WebView execution/profile features are unavailable", app.readingBrowser.available())
        ActivityScenario.launch(MainActivity::class.java).use {
            try {
                await("cold fixture startup") { !app.controller.state.value.siteBusy &&
                    app.controller.state.value.signIn == SignInState.SignedOut }
                click("Sign in to Tapas")
                nativeClick(By.text("Sign in to fixture"), "Fixture sign-in", 20_000)
                await("verified native fixture sign-in") { app.controller.state.value.signIn is SignInState.SignedIn }
                startSaving()
                await("native list observations", 150_000) {
                    val rows = observe().getJSONArray("native_library_diagnostics")
                    rows.length() >= 4 || (0 until rows.length()).any { !rows.getJSONObject(it).optBoolean("complete") }
                }
                val observations = observe().getJSONArray("native_library_diagnostics")
                for (category in listOf("subscribed", "updated", "recent", "free_episodes")) {
                    val rows = (0 until observations.length()).map { observations.getJSONObject(it) }
                        .filter { it.optString("category") == category }
                    assertEquals("Expected one actual $category result", 1, rows.size)
                    assertTrue("Incomplete native $category observation: ${rows.single()}",
                        rows.single().optBoolean("complete"))
                    assertEquals("Actual $category must navigate both supplied pages", 2,
                        rows.single().getJSONArray("pages").length())
                }
            } finally {
                save("native-discovery-diagnostic.json", observe())
                InstrumentationRegistry.getInstrumentation().runOnMainSync { app.controller.pause() }
                app.fixture().callAttr("release_transfer")
            }
        }
    }

    @Test fun prepareTransferAndWaitForHostStop() {
        FixtureSafety.requireEmulator()
        // The existing native gate executes this required browser boundary too;
        // merely compiling BrowserSurfaceTest is not isolation/viewport evidence.
        BrowserSurfaceTest().readingWindowHasAViewportAndPageScriptsCannotReachItsCommandWorld()
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
          try {
            stage("cold_start")
            await("cold signed-out startup") { !app.controller.state.value.siteBusy &&
                app.controller.state.value.signIn == SignInState.SignedOut }
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            assertEquals(0, observe().getInt("asset_requests"))
            click("Sign in to Tapas")
            nativeClick(By.text("Sign in to fixture"), "Fixture sign-in", 20_000)
            await("verified native sign-in") { app.controller.state.value.signIn is SignInState.SignedIn }
            assertEquals(0, observe().getInt("asset_requests"))
            assertEquals(0, app.readingSurfaceCreations.get())
            stage("signed_in")
            allowFixtureConnectionIfHeld()
            startSaving()
            await("real asset transfer blocked", terminal = { app.controller.state.value.download is DownloadState.Failed }) {
                observe().getBoolean("blocked_transfer")
            }
            assertTrue(app.controller.state.value.download is DownloadState.Running ||
                app.controller.state.value.download is DownloadState.Planning)
            stage("transfer_blocked")
            val controller = app.controller
            recreateObserved()
            assertSame(controller, app.controller)
            if (!observe().getBoolean("blocked_transfer")) failure("Transfer did not remain blocked across recreation")
            click("Pause")
            await("production Pause cancellation", 10_000) {
                app.fixture().callAttr("pause_requested", app.workspace.root.absolutePath).toBoolean()
            }
            app.fixture().callAttr("release_transfer")
            await("paused import settles", 20_000) {
                val state = JSONObject(app.bridge.dispatch("worker_status", JSONObject()
                    .put("session_root", app.workspace.session.absolutePath).toString()))
                state.optJSONObject("result")?.optString("state") == "ready"
            }
            assertTrue(archives().none { it.extension == "cbz" })
            stage("paused")
            openSetting("Download preferences")
            click("Fast")
            await("stored native Fast choice") { app.controller.state.value.settings?.speed == "fast" &&
                app.controller.state.value.settings?.speeds?.any { it.key == "fast" && it.available } == true }
            click("Downloads")
            stage("acquiring_archives")
            startSaving()
            await("complete comic and novel catalog records", 180_000,
                terminal = { app.controller.state.value.download is DownloadState.Failed }) { exportableCount() == 2 }
            await("real Recent/Free browser pagination and native Fast engine", 90_000) {
                val facts = observe()
                facts.getJSONArray("recent_pages").length() >= 2 && facts.getJSONArray("free_pages").length() >= 2 &&
                    facts.getInt("fast_asset_clients") > 0
            }
            // Pause leaves the library ready for the separate export journey.
            clickIfPresent(By.text("Pause"), 2_000)
            verifyOutputs()
            click("Library")
            stage("choosing_export_tree")
            pickTree()
            stage("export_tree_granted")
            documents("arm")
            val before = originals()
            click("Export all")
            await("real SAF copy blocked", 20_000) { documents().getBoolean("blocked") }
            val partial = documents()
            val original = archives().single { it.extension == "cbz" }
            assertTrue(partial.getLong("partial_bytes") in 1L until original.length())
            assertEquals(ExportPhaseUi.RUNNING, app.exports.panel.value.phase)
            assertTrue(journal().all().any { it.state == JournalState.COPYING })
            assertTrue(device.takeScreenshot(evidence.resolve("copy-blocked.png")))
            save("ready.json", JSONObject().put("phase", "copy_blocked").put("originals", before)
                .put("expected_cbz_bytes", original.length()).put("partial_bytes", partial.getLong("partial_bytes"))
                .put("document_id", partial.getString("document_id")).put("native_pause_verified", true)
                .put("native_recreation_verified", true).put("comic_and_novel_verified", true)
                .put("native_fast_verified", true).put("native_discovery_verified", true))
            // The fleet coordinator independently verifies the live partial/journal,
            // then kills this process. Returning normally is a failed interruption gate.
            stage("copy_blocked")
            SystemClock.sleep(150_000)
            fail("The host did not stop the app while the export was in flight.")
          } catch (error: Throwable) {
            if (!evidence.resolve("timeout.json").exists()) runCatching { captureFailure(error.toString()) }
            throw error
          }
        }
    }

    @Test fun recoverAfterHostStopWithoutGo() {
        FixtureSafety.requireEmulator()
        val before = JSONObject(evidence.resolve("ready.json").readText())
        assertEquals(FixtureSafety.runToken, before.getString("run_token"))
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            await("restored native sign-in") { app.controller.state.value.signIn is SignInState.SignedIn &&
                !app.controller.state.value.siteBusy }
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            assertEquals(0, app.readingSurfaceCreations.get())
            assertEquals(0, observe().getInt("asset_requests"))
            assertTrue(journal().all().any { row -> row.state == JournalState.COPYING })
            documents("abandon") // Close the old provider pipe before opening the same document again.
            sameOriginals(before.getJSONArray("originals"))
            click("Library")
            openExport()
            click("Export all")
            await("verified SAF recovery", 30_000) { app.exports.panel.value.phase == ExportPhaseUi.ALL_VERIFIED }
            val expected = before.getJSONArray("originals")
            val target = JSONArray(documents().getString("files"))
            assertEquals(2, target.length())
            for (index in 0 until expected.length()) {
                val source = expected.getJSONObject(index)
                assertEquals(1, (0 until target.length()).count { at ->
                    val copy = target.getJSONObject(at)
                    copy.getLong("bytes") == source.getLong("bytes") && copy.getString("sha256") == source.getString("sha256")
                })
            }
            assertTrue((0 until target.length()).any { index ->
                target.getJSONObject(index).getString("id") == before.getString("document_id")
            })
            val completed = journal().all()
            assertEquals(2, completed.size)
            assertTrue(completed.all { row -> row.state == JournalState.VERIFIED })
            sameOriginals(expected)
            assertEquals(0, observe().getInt("asset_requests"))
            assertEquals(DownloadState.Held, app.controller.state.value.download)
            assertTrue(device.takeScreenshot(evidence.resolve("recovered.png")))
            val libraryProof = verifyWorkingLibraryChange()
            save("result.json", JSONObject().put("phase", "recovered").put("held_without_go", true)
                .put("originals_unchanged", true).put("same_partial_document_recovered", true)
                .put("verified_exports", 2).put("comic_and_novel_verified", true).apply {
                    libraryProof.keys().forEach { key -> put(key, libraryProof.get(key)) }
                })
        }
    }
}
