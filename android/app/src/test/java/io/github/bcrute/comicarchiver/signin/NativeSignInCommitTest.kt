package io.github.bcrute.comicarchiver.signin

import android.app.Application
import android.os.Looper
import android.webkit.WebView
import io.github.bcrute.comicarchiver.controller.SignInState
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.time.Duration

/** Native attempt/commit ownership. Provider verification and profile isolation have
 * their own core/device gates; these tests never contact a site or persist cookies. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class NativeSignInCommitTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private fun store() = WebViewCookieStore(setOf("m.tapas.io"))

    @Test fun aCancelledOrExpiredAnswerNeverReachesTheCommitHook() {
        val committed = mutableListOf<Long>()
        val engine = WebViewSignInEngine(store(), timeoutMs = 1_000,
            onVerified = { _, token -> committed.add(token); true })
        val surface = WebView(context)
        engine.load("tapas")
        engine.startSignIn(surface)
        val cancelled = engine.currentSession()
        engine.cancelSignIn()
        engine.verified(cancelled)
        engine.startSignIn(surface)
        val expired = engine.currentSession()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1_001))
        engine.verified(expired)
        assertTrue(committed.isEmpty())
        assertTrue(engine.state.value is SignInState.Failed)
        surface.destroy()
    }

    @Test fun onlyTheCurrentSuccessfulVerificationCanCommitOnce() {
        val committed = mutableListOf<Pair<String, Long>>()
        val attempts = mutableListOf<Pair<Long, Boolean>>()
        val engine = WebViewSignInEngine(store(),
            onAttempt = { _, token, _, active -> attempts.add(token to active) },
            onVerified = { key, token -> committed.add(key to token); true })
        val surface = WebView(context)
        engine.load("tapas")
        engine.startSignIn(surface)
        val token = engine.currentSession()
        assertTrue(committed.isEmpty())
        engine.verified(token)
        engine.verified(token)
        assertEquals(listOf("tapas" to token), committed)
        assertEquals(listOf(token to true, token to false), attempts)
        assertEquals(SignInState.SignedIn("tapas"), engine.state.value)
        surface.destroy()
    }

    @Test fun failedCoreCommitNeverAuthenticatesTheNativeUi() {
        val engine = WebViewSignInEngine(store(), onVerified = { _, _ -> false })
        val surface = WebView(context)
        engine.load("tapas")
        engine.startSignIn(surface)
        engine.verified(engine.currentSession())
        assertEquals(SignInState.Failed("signin_unavailable"), engine.state.value)
        surface.destroy()
    }

    @Test fun originContainsNoLoginCodePasswordOrPrivatePath() {
        val engine = WebViewSignInEngine(store())
        val surface = WebView(context)
        engine.load("tapas")
        engine.startSignIn(surface)
        engine.pageStarted("https://fixture:FIXTURE_PASSWORD@accounts.example.test/private?code=FIXTURE_CODE#fragment",
            engine.currentSession())
        assertEquals("https://accounts.example.test", engine.currentOrigin())
        assertEquals(SignInState.Page("https://accounts.example.test"), engine.state.value)
        engine.cancelSignIn()
        assertNull(engine.currentOrigin())
        surface.destroy()
    }

    @Test fun anOldSurfaceCannotFailOrSettleTheReplacementAttempt() {
        val engine = WebViewSignInEngine(store())
        val old = WebView(context)
        engine.load("tapas")
        engine.startSignIn(old)
        val client = old.webViewClient as SignInWebViewClient
        engine.cancelSignIn()
        val replacement = WebView(context)
        engine.startSignIn(replacement)
        val current = engine.currentSession()
        assertTrue(client.shouldOverrideUrlLoading(old, "http://fixture.example.test/"))
        client.onPageStarted(old, "https://m.tapas.io/account", null)
        client.onPageFinished(old, "https://m.tapas.io/account")
        assertEquals(current, engine.currentSession())
        assertTrue(engine.state.value is SignInState.Page)
        engine.cancelSignIn()
        old.destroy(); replacement.destroy()
    }

    @Test fun anOldProfilesClearCallbackCannotReplaceTheNewSitesState() {
        var complete: ((Boolean) -> Unit)? = null
        val original = WebViewCookieStore(setOf("m.tapas.io"), clearProfile = { complete = it })
        val engine = WebViewSignInEngine(original,
            sites = WebViewSignInEngine.BETA_SITES + ("webtoon" to SiteAddress(
                "https://www.webtoons.com", "/member/login", setOf("www.webtoons.com"))),
            storeFor = { if (it == "tapas") original else WebViewCookieStore(setOf("www.webtoons.com")) })
        engine.load("tapas")
        var cleared: Boolean? = null
        engine.signOut { cleared = it }
        shadowOf(Looper.getMainLooper()).idle()
        assertNotNull(complete)
        engine.load("webtoon")
        complete!!.invoke(true)
        shadowOf(Looper.getMainLooper()).idle()
        assertEquals(false, cleared)
        assertEquals("webtoon", engine.loadedSiteKey())
        assertEquals(SignInState.SignedOut, engine.state.value)
    }

    @Test fun anUnavailableProfileCannotClearTheFormerSitesCookies() {
        var clearCalls = 0
        val original = WebViewCookieStore(setOf("m.tapas.io"), clearProfile = { clearCalls++; it(true) })
        val engine = WebViewSignInEngine(original,
            sites = WebViewSignInEngine.BETA_SITES + ("webtoon" to SiteAddress(
                "https://www.webtoons.com", "/member/login", setOf("www.webtoons.com"))),
            storeFor = { check(it == "tapas"); original })
        engine.load("tapas")
        engine.load("webtoon")
        var cleared: Boolean? = null
        engine.signOut { cleared = it }
        shadowOf(Looper.getMainLooper()).idle()
        assertEquals(false, cleared)
        assertEquals(0, clearCalls)
    }

    @Test fun expiredWorkerAuthenticationReopensTheSignInRouteWithoutClearingCookies() {
        var cleared = false
        val engine = WebViewSignInEngine(WebViewCookieStore(setOf("m.tapas.io"),
            clearProfile = { cleared = true; it(true) }))
        val surface = WebView(context)
        engine.load("tapas")
        engine.startSignIn(surface)
        engine.verified(engine.currentSession())
        assertTrue(engine.state.value is SignInState.SignedIn)
        engine.requireSignIn()
        assertEquals(SignInState.Failed("login_required"), engine.state.value)
        assertFalse(cleared)
        surface.destroy()
    }
}
