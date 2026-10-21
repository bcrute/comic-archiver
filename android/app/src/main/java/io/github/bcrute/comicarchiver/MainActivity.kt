package io.github.bcrute.comicarchiver

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.chaquo.python.Python
import io.github.bcrute.comicarchiver.runtime.ChaquopyBridge

/**
 * Scaffold launcher (native line). The UI line replaces the content with Compose screens
 * against AppController.
 *
 * Launching with the boolean extra "selfcheck" runs the in-app qualification (app_bridge
 * ping + android_selfcheck) and logs one JSON line under the ComicArchiverSelfcheck tag,
 * which scripts/android_feasibility.py collects on an emulator; that log line is the only
 * output, and it never contains credentials.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (intent.getBooleanExtra("selfcheck", false)) {
            Thread {
                val result = try {
                    val ping = ChaquopyBridge().dispatch("ping", "{}")
                    val selfcheck = Python.getInstance().getModule("android_selfcheck").callAttr("run").toString()
                    """{"ping":$ping,"selfcheck":$selfcheck}"""
                } catch (error: Exception) {
                    """{"error":"${error.javaClass.simpleName}"}"""
                }
                Log.i("ComicArchiverSelfcheck", result)
            }.start()
        }
    }
}
