package io.github.bcrute.comicarchiver.signin

import android.app.Application
import android.os.Looper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.time.Duration
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Completion is evidence from the selected browser profile, never a posted request
 * or another site's default cookie jar. Actual WebView isolation remains a device gate. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class ProfileClearCompletionTest {
    @Test
    fun blockingClearWaitsForTheProfilesActualCompletion() {
        val release = AtomicReference<((Boolean) -> Unit)?>(null)
        val store = WebViewCookieStore(setOf("www.webtoons.com"), timeoutMs = 5_000,
            managerProvider = { error("The default cookie manager must not be touched") },
            clearProfile = { release.set(it) })
        val executor = Executors.newSingleThreadExecutor()
        try {
            val result = executor.submit<Boolean> { store.clearAll() }
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3)
            while (release.get() == null && System.nanoTime() < deadline) {
                shadowOf(Looper.getMainLooper()).idle()
                Thread.sleep(1)
            }
            assertNotNull(release.get())
            assertFalse("Posting deletion is not completed deletion", result.isDone)
            release.get()!!.invoke(true)
            shadowOf(Looper.getMainLooper()).idle()
            assertTrue(result.get(5, TimeUnit.SECONDS))
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun aTimedOutProfileCannotLaterReportSuccessfulSignOut() {
        var release: ((Boolean) -> Unit)? = null
        val answers = mutableListOf<Boolean>()
        val store = WebViewCookieStore(setOf("www.tappytoon.com"), timeoutMs = 1_000,
            managerProvider = { error("The default cookie manager must not be touched") },
            clearProfile = { release = it })
        store.clearAllAsync { answers.add(it) }
        shadowOf(Looper.getMainLooper()).idle()
        assertNotNull(release)
        assertTrue(answers.isEmpty())
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1_001))
        assertEquals(listOf(false), answers)
        release!!.invoke(true)
        shadowOf(Looper.getMainLooper()).idle()
        assertEquals(listOf(false), answers)
    }
}
