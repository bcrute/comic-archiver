package io.github.bcrute.comicarchiver.worker

import android.content.Intent
import android.os.Looper
import io.github.bcrute.comicarchiver.MainActivity
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
import java.time.Duration
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch

/** Real service/notification routing, with a synthetic core. Robolectric does not
 * enforce the OS foreground-service budget or exercise a real Python worker drain. */
@RunWith(RobolectricTestRunner::class)
@Config(application = SwitchTestApp::class, sdk = [34])
class ServiceWorkspaceTest {
    private val app get() = RuntimeEnvironment.getApplication() as SwitchTestApp

    @Before @After fun nothingKeptAlive() = ActiveDownloads.clear()

    /** Stands in for the phone's wake lock, which only a phone can show keeping the processor awake. */
    private class FakeCpu : CpuHold {
        @Volatile var held = false
        val holds = CopyOnWriteArrayList<Long>()
        override fun acquire(timeoutMillis: Long) { holds.add(timeoutMillis); held = true }
        override fun release() { held = false }
    }

    private fun startIntent(root: String) = Intent(app, DownloadHostService::class.java)
        .setAction(DownloadHostService.ACTION_START).putExtra(DownloadHostService.EXTRA_WORKSPACE, root)

    @Test fun theServiceOutlastsASiteWhoseRunEndedWhileAnotherStillSavesAndEndsWithTheLast() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); val cpu = FakeCpu(); service.cpu = cpu
        try {
            val first = app.workspace.root.absolutePath
            service.onStartCommand(startIntent(first), 0, 1)
            app.selectWorkspace("webtoon")
            val second = app.workspace.root.absolutePath
            service.onStartCommand(startIntent(second), 0, 2)
            DownloadHostService.ended(first)
            assertEquals(listOf(second), ActiveDownloads.all())
            assertFalse(shadowOf(service).isStoppedBySelf); assertFalse(shadowOf(service).isForegroundStopped)
            assertNotNull(shadowOf(service).lastForegroundNotification)
            assertTrue(cpu.held)
            // A site that is no longer kept alive ending once more changes nothing.
            DownloadHostService.ended(first)
            assertFalse(shadowOf(service).isStoppedBySelf)
            DownloadHostService.ended(second)
            assertTrue(ActiveDownloads.all().isEmpty())
            assertTrue(shadowOf(service).isStoppedBySelf); assertTrue(shadowOf(service).isForegroundStopped)
            assertFalse(cpu.held)
            // Nothing was paused or drained: the runs had ended by themselves. Go starts the service as before.
            assertEquals(0, app.wire.count("pause")); assertEquals(0, app.wire.count("worker_stop"))
            service.onStartCommand(startIntent(second), 0, 3)
            assertEquals(listOf(second), ActiveDownloads.all())
            assertNotNull(shadowOf(service).lastForegroundNotification)
            assertTrue(cpu.held)
        } finally { lifecycle.destroy() }
    }

    @Test fun theProcessorIsHeldAwakeOnlyWhileASiteIsKeptAliveAndEveryHoldRunsOutByItself() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); val cpu = FakeCpu(); service.cpu = cpu
        fun stopIntent(): Intent = shadowOf(shadowOf(service).lastForegroundNotification.actions.single().actionIntent).savedIntent
        var destroyed = false
        try {
            val root = app.workspace.root.absolutePath
            assertFalse(cpu.held)
            service.onStartCommand(startIntent(root), 0, 1)
            assertTrue(cpu.held)
            // While the site is kept alive the hold is taken again before it runs out.
            assertTrue(DownloadHostService.RENEW_MILLIS < DownloadHostService.HOLD_MILLIS)
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(DownloadHostService.RENEW_MILLIS))
            assertEquals(listOf(DownloadHostService.HOLD_MILLIS, DownloadHostService.HOLD_MILLIS), cpu.holds.toList())
            // Stop lets go once its drain is over, and nothing takes the hold again afterwards.
            service.onStartCommand(stopIntent(), 0, 2)
            val deadline = System.nanoTime() + 5_000_000_000L
            while (cpu.held && System.nanoTime() < deadline) Thread.sleep(10)
            assertFalse(cpu.held)
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(DownloadHostService.RENEW_MILLIS * 2))
            assertEquals(2, cpu.holds.size); assertFalse(cpu.held)
            // A service that goes away for any other reason lets go as well.
            service.onStartCommand(startIntent(root), 0, 3)
            assertTrue(cpu.held)
            lifecycle.destroy(); destroyed = true
            assertFalse(cpu.held)
        } finally { if (!destroyed) lifecycle.destroy() }
    }

    @Test fun theProcessorIsLetGoWhileEverySiteOnlyWaitsForAConnectionAndTheNotificationStays() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); val cpu = FakeCpu(); val grace = FakeCpu(); service.cpu = cpu; service.grace = grace
        try {
            val first = app.workspace.root.absolutePath
            service.onStartCommand(startIntent(first), 0, 1)
            app.selectWorkspace("webtoon")
            val second = app.workspace.root.absolutePath
            service.onStartCommand(startIntent(second), 0, 2)
            // One site still working keeps the phone awake for both.
            DownloadHostService.waiting(first, true)
            assertTrue(cpu.held); assertTrue(grace.holds.isEmpty())
            DownloadHostService.waiting(second, true)
            assertFalse(cpu.held)
            // The next readings still arrive: the phone is given a moment before it may sleep.
            assertEquals(listOf(DownloadHostService.GRACE_MILLIS), grace.holds.toList())
            assertEquals(listOf(first, second), ActiveDownloads.all())
            assertFalse(shadowOf(service).isStoppedBySelf); assertFalse(shadowOf(service).isForegroundStopped)
            assertNotNull(shadowOf(service).lastForegroundNotification)
            // Nothing takes the hold again while they wait, however long that is.
            val held = cpu.holds.size
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(DownloadHostService.RENEW_MILLIS * 3))
            assertEquals(held, cpu.holds.size); assertFalse(cpu.held)
            // The same reading again changes nothing; a site that works again is held awake at once.
            DownloadHostService.waiting(second, true)
            assertFalse(cpu.held); assertEquals(1, grace.holds.size)
            DownloadHostService.waiting(second, false)
            assertTrue(cpu.held)
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(DownloadHostService.RENEW_MILLIS * 2))
            assertTrue(cpu.holds.size > held + 1)
            // A site that ends while the other waits leaves nothing to stay awake for.
            DownloadHostService.ended(second)
            assertFalse(cpu.held); assertEquals(listOf(first), ActiveDownloads.all())
            assertFalse(shadowOf(service).isStoppedBySelf)
            // Go on a waiting site holds the phone awake until it is read as waiting again.
            service.onStartCommand(startIntent(first), 0, 3)
            assertTrue(cpu.held)
        } finally { lifecycle.destroy() }
    }

    @Test fun aChangeOfConnectionHoldsThePhoneAwakeUntilTheWaitingSitesAreReadAgain() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); val cpu = FakeCpu(); val grace = FakeCpu(); service.cpu = cpu; service.grace = grace
        var destroyed = false
        try {
            // With nothing kept alive a change of connection holds nothing.
            DownloadHostService.connectionChanged()
            shadowOf(Looper.getMainLooper()).idle()
            assertFalse(cpu.held); assertTrue(grace.holds.isEmpty())
            val root = app.workspace.root.absolutePath
            service.onStartCommand(startIntent(root), 0, 1)
            DownloadHostService.waiting(root, true)
            assertFalse(cpu.held)
            val before = grace.holds.size
            // Told from the platform's own thread: the short hold is taken there, the rest on the main thread.
            val told = Thread { DownloadHostService.connectionChanged() }
            told.start(); told.join(5_000)
            assertEquals(before + 1, grace.holds.size)
            shadowOf(Looper.getMainLooper()).idle()
            assertTrue(cpu.held)
            // Still no connection it may use: the next reading lets the phone sleep again.
            DownloadHostService.waiting(root, true)
            assertFalse(cpu.held)
            lifecycle.destroy(); destroyed = true
            assertFalse(grace.held)
        } finally { if (!destroyed) lifecycle.destroy() }
    }

    @Test fun aTimeoutEndsTheServiceWithinItsBoundWhileADrainIsStillWaiting() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); val cpu = FakeCpu(); service.cpu = cpu
        val release = CountDownLatch(1)
        app.wire.holdWorkerStop = release
        try {
            service.onStartCommand(startIntent(app.workspace.root.absolutePath), 0, 1)
            service.onTimeout(1, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            val deadline = System.nanoTime() + 5_000_000_000L
            while (app.wire.count("worker_stop") == 0 && System.nanoTime() < deadline) Thread.sleep(10)
            assertEquals(1, app.wire.count("worker_stop"))
            // The drain is still waiting for its site, and the service with it.
            assertFalse(shadowOf(service).isStoppedBySelf); assertTrue(cpu.held)
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(4))
            assertTrue(shadowOf(service).isStoppedBySelf); assertTrue(shadowOf(service).isForegroundStopped)
            assertFalse(cpu.held)
        } finally {
            release.countDown()
            val deadline = System.nanoTime() + 5_000_000_000L
            while (ActiveDownloads.all().isNotEmpty() && System.nanoTime() < deadline) Thread.sleep(10)
            lifecycle.destroy()
        }
    }

    @Test fun tappingTheNotificationAsksForTheOpenScreenAsTheLauncherDoesAndTheManifestKeepsOneCopyOfIt() {
        val lifecycle = Robolectric.buildService(DownloadHostService::class.java).create()
        val service = lifecycle.get(); service.cpu = FakeCpu()
        try {
            service.onStartCommand(startIntent(app.workspace.root.absolutePath), 0, 1)
            val open = shadowOf(shadowOf(service).lastForegroundNotification.contentIntent).savedIntent
            assertEquals(Intent.ACTION_MAIN, open.action)
            assertEquals(setOf(Intent.CATEGORY_LAUNCHER), open.categories)
            assertEquals(MainActivity::class.java.name, open.component?.className)
            assertTrue((open.flags and Intent.FLAG_ACTIVITY_NEW_TASK) != 0)
            val packages = app.packageManager
            assertEquals(android.content.pm.ActivityInfo.LAUNCH_SINGLE_TOP, packages.getActivityInfo(
                android.content.ComponentName(app, MainActivity::class.java), 0).launchMode)
            assertTrue(android.Manifest.permission.WAKE_LOCK in packages.getPackageInfo(app.packageName,
                android.content.pm.PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty())
        } finally { lifecycle.destroy() }
    }

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
