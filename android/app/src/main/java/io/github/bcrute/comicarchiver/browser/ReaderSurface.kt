package io.github.bcrute.comicarchiver.browser

import android.app.Presentation
import android.content.Context
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.view.Window
import android.view.WindowManager
import android.webkit.WebView
import android.widget.FrameLayout

/**
 * A real, laid-out browser window owned by the process rather than an Activity.
 * Its display is private and own-content-only: it never mirrors the phone screen.
 * Frames are drained/discarded in memory; no recording or filesystem output exists.
 * The same surface can outlive navigation or Activity recreation while its worker
 * remains admitted. Android's foreground-service/system limits still apply.
 */
internal class ReaderSurface private constructor(
    val view: WebView,
    private val presentation: Presentation,
    private val display: VirtualDisplay,
    private val frames: ImageReader,
) {
    private var closed = false

    /** The browser owner closes its script waiters before releasing this window. */
    fun close() {
        requireMain()
        if (closed) return
        closed = true
        release(view, presentation, display, frames)
    }

    companion object {
        internal const val WIDTH = 1280
        internal const val HEIGHT = 720
        private const val DENSITY = 160

        /** [boundView] must profile-bind the newly created WebView before any other use. */
        fun create(context: Context, boundView: (Context) -> WebView): ReaderSurface {
            requireMain()
            var frames: ImageReader? = null
            var display: VirtualDisplay? = null
            var presentation: Presentation? = null
            var view: WebView? = null
            try {
                val handler = Handler(Looper.getMainLooper())
                val manager = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager
                    ?: throw ScriptRequestException(ScriptFailure.NOT_READY)
                val images = ImageReader.newInstance(WIDTH, HEIGHT, PixelFormat.RGBA_8888, 2)
                frames = images
                images.setOnImageAvailableListener({ reader ->
                    // Drain even when no page pixels are needed; a full producer queue
                    // would otherwise stall rendering/timers behind a hidden window.
                    try { reader.acquireLatestImage()?.close() } catch (_: IllegalStateException) { }
                }, handler)
                val created = manager.createVirtualDisplay("ComicArchiverReader", WIDTH, HEIGHT, DENSITY,
                    images.surface, DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY)
                    ?: throw ScriptRequestException(ScriptFailure.NOT_READY)
                display = created
                val window = Presentation(context.applicationContext, created.display)
                presentation = window
                window.requestWindowFeature(Window.FEATURE_NO_TITLE)
                window.window?.addFlags(WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED)
                val content = FrameLayout(window.context)
                val browser = boundView(window.context)
                view = browser
                content.addView(browser, FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                window.setContentView(content)
                window.setCancelable(false)
                window.show()
                window.window?.apply {
                    decorView.setPadding(0, 0, 0, 0)
                    setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                }
                browser.requestFocus()
                return ReaderSurface(browser, window, created, images)
            } catch (_: Exception) {
                release(view, presentation, display, frames)
                throw ScriptRequestException(ScriptFailure.NOT_READY)
            }
        }

        private fun requireMain() {
            check(Looper.myLooper() == Looper.getMainLooper()) { "The reading surface belongs to the main thread." }
        }

        /** All allocations are independently released, including partial construction. */
        private fun release(view: WebView?, presentation: Presentation?, display: VirtualDisplay?, frames: ImageReader?) {
            try { view?.stopLoading() } catch (_: Exception) { }
            try { (view?.parent as? ViewGroup)?.removeView(view) } catch (_: Exception) { }
            try { view?.destroy() } catch (_: Exception) { }
            try { presentation?.dismiss() } catch (_: Exception) { }
            try { display?.release() } catch (_: Exception) { }
            try { frames?.setOnImageAvailableListener(null, null) } catch (_: Exception) { }
            try { frames?.close() } catch (_: Exception) { }
        }
    }
}
