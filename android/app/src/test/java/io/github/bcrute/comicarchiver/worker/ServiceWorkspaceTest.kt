package io.github.bcrute.comicarchiver.worker

import android.content.Intent
import io.github.bcrute.comicarchiver.ui.SwitchTestApp
import org.junit.Assert.*
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

    @Test fun aPreviousSitesNotificationCannotStopTheReplacementWorkspace() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get()
        fun start(id: Int) = service.onStartCommand(Intent(app, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START)
            .putExtra(DownloadHostService.EXTRA_WORKSPACE, app.workspace.root.absolutePath), 0, id)
        fun stopIntent(): Intent = shadowOf(shadowOf(service).lastForegroundNotification.actions.single().actionIntent).savedIntent
        try {
            val original = app.workspace
            start(1)
            val old = stopIntent()
            assertEquals(original.root.absolutePath, old.getStringExtra(DownloadHostService.EXTRA_WORKSPACE))
            app.selectWorkspace("webtoon")
            val selected = app.workspace
            start(2)
            val current = stopIntent()
            assertNotEquals(old.data, current.data)
            assertEquals(original.root.absolutePath, old.getStringExtra(DownloadHostService.EXTRA_WORKSPACE))
            assertEquals(selected.root.absolutePath, current.getStringExtra(DownloadHostService.EXTRA_WORKSPACE))
            service.onStartCommand(old, 0, 3)
            assertEquals(0, app.wire.count("pause"))
            assertEquals(0, app.wire.count("worker_stop"))
            service.onStartCommand(current, 0, 4)
            val deadline = System.nanoTime() + 5_000_000_000L
            while (app.wire.count("worker_stop") == 0 && System.nanoTime() < deadline) Thread.sleep(10)
            val pause = app.wire.requests.single { it.first == "pause" }.second
            val stop = app.wire.requests.single { it.first == "worker_stop" }.second
            assertEquals(selected.root.absolutePath, pause.getString("root"))
            assertEquals(selected.session.absolutePath, stop.getString("session_root"))
        } finally { lifecycle.destroy() }
    }

    @Test fun aPreviousLibrarysNotificationCannotRetargetTheSameProvidersNewLibrary() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create(); val service = lifecycle.get()
        fun start(id: Int) = service.onStartCommand(Intent(app, DownloadHostService::class.java)
            .setAction(DownloadHostService.ACTION_START).putExtra(DownloadHostService.EXTRA_WORKSPACE, app.workspace.root.absolutePath), 0, id)
        fun stopIntent() = shadowOf(shadowOf(service).lastForegroundNotification.actions.single().actionIntent).savedIntent
        try {
            start(1); val old = stopIntent(); val oldRoot = app.workspace.root
            assertEquals("comicarchiver://downloads/tapas", old.data.toString())
            val locations = app.libraryLocations
            val pending = (locations.begin("Other copy", WorkingLibrarySource("a".repeat(64),
                "content://fixture.documents/tree/library")) as WorkingLibraryStart.Copy).pending
            locations.verified(pending.id, "b".repeat(64)); val ready = locations.complete(pending.id)
            locations.select(ready.id); app.selectWorkspace("tapas", ready.id); start(2)
            val current = stopIntent()
            assertNotEquals(old.data, current.data)
            assertEquals(oldRoot.absolutePath, old.getStringExtra(DownloadHostService.EXTRA_WORKSPACE))
            assertEquals(app.workspace.root.absolutePath, current.getStringExtra(DownloadHostService.EXTRA_WORKSPACE))
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
