package io.github.bcrute.comicarchiver.runtime

/**
 * Kotlin side of the versioned JSON bridge, frozen.
 *
 * The native line owns the Chaquopy-backed implementation. [op] must be one of the frozen op
 * names; [argsJson] is a JSON object; the return value is the response envelope as JSON text.
 * Callers never see Python exceptions: failures arrive as ok=false envelopes with public-safe
 * messages. Downloads use the Python worker. Local-reader operations are synchronous,
 * bounded file operations and belong exclusively on the native reader's I/O executor.
 */
interface PythonBridge {
    fun dispatch(op: String, argsJson: String): String
}
