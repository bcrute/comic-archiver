package io.github.bcrute.comicarchiver

import android.os.Bundle
import android.os.Build
import android.util.Log
import android.view.ViewGroup
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.toArgb
import androidx.core.view.WindowCompat
import androidx.compose.ui.viewinterop.AndroidView
import com.chaquo.python.Python
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.DefaultAppController
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.ui.ComicArchiverAppRoot
import io.github.bcrute.comicarchiver.ui.ComicArchiverTheme

/**
 * The app's one activity (UI/export line): mounts the Compose root over the real
 * controller and the export runtime.
 *
 * Normal start is the working UI. Launching with the boolean extra "selfcheck" ADDS the
 * in-app qualification (app_bridge ping + android_selfcheck) that logs one JSON line
 * under the ComicArchiverSelfcheck tag for scripts/android_feasibility.py; that path is
 * unchanged from the scaffold launcher and never logs credentials.
 *
 * Sign-in's WebView is one activity-owned surface the frozen engine drives, mounted in
 * the tree only while a sign-in is in flight; no object is ever
 * exposed to page JavaScript. The folder picker's persistable grant lands in
 * [SafExportRuntime.onFolderGranted].
 */
class MainActivity : ComponentActivity() {

    private lateinit var controller: AppController

    /** The one activity-owned sign-in WebView; destroyed with the activity in [onDestroy]. */
    private var signInWebView by mutableStateOf<WebView?>(null)
    private var surfaceSite: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Compose owns the insets on every supported API. Leaving Android's
        // default adjustPan active can move the entire app off a short IME window.
        enableEdgeToEdge()

        val app = applicationContext as App

        if (intent.getBooleanExtra("selfcheck", false)) {
            Thread {
                val result = try {
                    val ping = app.bridge.dispatch("ping", "{}")
                    val selfcheck = Python.getInstance().getModule("android_selfcheck").callAttr("run").toString()
                    """{"ping":$ping,"selfcheck":$selfcheck}"""
                } catch (error: Exception) {
                    """{"error":"${error.javaClass.simpleName}"}"""
                }
                Log.i("ComicArchiverSelfcheck", result)
            }.start()
        }

        controller = app.controller
        // The sign-in WebView is activity-owned and created up front, attached to the
        // controller BEFORE any Sign in can run, so the engine always drives this one
        // instance; the UI mounts it only while a sign-in is in flight (see
        // SignInScreen). No unloaded WebView ever sits in the entry tree.
        (controller as DefaultAppController).attachSurfaceFactory(this, release = ::releaseSignInSurface) { site ->
            replaceSignInSurface(site, force = true)
        }
        try { replaceSignInSurface(controller.state.value.site) } catch (_: Exception) { surfaceUnavailable() }

        setContent {
            val appState by controller.state.collectAsState()
            val exports = remember(appState.libraryId, appState.site, appState.workspaceReady) { app.exportsFor(appState.site, appState.libraryId) }
            LaunchedEffect(appState.libraryId, appState.site) {
                try { replaceSignInSurface(appState.site) } catch (_: Exception) { surfaceUnavailable() }
            }
            var pickerSite by rememberSaveable { mutableStateOf<String?>(null) }
            var pickerLibrary by rememberSaveable { mutableStateOf<String?>(null) }
            ComicArchiverTheme(appState.theme) {
                val background = MaterialTheme.colorScheme.background
                SideEffect {
                    val light = background.luminance() > 0.5f
                    WindowCompat.getInsetsController(window, window.decorView).apply {
                        isAppearanceLightStatusBars = light
                        isAppearanceLightNavigationBars = light
                    }
                    // Older supported Android versions draw opaque system bars;
                    // API 35+ supplies the enforced edge-to-edge surface instead.
                    if (Build.VERSION.SDK_INT < 35) {
                        window.statusBarColor = background.toArgb()
                        window.navigationBarColor = background.toArgb()
                    }
                }
                val panel by exports.panel.collectAsState()
                val openedLibrary = remember { app.openedLibrary }
                val folderState by openedLibrary.state.collectAsState()
                val working = remember { app.workingLibraries }
                val workingState by working.state.collectAsState()
                LaunchedEffect(Unit) { working.load() }
                var workingPickerTicket by rememberSaveable { mutableStateOf<String?>(null) }
                val chooseWorking = rememberLauncherForActivityResult(io.github.bcrute.comicarchiver.library.ReadOnlyLibraryTree()) { uri ->
                    val ticket = workingPickerTicket; workingPickerTicket = null
                    if (ticket != null) working.picked(uri, ticket)
                }
                var folderPickerTicket by rememberSaveable { mutableStateOf<String?>(null) }
                val openLibraryFolder = rememberLauncherForActivityResult(
                    io.github.bcrute.comicarchiver.library.ReadOnlyLibraryTree(),
                ) { uri ->
                    val ticket = folderPickerTicket
                    folderPickerTicket = null
                    if (uri != null && ticket != null) openedLibrary.select(uri, ticket)
                }
                val pickFolder = rememberLauncherForActivityResult(
                    ActivityResultContracts.OpenDocumentTree(),
                ) { uri ->
                    val selected = pickerSite
                    val library = pickerLibrary
                    pickerSite = null
                    pickerLibrary = null
                    val current = controller.state.value
                    if (uri != null && selected != null && library != null && selected == current.site && library == current.libraryId &&
                        current.workspaceReady && !current.siteBusy && !current.libraryBusy)
                        app.exportsFor(selected, library).onFolderGranted(uri)
                }

                // Compose owns the whole NoActionBar window: this Surface paints the app
                // background edge to edge, and the system bars stay clear of the controls
                // instead of the old legacy action bar overlapping them.
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    Box(Modifier.windowInsetsPadding(WindowInsets.safeDrawing)) {
                        ComicArchiverAppRoot(
                            controller = controller,
                            exportState = panel,
                            folderState = folderState,
                            onFolderLoad = openedLibrary::ensureLoaded,
                            onOpenFolder = {
                                folderPickerTicket = openedLibrary.pickerTicket()
                                openLibraryFolder.launch(null)
                            },
                            onCloseFolder = openedLibrary::close,
                            onFolderQuery = openedLibrary::load,
                            onOpenFolderFile = openedLibrary::open,
                            workingLibraries = workingState,
                            workingLibraryActions = io.github.bcrute.comicarchiver.ui.WorkingLibraryActions(
                                load = working::load, pick = {
                                    working.requestPicker()?.let { ticket ->
                                        workingPickerTicket = ticket
                                        try { chooseWorking.launch(null) } catch (_: Exception) {
                                            workingPickerTicket = null; working.picked(null, ticket)
                                            controller.openNotice(Notice(BetaCodes.STORAGE_LOW, "Android could not open the folder chooser. Try again."))
                                        }
                                    }
                                }, confirm = { working.confirm(it) }, stop = working::stop, discard = working::discard,
                                retry = working::retry, select = working::select, refresh = working::refreshSource),
                            onPickFolder = {
                                val current = controller.state.value
                                if (current.workspaceReady && !current.siteBusy && !current.libraryBusy && current.site == appState.site && current.libraryId == appState.libraryId) {
                                    pickerSite = appState.site
                                    pickerLibrary = appState.libraryId
                                    pickFolder.launch(null)
                                }
                            },
                            onStartExport = {
                                val current = controller.state.value
                                if (current.workspaceReady && !current.siteBusy && !current.libraryBusy && current.site == appState.site && current.libraryId == appState.libraryId)
                                    exports.startExport()
                            },
                            onStopExport = {
                                exports.stopExport()
                                controller.cancelExport()
                            },
                            signInContent = {
                                // The pre-created, already-attached activity-owned
                                // WebView, mounted into the tree only while busy; the
                                // factory hands over the existing instance (never a new
                                // one), so detach/remount reuses the same surface.
                                val webView = signInWebView
                                if (webView != null) {
                                  key(webView) {
                                    AndroidView(
                                        // A remount (retry, back-and-forth) never fights
                                        // a stale parent: hand the one instance over
                                        // detached.
                                        factory = { _ ->
                                            webView.also {
                                                (it.parent as? ViewGroup)?.removeView(it)
                                            }
                                        },
                                        // Fills the remaining-height region SignInScreen
                                        // allots while busy; a fixed 360dp surface kept
                                        // the site's login form below the fold.
                                        modifier = Modifier.fillMaxSize(),
                                    )
                                  }
                                }
                            },
                        )
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        // An unfinished sign-in attempt ends with the activity, so the settle-time probe
        // stops asking for a surface nobody is looking at. This is a cancel, never a
        // signOut: the cookie store keeps the owner's cookies, and a completed sign-in is
        // not an attempt — nothing here ends it. Guarded for a start that failed before
        // the controller existed.
        // The activity-owned sign-in surface goes with the activity — including on
        // rotation, where the cookie store (the app's cookie authority) is deliberately
        // untouched: no signout, no cookie clearing. Detach from any Compose-view parent
        // BEFORE destroy(); destroying a still-attached WebView throws.
        if (this::controller.isInitialized) (controller as DefaultAppController).detachSurfaceFactory(this)
        releaseSignInSurface()
        super.onDestroy()
    }

    private fun replaceSignInSurface(site: String, force: Boolean = false) {
        val libraryId = controller.state.value.libraryId
        val identity = "$libraryId/$site"
        if (!force && surfaceSite == identity && signInWebView != null) return
        releaseSignInSurface()
        val surface = (applicationContext as App).createSignInSurface(this, site)
        try {
            // AndroidView's addView otherwise supplies WRAP_CONTENT. Chromium then
            // forces percentage layout height to zero even inside a measured Compose
            // box, blanking sites whose html/body fill 100% of the viewport.
            surface.layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            surfaceSite = identity
            signInWebView = surface
            (controller as DefaultAppController).attachSignInWebView(surface, site, libraryId)
        } catch (error: Exception) {
            surface.destroy()
            signInWebView = null
            surfaceSite = null
            throw error
        }
    }

    private fun releaseSignInSurface() {
        signInWebView?.let { webView ->
            if (this::controller.isInitialized) (controller as DefaultAppController).detachSignInWebView(webView)
            (webView.parent as? ViewGroup)?.removeView(webView)
            try { webView.stopLoading(); webView.destroy() } catch (_: Exception) {
                // A render-process-gone callback may already have destroyed this surface.
            }
        }
        signInWebView = null
        surfaceSite = null
    }

    private fun surfaceUnavailable() {
        val notice = Notice(BetaCodes.JOB_FAILED,
            "The sign-in page could not be opened. Update Android System WebView and try sign-in again.")
        // Creation and the initial Compose effect may both observe the same failure.
        // Only a user acknowledgement should toggle an existing notice away.
        if (controller.state.value.notice != notice) controller.openNotice(notice)
    }

}
