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

    private fun card(state: io.github.bcrute.comicarchiver.controller.AppState) = state.toDownloadsScreenUi().cards.single()

    @Test
    fun `the sign-in page takes the tab only while it is open`() {
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Page("https://example.invalid/")))
        assertEquals(RootRoute.SIGN_IN, routeFor(SignInState.Settling))
        for (signIn in listOf(SignInState.SignedIn("tapas"), SignInState.SignedOut, SignInState.Restoring,
            SignInState.Failed("login_required"))) assertEquals(RootRoute.APP, routeFor(signIn))
    }

    @Test
    fun `a signed-out site offers sign in on its own card, never a download`() {
        val signedOut = card(UiFixtures.coldStart)
        assertEquals(CardAction.SIGN_IN to "Sign in", signedOut.action to signedOut.actionText)
        assertTrue(signedOut.actionEnabled)
        assertFalse(signedOut.signOut)
        assertEquals("", signedOut.message)
        val refused = card(UiFixtures.coldStart.copy(signIn = SignInState.Failed("login_required")))
        assertEquals(CardAction.SIGN_IN, refused.action)
        assertEquals("Tapas signed you out" to true, refused.message to refused.messageError)
    }

    @Test
    fun `go is offered only while held or finished, never while running`() {
        val held = card(UiFixtures.heldAfterRestart)
        assertEquals(CardAction.GO to "Start", held.action to held.actionText)
        assertTrue(held.actionEnabled && held.signOut && held.signOutEnabled)

        val running = card(UiFixtures.running)
        assertEquals(CardAction.PAUSE to "Pause", running.action to running.actionText)
        assertTrue(running.actionEnabled)
        assertFalse(running.signOutEnabled)
    }

    @Test
    fun `planning offers pause but not go`() {
        assertEquals(CardAction.PAUSE, card(UiFixtures.running.copy(download = DownloadState.Planning)).action)
    }

    @Test
    fun `finished offers start again, paused offers continue`() {
        val finished = card(UiFixtures.running.copy(download = DownloadState.Finished("37 items saved")))
        assertEquals(CardAction.GO to "Start", finished.action to finished.actionText)
        val paused = card(UiFixtures.meteredHold)
        assertEquals(CardAction.GO to "Continue", paused.action to paused.actionText)
        assertTrue(paused.actionEnabled)
    }
}
