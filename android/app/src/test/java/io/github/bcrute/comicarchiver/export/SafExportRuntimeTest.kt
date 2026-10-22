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
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.concurrent.atomic.AtomicInteger

/** Real preferences and coordinator; a synthetic bridge prevents account/filesystem
 * exports. Grant persistence and actual provider copies still need device coverage. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class SafExportRuntimeTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val context get() = RuntimeEnvironment.getApplication()
    private val calls = AtomicInteger()

    @Before fun resetPreferences() {
        context.getSharedPreferences("comicarchiver_export", Context.MODE_PRIVATE).edit().clear().commit()
    }

    @After fun stopScope() { scope.cancel() }

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
}
