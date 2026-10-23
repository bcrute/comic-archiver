package io.github.bcrute.comicarchiver.ui

import android.content.Context
import android.os.Looper
import android.webkit.WebView
import android.view.ViewGroup
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import io.github.bcrute.comicarchiver.App
import io.github.bcrute.comicarchiver.MainActivity
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import io.github.bcrute.comicarchiver.runtime.SiteWorkspace
import io.github.bcrute.comicarchiver.signin.SiteAddress
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Actual Activity/controller/engine routing with synthetic provider/profile seams.
 * Real profile isolation, HTTP, filesystem archives and Android service execution are
 * separate fleet/system/device requirements; no mock is reported as that evidence. */
@RunWith(RobolectricTestRunner::class)
@Config(application = SwitchTestApp::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class SiteSwitchActivityTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    private val app get() = compose.activity.application as SwitchTestApp
    private fun await(condition: () -> Boolean) = compose.waitUntil(10_000) {
        shadowOf(Looper.getMainLooper()).idle(); condition()
    }
    private fun ready(key: String = "tapas") = await {
        app.controller.state.value.site == key && !app.controller.state.value.siteBusy &&
            app.controller.state.value.signIn == SignInState.SignedIn(key) &&
            app.controller.state.value.library.isNotEmpty() && app.controller.state.value.settings != null
    }
    private fun select(label: String) = compose.onNode(hasText(label) and hasClickAction()).performClick()

    @Test fun idleSwitchUsesTheOtherWorkspaceAndRestoresHeldLibraryWithoutGo() {
        ready()
        select("Webtoon"); ready("webtoon")
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        assertEquals("Webtoon book", app.controller.state.value.library.single().title)
        val prepared = app.wire.requests.last { it.first == "prepare_workspace" }.second
        assertTrue(prepared.getString("root").endsWith("/workspace/Webtoon"))
        assertEquals("webtoon", prepared.getString("site_key"))
        assertEquals(0, app.wire.count("go")); assertEquals(0, app.wire.count("worker_start"))
        compose.activityRule.scenario.recreate()
        assertEquals("webtoon", app.controller.state.value.site)
        assertEquals(DownloadState.Held, app.controller.state.value.download)
        select("Tapas"); ready()
        assertEquals("Tapas book", app.controller.state.value.library.single().title)
    }

    @Test fun switchingDuringSavingRefusesWithVisibleRecoveryAndKeepsTheOriginalSite() {
        ready()
        select("Go")
        await { app.controller.state.value.download is DownloadState.Running }
        select("Library")
        select("Webtoon")
        compose.onNodeWithText("Finish sign-in, saving, export or cloud activity before switching sites.")
            .assertIsDisplayed()
        assertEquals("tapas", app.controller.state.value.site)
        assertEquals(1, app.wire.count("prepare_workspace"))
    }

    @Test fun aRefusedWorkspaceKeepsItsLibraryAndNeverAdmitsAnotherJob() {
        ready()
        app.wire.refuseWebtoon = true
        select("Library"); select("Webtoon")
        await { !app.controller.state.value.siteBusy && app.controller.state.value.notice != null }
        assertEquals("tapas", app.controller.state.value.site)
        assertEquals("Tapas book", app.controller.state.value.library.single().title)
        assertEquals(0, app.wire.count("go")); assertEquals(0, app.wire.count("worker_start"))
        compose.onNodeWithText("The selected workspace is unavailable. Try again.").assertIsDisplayed()
    }

    @Test fun cancellationOfALateRestoreCannotCommitThatSitesSession() {
        ready()
        val release = CountDownLatch(1)
        app.wire.holdWebtoonRestore = release
        try {
            select("Webtoon")
            await { app.wire.restoreEntered.count == 0L }
            compose.runOnIdle { app.controller.cancelSignIn() }
            release.countDown()
            await { app.controller.state.value.signIn is SignInState.Failed }
            assertEquals(0, app.wire.requests.count { it.first == "signin_commit" &&
                it.second.optString("site_key") == "webtoon" })
            assertEquals(0, app.wire.count("go"))
        } finally { release.countDown() }
    }

    @Test fun speedChoicesStayWithTheirSiteWhileSwitchingNeverStartsWork() {
        ready()
        select("Settings"); compose.onNodeWithText("Slow").performScrollTo().performClick()
        await { app.controller.state.value.settings?.speed == "slow" && !app.controller.state.value.settingsBusy }
        select("Webtoon"); ready("webtoon")
        assertEquals("medium", app.controller.state.value.settings?.speed)
        select("Tapas"); ready()
        assertEquals("slow", app.controller.state.value.settings?.speed)
        assertEquals(0, app.wire.count("go"))
    }

    @Test fun aWorkerLoginRefusalShowsSignInAgainWhileTheLibraryRemainsAvailable() {
        ready()
        app.wire.loginRequired = true
        select("Go")
        await { app.controller.state.value.signIn == SignInState.Failed("login_required") }
        compose.onNodeWithText("Sign in to Tapas").assertIsDisplayed()
        select("Library")
        compose.onNodeWithText("Tapas book").assertIsDisplayed()
        assertEquals(1, app.wire.count("go"))
    }

    @Test
    @Config(application = OfflineSwitchTestApp::class)
    fun savedFilesRemainReachableWhenTheOtherSitesBrowserProfileIsUnavailable() {
        ready()
        select("Webtoon")
        await { app.controller.state.value.site == "webtoon" && !app.controller.state.value.siteBusy &&
            app.controller.state.value.library.isNotEmpty() }
        select("Library")
        compose.onNodeWithText("Webtoon book").assertIsDisplayed()
        assertFalse(app.controller.state.value.signIn is SignInState.SignedIn)
        assertEquals(0, app.wire.count("go")); assertEquals(0, app.wire.count("worker_start"))
    }

    @Test
    @Config(application = SurfaceFailureTestApp::class)
    fun repeatedSurfaceFailureKeepsItsNoticeAndANewSignInCanRecover() {
        await { !app.controller.state.value.siteBusy && app.controller.state.value.notice != null }
        compose.onNodeWithText("The sign-in page could not be opened. Update Android System WebView and try sign-in again.")
            .assertIsDisplayed()
        compose.onNodeWithText("Press Go to start it again.").assertDoesNotExist()
        compose.runOnIdle { (app as SurfaceFailureTestApp).failSurface = false }
        compose.onNodeWithText("Sign in to Tapas").performClick()
        await { app.controller.state.value.signIn is SignInState.Page }
        assertNull(app.controller.state.value.notice)
        assertEquals(0, app.wire.count("go")); assertEquals(0, app.wire.count("worker_start"))
    }

    @Test fun failedProfileClearKeepsARetryAndCannotStartOrSwitchAnAccount() {
        ready()
        app.failClear = true
        select("About"); compose.onNodeWithText("Sign out of Tapas").performClick()
        await { app.controller.state.value.signOutPending && !app.controller.state.value.siteBusy }
        assertEquals(0, app.wire.count("signout_record"))
        compose.onNode(hasText("Webtoon") and hasClickAction()).assertIsNotEnabled()
        compose.runOnIdle { app.controller.go(); app.controller.selectSite("webtoon") }
        assertEquals("tapas", app.controller.state.value.site)
        assertEquals(0, app.wire.count("go"))
        select("Library"); compose.onNodeWithText("Tapas book").assertIsDisplayed()
        compose.onNodeWithText("Retry sign-out").assertIsDisplayed()
        assertTrue(app.getSharedPreferences("comicarchiver_session_actions", 0).getBoolean("signout_tapas", false))
        compose.runOnIdle { app.failClear = false }
        compose.onNodeWithText("Retry sign-out").performClick()
        await { !app.controller.state.value.signOutPending && !app.controller.state.value.siteBusy &&
            app.controller.state.value.signIn == SignInState.SignedOut }
        assertEquals(1, app.wire.count("signout_record"))
        assertEquals(2, app.clearCalls.get())
        assertFalse(app.getSharedPreferences("comicarchiver_session_actions", 0).contains("signout_tapas"))
        assertEquals(SignInState.SignedOut, app.controller.state.value.signIn)
    }

    @Test
    @Config(application = ViewportTestApp::class)
    fun signInSetsBrowserLayoutBeforeNavigationAndAfterActivityRecreation() {
        ready()
        fun startAndInspect(): WebView {
            compose.runOnIdle { app.controller.startSignIn() }
            await { app.controller.state.value.signIn is SignInState.Page }
            compose.onNodeWithText("https://m.tapas.io").assertIsDisplayed()
            return compose.runOnIdle { app.surfaces.last().also { surface ->
                assertTrue("The sign-in screen must mount its browser", surface.parent is ViewGroup)
                assertEquals(ViewGroup.LayoutParams.MATCH_PARENT, surface.layoutParams.width)
                assertEquals(ViewGroup.LayoutParams.MATCH_PARENT, surface.layoutParams.height)
                val firstLoad = (app as ViewportTestApp).layoutsAtLoad.last()
                assertSame(surface, firstLoad.first)
                assertEquals(ViewGroup.LayoutParams.MATCH_PARENT to ViewGroup.LayoutParams.MATCH_PARENT,
                    firstLoad.second)
            } }
        }
        val first = startAndInspect()
        compose.activityRule.scenario.recreate()
        val replacement = startAndInspect()
        assertNotSame(first, replacement)
        assertEquals(0, app.wire.count("go"))
    }

    @Test fun aFailedRecordForgetRemainsRetryableAfterTheBrowserProfileIsAlreadyClear() {
        ready()
        app.wire.refuseForget = true
        select("About"); compose.onNodeWithText("Sign out of Tapas").performClick()
        await { app.controller.state.value.signOutPending && !app.controller.state.value.siteBusy }
        assertEquals(1, app.wire.count("signout_record"))
        assertEquals(1, app.clearCalls.get())
        compose.runOnIdle { app.wire.refuseForget = false }
        compose.onNodeWithText("Retry sign-out").performClick()
        await { !app.controller.state.value.signOutPending && !app.controller.state.value.siteBusy }
        assertEquals(2, app.wire.count("signout_record"))
        assertEquals(2, app.clearCalls.get())
        assertEquals(0, app.wire.count("go"))
    }

    @Test
    @Config(application = PendingSignOutTestApp::class)
    fun aColdProcessWithUnfinishedSignOutShowsItsLibraryAndRetryWithoutRestoringAuth() {
        await { !app.controller.state.value.siteBusy && app.controller.state.value.library.isNotEmpty() }
        assertTrue(app.controller.state.value.signOutPending)
        assertEquals(0, app.wire.count("signin_restore"))
        assertEquals(SignInState.SignedOut, app.controller.state.value.signIn)
        select("Library"); compose.onNodeWithText("Tapas book").assertIsDisplayed()
        compose.onNodeWithText("Retry sign-out").performClick()
        await { !app.controller.state.value.signOutPending && !app.controller.state.value.siteBusy }
        assertEquals(1, app.wire.count("signout_record"))
        assertEquals(0, app.wire.count("signin_restore"))
        assertEquals(0, app.wire.count("go"))
    }
}

/** Record the real Activity's parameters when navigation starts. Robolectric has
 * no Chromium measure delegate, so actual CSS/viewport sizes require the device
 * gate. Do not manufacture browser measurements in this ordering/lifecycle test. */
class ViewportTestApp : SwitchTestApp() {
    val layoutsAtLoad = CopyOnWriteArrayList<Pair<WebView, Pair<Int?, Int?>>>()
    override fun createSignInSurface(context: Context, key: String) = object : WebView(context) {
        override fun loadUrl(url: String) {
            layoutsAtLoad.add(this to (layoutParams?.width to layoutParams?.height))
            super.loadUrl(url)
        }
    }.also { surfaces.add(it) }
}

open class SwitchTestApp : App() {
    val wire = SwitchBridge()
    var failClear = false
    val clearCalls = java.util.concurrent.atomic.AtomicInteger()
    val surfaces = CopyOnWriteArrayList<WebView>()
    override val bridge: PythonBridge get() = wire
    override val signInSites get() = WebViewSignInEngine.BETA_SITES + mapOf(
        "webtoon" to SiteAddress("https://www.webtoons.com", "/member/login", setOf("www.webtoons.com"),
            name = "Webtoon", workspaceFolder = "Webtoon"),
        "tappytoon" to SiteAddress("https://www.tappytoon.com", "/en/login", setOf("www.tappytoon.com"),
            name = "Tappytoon", workspaceFolder = "Tappytoon"))
    private val stores = mutableMapOf<String, WebViewCookieStore>()
    override fun startRuntime() {}
    override fun selectRuntimeCookieStore(store: WebViewCookieStore, siteKey: String) {}
    override fun deactivateRuntimeCookieStore() {}
    override fun startNetworkWriter(target: SiteWorkspace) {}
    override fun signInAvailable(key: String) = true
    override fun cookieStoreForSite(key: String): WebViewCookieStore =
        stores.getOrPut(key) { WebViewCookieStore(signInSites.getValue(key).cookieHosts,
            clearProfile = { done -> clearCalls.incrementAndGet(); done(!failClear) }) }
    override fun createSignInSurface(context: Context, key: String) = WebView(context).also { surfaces.add(it) }
}

class OfflineSwitchTestApp : SwitchTestApp() {
    override fun signInAvailable(key: String) = key == "tapas"
    override fun cookieStoreForSite(key: String): WebViewCookieStore {
        check(key == "tapas") { "Synthetic missing profile capability" }
        return super.cookieStoreForSite(key)
    }
    override fun createSignInSurface(context: Context, key: String): WebView {
        check(key == "tapas") { "Synthetic missing profile capability" }
        return super.createSignInSurface(context, key)
    }
}

class SurfaceFailureTestApp : SwitchTestApp() {
    var failSurface = true
    override fun createSignInSurface(context: Context, key: String): WebView {
        check(!failSurface) { "Synthetic unavailable browser surface" }
        return super.createSignInSurface(context, key)
    }
}

class PendingSignOutTestApp : SwitchTestApp() {
    override fun onCreate() {
        super.onCreate()
        getSharedPreferences("comicarchiver_session_actions", 0).edit()
            .putBoolean("signout_tapas", true).commit()
    }
}

class SwitchBridge : PythonBridge {
    val requests = CopyOnWriteArrayList<Pair<String, JSONObject>>()
    private val fallback = LifecycleBridge()
    private val speeds = java.util.concurrent.ConcurrentHashMap<String, String>()
    @Volatile var refuseWebtoon = false
    @Volatile var loginRequired = false
    @Volatile var refuseForget = false
    @Volatile var holdWebtoonRestore: CountDownLatch? = null
    val restoreEntered = CountDownLatch(1)
    fun count(op: String) = requests.count { it.first == op }

    override fun dispatch(op: String, argsJson: String): String {
        val args = JSONObject(argsJson)
        requests.add(op to args)
        val root = args.optString("root").ifBlank { File(args.optString("session_root")).parent ?: "" }
        val site = when (File(root).name) { "Webtoon" -> "Webtoon"; "Tappytoon" -> "Tappytoon"; else -> "Tapas" }
        if (op == "prepare_workspace" && site == "Webtoon" && refuseWebtoon)
            return """{"ok":false,"code":"refused","message":"The selected workspace is unavailable. Try again."}"""
        if (op == "signout_record" && refuseForget)
            return """{"ok":false,"code":"refused","message":"The sign-out record could not be removed. Try again."}"""
        if (op == "signin_restore" && site == "Webtoon") {
            restoreEntered.countDown()
            holdWebtoonRestore?.await(5, TimeUnit.SECONDS)
        }
        val result = when (op) {
            "download_status" -> if (loginRequired)
                """{"worker":{"running":true},"account":{"status":"held","code":"login_required"}}"""
                else return fallback.dispatch(op, argsJson)
            "worker_stop" -> """{"running":false,"stop_requested":false}"""
            "library_listing" -> """[{"seriesId":"fixture","title":"$site book","kind":"comics","episodeCount":1,"savedCount":1}]"""
            "settings", "configure_settings" -> {
                if (args.has("speed")) speeds[root] = args.getString("speed")
                """{"wifi_only":true,"speed":"${speeds[root] ?: "medium"}","connection":"wifi","metered":false,"allowed":true,
                  "speeds":[{"key":"medium","label":"Medium","available":true},{"key":"slow","label":"Slow","available":true}]}"""
            }
            else -> return fallback.dispatch(op, argsJson)
        }
        return """{"ok":true,"result":$result}"""
    }
}
