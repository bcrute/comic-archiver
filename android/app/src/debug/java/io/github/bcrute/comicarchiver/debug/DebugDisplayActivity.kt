package io.github.bcrute.comicarchiver.debug

import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color as ComposeColor
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * Debug-build-only rendering probe (never in release): discriminates whether this device
 * paints plain framework views, pure Compose, or Compose plus a WebView — the real app's
 * white screen persists after the window/theme repair, so the failing layer must be
 * identified from actual screenshots, not inferred. Started only by an explicit adb
 * intent with a mode extra; never touches the bridge, controller, cookies or any
 * account surface, and loads no network page:
 *
 *   adb shell am start -n io.github.bcrute.comicarchiver/.debug.DebugDisplayActivity \
 *       --es mode native|compose|webview
 *
 * Each mode paints its own opaque color so a screenshot is unambiguous.
 */
class DebugDisplayActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        when (intent.getStringExtra("mode")) {
            "native" -> nativeProbe()
            "compose" -> composeProbe(includeWebView = false)
            else -> composeProbe(includeWebView = true)
        }
    }

    /** Plain framework views on an opaque red field; no Compose at all. */
    private fun nativeProbe() {
        val text = TextView(this).apply {
            text = "NATIVE MODE VISIBLE"
            textSize = 24f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        }
        setContentView(FrameLayout(this).apply {
            setBackgroundColor(Color.rgb(0xB2, 0x3A, 0x48))
            addView(
                text,
                FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    Gravity.CENTER,
                ),
            )
        })
    }

    /** Pure Compose (green field), or Compose plus one WebView (its own blue page),
     *  mirroring the real app's MaterialTheme/Surface/AndroidView stack with explicit
     *  opaque colors instead of the near-white theme defaults. */
    private fun composeProbe(includeWebView: Boolean) {
        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = ComposeColor(0xFF2E6F40),
                ) {
                    Column(Modifier.fillMaxSize()) {
                        Text(
                            if (includeWebView) "COMPOSE+WEBVIEW MODE VISIBLE"
                            else "COMPOSE MODE VISIBLE",
                            color = ComposeColor.White,
                        )
                        Button(onClick = {}) { Text("Probe button") }
                        if (includeWebView) {
                            AndroidView(
                                factory = { context ->
                                    WebView(context).also { webView ->
                                        webView.loadData(
                                            "<html><body style=\"background:#204080;" +
                                                "color:white;font-size:xx-large\">" +
                                                "<p>WEBVIEW VISIBLE</p></body></html>",
                                            "text/html",
                                            "utf-8",
                                        )
                                    }
                                },
                                modifier = Modifier.fillMaxWidth().height(360.dp),
                            )
                        }
                    }
                }
            }
        }
    }
}
