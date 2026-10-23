package io.github.bcrute.comicarchiver.runtime

import android.os.Looper
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File

internal data class WorkingLibrarySiteProof(val key: String, val series: Int, val episodes: Int,
    val cloudOnly: Int, val accountBound: Boolean)
internal data class WorkingLibraryProof(val digest: String, val sites: List<WorkingLibrarySiteProof>)
internal class WorkingPreparationRequest(val captured: File, val payload: File, val ticket: String,
    val sourceKey: String, val recover: Boolean) {
    override fun toString() = "WorkingPreparationRequest(private)"
}

/** Native IO path with a cancellation capability, separate from the prompt JSON command bridge. */
internal class WorkingLibraryPreparation(private val sites: Set<String>,
    private val request: (WorkingPreparationRequest, LibraryCaptureCancellation) -> String = { call, cancellation ->
        check(Looper.myLooper() != Looper.getMainLooper())
        Python.getInstance().getModule("app_bridge").callAttr("working_library_prepare", call.captured.absolutePath,
            call.payload.absolutePath, call.ticket, call.sourceKey, cancellation, call.recover).toString()
    }) {
    fun prepare(captured: File, payload: File, pending: PendingWorkingLibrary,
        cancellation: LibraryCaptureCancellation, recover: Boolean = false): WorkingLibraryProof? {
        try {
            cancellation.check()
            val response = JSONObject(request(WorkingPreparationRequest(captured, payload,
                pending.id, pending.source.key, recover), cancellation))
            cancellation.check()
            if (response.opt("v") != 1 || response.opt("ok") !is Boolean) fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (!response.getBoolean("ok")) {
                val failure = when (response.optString("code")) {
                    "library_import_cancelled" -> LibraryCaptureFailure.CANCELLED
                    "library_import_changed" -> LibraryCaptureFailure.CHANGED
                    "library_import_incomplete", "library_import_invalid" -> LibraryCaptureFailure.INCOMPLETE
                    else -> LibraryCaptureFailure.PRIVATE_COPY
                }
                fail(failure)
            }
            if (response.isNull("result")) {
                if (!recover || !response.has("result")) fail(LibraryCaptureFailure.PRIVATE_COPY)
                return null
            }
            val result = response.getJSONObject("result")
            val digest = result.opt("digest") as? String ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
            if (fields(result) != setOf("digest", "sites") || !digest.matches(Regex("[a-f0-9]{64}")))
                fail(LibraryCaptureFailure.PRIVATE_COPY)
            val rows = result.getJSONArray("sites")
            if (rows.length() !in 1..sites.size) fail(LibraryCaptureFailure.PRIVATE_COPY)
            val proofs = ArrayList<WorkingLibrarySiteProof>()
            for (index in 0 until rows.length()) {
                val row = rows.getJSONObject(index)
                if (fields(row) != setOf("key", "series", "episodes", "cloud_only", "account_bound"))
                    fail(LibraryCaptureFailure.PRIVATE_COPY)
                val key = row.opt("key") as? String ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
                val bound = row.opt("account_bound") as? Boolean ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
                if (key !in sites || proofs.any { it.key == key }) fail(LibraryCaptureFailure.PRIVATE_COPY)
                fun count(name: String): Int {
                    val number = row.opt(name) as? Number ?: fail(LibraryCaptureFailure.PRIVATE_COPY)
                    val count = number.toLong()
                    if (count !in 0L..WorkingLibraryCapture.MAX_FILES.toLong() || number.toDouble() != count.toDouble())
                        fail(LibraryCaptureFailure.PRIVATE_COPY)
                    return count.toInt()
                }
                val series = count("series"); val episodes = count("episodes"); val cloud = count("cloud_only")
                if (cloud > episodes || episodes > 0 && series == 0) fail(LibraryCaptureFailure.PRIVATE_COPY)
                proofs.add(WorkingLibrarySiteProof(key, series, episodes, cloud, bound))
            }
            return WorkingLibraryProof(digest, proofs)
        } catch (error: LibraryCaptureException) { throw error }
          catch (_: Exception) { cancellation.check(); fail(LibraryCaptureFailure.PRIVATE_COPY) }
    }

    private fun fields(value: JSONObject) = value.keys().asSequence().toSet()
    private fun fail(failure: LibraryCaptureFailure): Nothing = throw LibraryCaptureException(failure)
}
