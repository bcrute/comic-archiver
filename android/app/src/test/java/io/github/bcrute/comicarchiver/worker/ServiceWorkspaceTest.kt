package io.github.bcrute.comicarchiver.worker

import android.content.Intent
import io.github.bcrute.comicarchiver.ui.SwitchTestApp
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import io.github.bcrute.comicarchiver.runtime.WorkingLibrarySource
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryStart

/** Real service/notification routing, with a synthetic core. Robolectric does not
 * enforce the OS foreground-service budget or exercise a real Python worker drain. */
@RunWith(RobolectricTestRunner::class)
@Config(application = SwitchTestApp::class, sdk = [34])
class ServiceWorkspaceTest {
    private val app get() = RuntimeEnvironment.getApplication() as SwitchTestApp

    @Before @After fun nothingKeptAlive() = ActiveDownloads.clear()

    @Test fun twoSitesAreKeptAliveUnderOneNotificationAndItsStopReachesBoth() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get()
        fun start(id: Int) = service.onStartCommand(Intent(app, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START)
            .putExtra(DownloadHostService.EXTRA_WORKSPACE, app.workspace.root.absolutePath), 0, id)
        fun stopIntent(): Intent = shadowOf(shadowOf(service).lastForegroundNotification.actions.single().actionIntent).savedIntent
        try {
            val original = app.workspace
            start(1)
            app.selectWorkspace("webtoon")
            val selected = app.workspace
            start(2)
            // Showing another site keeps the first one alive beside it.
            assertEquals(listOf(original.root.absolutePath, selected.root.absolutePath), ActiveDownloads.all())
            // A folder that is no site's workspace here is never kept alive.
            service.onStartCommand(Intent(app, DownloadHostService::class.java)
                .setAction(DownloadHostService.ACTION_START)
                .putExtra(DownloadHostService.EXTRA_WORKSPACE, original.root.resolve("elsewhere").absolutePath), 0, 3)
            assertEquals(2, ActiveDownloads.all().size)
            service.onStartCommand(stopIntent(), 0, 4)
            val deadline = System.nanoTime() + 5_000_000_000L
            while (ActiveDownloads.all().isNotEmpty() && System.nanoTime() < deadline) Thread.sleep(10)
            assertTrue(ActiveDownloads.all().isEmpty())
            val roots = setOf(original, selected)
            assertEquals(roots.map { it.root.absolutePath }.toSet(),
                app.wire.requests.filter { it.first == "pause" }.map { it.second.getString("root") }.toSet())
            assertEquals(roots.map { it.session.absolutePath }.toSet(),
                app.wire.requests.filter { it.first == "worker_stop" }.map { it.second.getString("session_root") }.toSet())
            assertEquals(2, app.wire.count("pause")); assertEquals(2, app.wire.count("worker_stop"))
            // Every site is asked to pause before any is waited for.
            assertEquals(listOf("pause", "pause", "worker_stop", "worker_stop"), app.wire.requests.map { it.first })
        } finally { lifecycle.destroy() }
    }

    @Test fun aPreviousLibrarysNotificationCannotRetargetTheSameProvidersNewLibrary() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create(); val service = lifecycle.get()
        fun start(id: Int) = service.onStartCommand(Intent(app, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START).putExtra(DownloadHostService.EXTRA_WORKSPACE, app.workspace.root.absolutePath), 0, id)
        fun stopIntent() = shadowOf(shadowOf(service).lastForegroundNotification.actions.single().actionIntent).savedIntent
        try {
            start(1); val old = stopIntent()
            assertEquals("comicarchiver://downloads/default", old.data.toString())
            val locations = app.libraryLocations
            val pending = (locations.begin("Other copy", WorkingLibrarySource("a".repeat(64),
                "content://fixture.documents/tree/library")) as WorkingLibraryStart.Copy).pending
            locations.verified(pending.id, "b".repeat(64)); val ready = locations.complete(pending.id)
            locations.select(ready.id); app.selectWorkspace("tapas", ready.id); start(2)
            val current = stopIntent()
            assertNotEquals(old.data, current.data)
            assertEquals("default", old.getStringExtra(DownloadHostService.EXTRA_LIBRARY))
            assertEquals(ready.id, current.getStringExtra(DownloadHostService.EXTRA_LIBRARY))
            // Only the library now in use is kept alive, and the old notification cannot stop it.
            assertEquals(listOf(app.workspace.root.absolutePath), ActiveDownloads.all())
            service.onStartCommand(old, 0, 3)
            assertEquals(0, app.wire.count("pause")); assertEquals(0, app.wire.count("worker_stop"))
        } finally { lifecycle.destroy() }
    }

    @Test fun timeoutUsesABoundedDrainForItsCapturedWorkspace() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get()
        try {
            val selected = app.workspace
            service.onStartCommand(Intent(app, DownloadHostService::class.java)
                .setAction(DownloadHostService.ACTION_START)
                .putExtra(DownloadHostService.EXTRA_WORKSPACE, selected.root.absolutePath), 0, 1)
            service.onTimeout(1, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            val deadline = System.nanoTime() + 5_000_000_000L
            while (app.wire.count("worker_stop") == 0 && System.nanoTime() < deadline) Thread.sleep(10)
            val stop = app.wire.requests.single { it.first == "worker_stop" }.second
            assertEquals(selected.session.absolutePath, stop.getString("session_root"))
            assertEquals(2, stop.getInt("wait_seconds"))
            assertEquals(listOf("pause", "worker_stop"), app.wire.requests.map { it.first })
        } finally { lifecycle.destroy() }
    }
}
