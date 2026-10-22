package io.github.bcrute.comicarchiver

import android.os.Bundle
import android.util.Log
import android.view.ViewGroup
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.lifecycleScope
import com.chaquo.python.Python
import io.github.bcrute.comicarchiver.controller.AppController
import io.github.bcrute.comicarchiver.controller.DefaultAppController
import io.github.bcrute.comicarchiver.export.SafExportRuntime
import io.github.bcrute.comicarchiver.signin.SignInEngine
import io.github.bcrute.comicarchiver.signin.WebViewSignInEngine
import io.github.bcrute.comicarchiver.ui.ComicArchiverAppRoot
import java.io.File

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
    private lateinit var exports: SafExportRuntime

    /** The one activity-owned sign-in WebView; destroyed with the activity in [onDestroy]. */
    private var signInWebView: WebView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

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

        controller = DefaultAppController(this, createSignInEngine(app), lifecycleScope)
        // The sign-in WebView is activity-owned and created up front, attached to the
        // controller BEFORE any Sign in can run, so the engine always drives this one
        // instance; the UI mounts it only while a sign-in is in flight (see
        // SignInScreen). No unloaded WebView ever sits in the entry tree.
        signInWebView = WebView(this)
            .also { (controller as DefaultAppController).attachSignInWebView(it) }
        exports = SafExportRuntime(
            context = this,
            bridge = app.bridge,
            workspaceRoot = File(filesDir, "workspace"),
            scope = lifecycleScope,
        )

        setContent {
            MaterialTheme {
                val panel by exports.panel.collectAsState()
                val pickFolder = rememberLauncherForActivityResult(
                    ActivityResultContracts.OpenDocumentTree(),
                ) { uri -> if (uri != null) exports.onFolderGranted(uri) }

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
                            onPickFolder = { pickFolder.launch(null) },
                            onStartExport = { exports.startExport() },
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
                                    AndroidView(
                                        // A remount (retry, back-and-forth) never fights
                                        // a stale parent: hand the one instance over
                                        // detached.
                                        factory = { _ ->
                                            webView.also {
                                                (it.parent as? ViewGroup)?.removeView(it)
                                            }
                                        },
                                        modifier = Modifier.fillMaxWidth().height(360.dp),
                                    )
                                }
                            },
                        )
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        // The activity-owned sign-in surface goes with the activity — including on
        // rotation, where the cookie store (the app's cookie authority) is deliberately
        // untouched: no signout, no cookie clearing. Detach from any Compose-view parent
        // BEFORE destroy(); destroying a still-attached WebView throws.
        signInWebView?.let { webView ->
            (webView.parent as? ViewGroup)?.removeView(webView)
            webView.stopLoading()
            webView.destroy()
        }
        signInWebView = null
        super.onDestroy()
    }

    /**
     * NATIVE SEAM (published in control-dir/ui-next-handoff.md), now filled: the engine's
     * onSettle body runs the embedded core's verified who-am-i through the bridge
     * (app.signInProbe — cookie presence alone never yields SignedIn), and the site table
     * comes from the core's own sites module (app.signInSites).
     */
    private fun createSignInEngine(app: App): SignInEngine {
        lateinit var engine: WebViewSignInEngine
        engine = WebViewSignInEngine(
            app.cookieStore,
            onSettle = { app.signInProbe.settled(engine) },
            sites = app.signInSites,
        )
        return engine
    }
}
