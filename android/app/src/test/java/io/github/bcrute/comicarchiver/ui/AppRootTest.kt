package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Focused tests for the root's state/action decisions — the mistakes that would matter
 * in an assembled app: offering downloads to someone who is not signed in,
 * offering Go while a transfer is already running, or offering Pause with nothing in
 * flight. Compose rendering itself is not tested here; it waits for the fleet build.
 */
class AppRootTest {

    @Test
    fun `only a verified sign-in reaches download controls`() {
        assertEquals(RootRoute.APP, routeFor(SignInState.SignedIn("tapas")))
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.SignedOut))
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Settling))
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Restoring))
        assertEquals(
            RootRoute.SIGN_IN,
            routeFor(SignInState.Page("https://example.invalid/")),
        )
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Failed("login_required")))
    }

    @Test
    fun `a failed sign-in stays on the sign-in screen, with its notice`() {
        val ui = UiFixtures.coldStart
            .copy(signIn = SignInState.Failed("login_required"))
            .toSignInScreenUi()
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Failed("login_required")))
        assertEquals(PublicIssue.LOGIN_REQUIRED, ui.notice?.issue)
    }

    @Test
    fun `go is offered only while held or finished, never while running`() {
        val held = UiFixtures.coldStart.toDownloadsScreenUi().download
        assertTrue(held.goEnabled())
        assertFalse(held.pauseEnabled())

        val running = UiFixtures.running.toDownloadsScreenUi().download
        assertFalse(running.goEnabled())
        assertTrue(running.pauseEnabled())
    }

    @Test
    fun `planning offers pause but not go`() {
        val planning = UiFixtures.running
            .copy(download = DownloadState.Planning)
            .toDownloadsScreenUi().download
        assertFalse(planning.goEnabled())
        assertTrue(planning.pauseEnabled())
    }

    @Test
    fun `finished offers go again, paused does too`() {
        val finished = UiFixtures.running
            .copy(download = DownloadState.Finished("37 items saved"))
            .toDownloadsScreenUi().download
        assertTrue(finished.goEnabled())
        assertFalse(finished.pauseEnabled())

        val paused = UiFixtures.meteredHold.toDownloadsScreenUi().download
        assertTrue(paused.goEnabled())
        assertFalse(paused.pauseEnabled())
    }
}
