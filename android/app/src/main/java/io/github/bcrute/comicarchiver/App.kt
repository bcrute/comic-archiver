package io.github.bcrute.comicarchiver

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPython

/**
 * Starts the embedded Python runtime once per process (native line; the bootstrap form is
 * verified by the first fleet build).
 */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) Python.start(AndroidPython(this))
    }
}
