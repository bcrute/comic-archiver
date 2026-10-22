package io.github.bcrute.comicarchiver.signin

import android.app.Application
import android.os.Looper
import android.webkit.WebView
import io.github.bcrute.comicarchiver.controller.SignInState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class SessionRestoreTest {
    private fun surface() = WebView(RuntimeEnvironment.getApplication()).apply {
        settings.userAgentString = "test-browser-agent"
    }
    private fun engine(onRestore: () -> Unit = {}) = WebViewSignInEngine(
        WebViewCookieStore(setOf("m.tapas.io")), onRestore = onRestore,
    ).also { it.load("tapas") }

    @Test fun restoreNeedsVerifiedAnswerAndRunsOnlyOnce() {
        var restores = 0
        val engine = engine { restores++ }
        val view = surface()
        engine.restoreSignIn(view)
        assertEquals(SignInState.Restoring, engine.state.value)
        val session = engine.currentSession()
        engine.verified(session)
        assertEquals(SignInState.SignedIn("tapas"), engine.state.value)
        engine.restoreSignIn(view)
        assertEquals(1, restores)
        view.destroy()
    }

    @Test fun expiredRestoreReturnsToEntryAndLateAnswerCannotAuthenticateInteractiveAttempt() {
        val engine = engine()
        val view = surface()
        engine.restoreSignIn(view)
        val old = engine.currentSession()
        engine.restoreNotVerified(old)
        assertEquals(SignInState.SignedOut, engine.state.value)
        engine.startSignIn(view)
        engine.verified(old)
        assertTrue(engine.state.value is SignInState.Page)
        engine.cancelSignIn()
        view.destroy()
    }

    @Test fun cancelledOrTimedOutRestoreRejectsLateVerification() {
        val engine = engine()
        val view = surface()
        engine.restoreSignIn(view)
        val old = engine.currentSession()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(16))
        engine.verified(old)
        assertEquals(SignInState.SignedOut, engine.state.value)
        engine.startSignIn(view)
        val active = engine.currentSession()
        engine.cancelSignIn()
        engine.verified(active)
        assertTrue(engine.state.value is SignInState.Failed)
        view.destroy()
    }
}
