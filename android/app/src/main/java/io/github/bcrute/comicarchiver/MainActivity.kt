package io.github.bcrute.comicarchiver

import android.os.Bundle
import android.util.Log
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
 * Sign-in's WebView is the UI-owned surface the frozen engine drives; no object is ever
 * exposed to page JavaScript. The folder picker's persistable grant lands in
 * [SafExportRuntime.onFolderGranted].
 */
class MainActivity : ComponentActivity() {

    private lateinit var controller: AppController
    private lateinit var exports: SafExportRuntime

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
                                AndroidView(
                                    factory = { context ->
                                        WebView(context).also { webView ->
                                            (controller as DefaultAppController)
                                                .attachSignInWebView(webView)
                                        }
                                    },
                                    modifier = Modifier.fillMaxWidth().height(360.dp),
                                )
                            },
                        )
                    }
                }
            }
        }
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
