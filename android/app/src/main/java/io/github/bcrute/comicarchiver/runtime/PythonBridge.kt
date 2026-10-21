package io.github.bcrute.comicarchiver.runtime

/**
 * Kotlin side of the versioned JSON bridge (docs/android-beta-contracts.md §2), frozen.
 *
 * The native line owns the Chaquopy-backed implementation. [op] must be one of the frozen op
 * names; [argsJson] is a JSON object; the return value is the response envelope as JSON text.
 * Callers never see Python exceptions: failures arrive as ok=false envelopes with public-safe
 * messages. Long work runs inside Python on the worker thread; calls return promptly.
 */
interface PythonBridge {
    fun dispatch(op: String, argsJson: String): String
}
