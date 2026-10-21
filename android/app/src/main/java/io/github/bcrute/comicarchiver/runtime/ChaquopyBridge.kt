package io.github.bcrute.comicarchiver.runtime

import com.chaquo.python.PyObject
import com.chaquo.python.Python

/**
 * Chaquopy-backed PythonBridge (native line; compile verified only by the first fleet build).
 * Loads the app's app_bridge module once per process and forwards one JSON envelope per call.
 */
class ChaquopyBridge : PythonBridge {
    private val module: PyObject by lazy { Python.getInstance().getModule("app_bridge") }

    override fun dispatch(op: String, argsJson: String): String {
        val request = """{"v":1,"op":"$op","args":$argsJson}"""
        return module.callAttr("dispatch", request).toString()
    }
}
