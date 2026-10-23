package io.github.bcrute.comicarchiver.export

import android.app.Application
import android.content.Context
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.ui.ExportPhaseUi
import io.github.bcrute.comicarchiver.ui.PublicIssue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.security.MessageDigest
import org.json.JSONObject

/** Real preferences and coordinator; a synthetic bridge prevents account/filesystem
 * exports. Grant persistence and actual provider copies still need device coverage. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class SafExportRuntimeTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val context get() = RuntimeEnvironment.getApplication()
    private val calls = AtomicInteger()

    @Before fun resetPreferences() {
        listOf("comicarchiver_export", "comicarchiver_export_webtoon", "comicarchiver_export_tappytoon").forEach {
            context.getSharedPreferences(it, Context.MODE_PRIVATE).edit().clear().commit()
        }
    }

    @After fun stopScope() { scope.cancel() }

    @Test fun separateWorkingLibrariesKeepTheirOwnDestinationJournalAndInactiveRuntimeCannotStart() {
        val documents = FakeDocuments()
        val ids = listOf("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        val manifests = AtomicInteger()
        val journals = mutableMapOf<String, ByteArray>()
        for (id in ids) {
            val tree = "content://fixture.documents/tree/$id"
            context.getSharedPreferences("comicarchiver_export_${id}_tapas", Context.MODE_PRIVATE).edit().putString("tree_uri", tree).commit()
            val root = context.filesDir.resolve("working-libraries/$id")
            val file = root.resolve("Library/same/chapter.cbz").apply { parentFile!!.mkdirs(); writeText("$id public fixture") }
            val bytes = file.readBytes()
            val hash = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            val bridge = object : PythonBridge {
                override fun dispatch(op: String, argsJson: String): String {
                    manifests.incrementAndGet()
                    assertEquals(root.absolutePath, JSONObject(argsJson).getString("root"))
                    return """{"ok":true,"result":{"entries":[{"seriesId":"same","episodeId":"same","kind":"CBZ",
                        "filename":"chapter.cbz","bytes":${bytes.size},"sha256":"$hash","relpath":"same/chapter.cbz"}]}}"""
                }
            }
            val runtime = SafExportRuntime(context, bridge, root, scope, documents, libraryId = id)
            runtime.startExport()
            runBlocking { withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.ALL_VERIFIED } } }
            val path = context.filesDir.resolve("export/libraries/$id/tapas/journal-v1.txt")
            val record = FileJournalStore(path).load().all().single()
            assertEquals(tree, record.targetTreeUri); assertEquals(hash, record.sha256)
            journals[id] = path.readBytes()
            val inactive = SafExportRuntime(context, bridge, root, scope, documents, libraryId = id, canStart = { false })
            val before = manifests.get(); inactive.startExport(); assertEquals(before, manifests.get())
        }
        assertEquals(2, manifests.get())
        journals.forEach { (id, bytes) -> assertTrue(bytes.contentEquals(context.filesDir.resolve("export/libraries/$id/tapas/journal-v1.txt").readBytes())) }
        assertTrue(context.getSharedPreferences("comicarchiver_export", Context.MODE_PRIVATE).getString("tree_uri", null) == null)
    }

    private fun rememberedFolder() {
        context.getSharedPreferences("comicarchiver_export", Context.MODE_PRIVATE).edit()
            .putString("tree_uri", "content://example.documents/tree/test-library").commit()
    }

    private fun runtime(manifest: String) = SafExportRuntime(
        context, object : PythonBridge {
            override fun dispatch(op: String, argsJson: String): String {
                assertEquals("export_manifest", op)
                calls.incrementAndGet()
                return manifest
            }
        }, context.filesDir.resolve("test-workspace"), scope,
    )

    @Test fun rememberedFolderIsUsableAfterReconstructionWithoutPickingAgain() {
        rememberedFolder()
        val runtime = runtime("""{"ok":true,"result":{"entries":[]}}""")
        assertEquals(ExportPhaseUi.READY, runtime.panel.value.phase)
        runtime.startExport()
        assertNotEquals(ExportPhaseUi.NOT_PICKED, runtime.panel.value.phase)
        runBlocking { withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.READY } } }
        assertEquals(1, calls.get())
    }

    @Test fun missingFolderAsksForPickerAndDoesNotReadTheManifest() {
        val runtime = runtime("""{"ok":true,"result":[]}""")
        runtime.startExport()
        assertEquals(ExportPhaseUi.NOT_PICKED, runtime.panel.value.phase)
        assertEquals(0, calls.get())
    }

    @Test fun invalidManifestCannotBecomeAllFilesExported() {
        rememberedFolder()
        val runtime = runtime("""{"ok":true,"result":{"entries":[{"kind":"CBZ"}]}}""")
        runtime.startExport()
        val result = runBlocking {
            withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.FINISHED_WITH_FAILURES } }
        }
        assertEquals(PublicIssue.JOB_FAILED, result.lastIssue)
        assertEquals(1, calls.get())
    }

    @Test fun repeatedStartCannotAdmitAnotherRunAndStopDuringManifestIsRetained() {
        rememberedFolder()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val runtime = SafExportRuntime(context, object : PythonBridge {
            override fun dispatch(op: String, argsJson: String): String {
                calls.incrementAndGet(); entered.countDown()
                check(release.await(5, TimeUnit.SECONDS))
                return """{"ok":true,"result":{"entries":[]}}"""
            }
        }, context.filesDir.resolve("test-workspace"), scope, FakeDocuments())
        runtime.startExport()
        try {
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            runtime.startExport()
            assertEquals(1, calls.get())
            runtime.stopExport()
        } finally { release.countDown() }
        val result = runBlocking {
            withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.INTERRUPTED } }
        }
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, result.lastIssue)
        assertEquals(1, calls.get())
    }

    @Test fun unexpectedProviderFailureLeavesAnActionableErrorInsteadOfRunningForever() {
        rememberedFolder()
        val documents = object : DocumentsAdapter by FakeDocuments() {
            override fun rootFolder(target: SafExportTarget): DocumentsFolder =
                throw IllegalStateException("synthetic provider failure")
        }
        val manifest = """{"ok":true,"result":{"entries":[{"seriesId":"s","episodeId":"e",
            "kind":"CBZ","filename":"one.cbz","bytes":1,"sha256":"${"a".repeat(64)}","relpath":"s/one.cbz"}]}}"""
        val runtime = SafExportRuntime(context, object : PythonBridge {
            override fun dispatch(op: String, argsJson: String) = manifest
        }, context.filesDir.resolve("test-workspace"), scope, documents)
        runtime.startExport()
        val result = runBlocking {
            withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.FINISHED_WITH_FAILURES } }
        }
        assertEquals(PublicIssue.JOB_FAILED, result.lastIssue)
    }

    @Test fun providerFoldersAndJournalsStaySeparateAcrossReconstructionAndIdCollisions() {
        val documents = FakeDocuments()
        val journalBytes = mutableMapOf<String, ByteArray>()
        listOf("tapas", "webtoon", "tappytoon").forEach { site ->
            val preferences = if (site == "tapas") "comicarchiver_export" else "comicarchiver_export_$site"
            context.getSharedPreferences(preferences, Context.MODE_PRIVATE).edit()
                .putString("tree_uri", "content://fixture.documents/tree/$site").commit()
            val root = context.filesDir.resolve("namespace-fixture/$site")
            val original = root.resolve("Library/same/chapter.cbz").apply {
                parentFile!!.mkdirs(); writeText("$site fixture archive")
            }
            val bytes = original.readBytes()
            val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            val manifest = """{"ok":true,"result":{"entries":[{"seriesId":"same","episodeId":"same",
                "kind":"CBZ","filename":"chapter.cbz","bytes":${bytes.size},"sha256":"$digest","relpath":"same/chapter.cbz"}]}}"""
            val bridge = object : PythonBridge {
                override fun dispatch(op: String, argsJson: String): String {
                    assertEquals("export_manifest", op)
                    assertEquals(root.absolutePath, JSONObject(argsJson).getString("root"))
                    return manifest
                }
            }
            val runtime = SafExportRuntime(context, bridge, root, scope, documents, siteKey = site)
            runtime.startExport()
            runBlocking { withTimeout(5000) { runtime.panel.first { it.phase == ExportPhaseUi.ALL_VERIFIED } } }
            val file = context.filesDir.resolve(if (site == "tapas") "export/journal-v1.txt" else "export/$site/journal-v1.txt")
            val entry = FileJournalStore(file).load().all().single()
            assertEquals(JournalState.VERIFIED, entry.state)
            assertEquals("content://fixture.documents/tree/$site", entry.targetTreeUri)
            assertEquals(digest, entry.sha256)
            assertTrue(documents.belongsToTree(DocumentsFile(entry.targetUri!!), SafExportTarget(entry.targetTreeUri!!)))
            assertTrue(bytes.contentEquals(documents.docs.getValue(entry.targetUri!!).bytes!!))
            journalBytes[site] = file.readBytes()
            val before = documents.createdNames.size
            val restored = SafExportRuntime(context, bridge, root, scope, documents, siteKey = site)
            restored.startExport()
            runBlocking { withTimeout(5000) { restored.panel.first { it.phase == ExportPhaseUi.ALL_VERIFIED } } }
            assertEquals(before, documents.createdNames.size)
        }
        journalBytes.forEach { (site, expected) ->
            val path = if (site == "tapas") "export/journal-v1.txt" else "export/$site/journal-v1.txt"
            assertTrue(expected.contentEquals(context.filesDir.resolve(path).readBytes()))
        }
    }
}
