package io.github.bcrute.comicarchiver.runtime

import android.app.Application
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

/** Envelope/cancellation seam only; the compiled native/core consumer remains an independent required journey. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class WorkingLibraryPreparationTest {
    @get:Rule val temporary = TemporaryFolder()
    private val source = WorkingLibrarySource("a".repeat(64), "content://fixture.documents/tree/root")
    private val pending = PendingWorkingLibrary(UUID.randomUUID().toString(), "Picked library", source)
    private fun result() = JSONObject().put("v", 1).put("ok", true).put("result", JSONObject()
        .put("digest", "b".repeat(64)).put("sites", JSONArray().put(JSONObject()
            .put("key", "tapas").put("series", 1).put("episodes", 2).put("cloud_only", 1).put("account_bound", true))))
    private fun call(body: String, recover: Boolean = false) = WorkingLibraryPreparation(setOf("tapas"), { _, _ -> body })
        .prepare(temporary.root.resolve("capture/workspace"), temporary.root.resolve("payload"), pending,
            LibraryCaptureCancellation(), recover)
    private fun failure(expected: LibraryCaptureFailure = LibraryCaptureFailure.PRIVATE_COPY, action: () -> Unit) {
        try { action(); fail("Expected a bounded preparation refusal") }
        catch (error: LibraryCaptureException) {
            assertEquals(expected, error.failure)
            assertEquals(expected.words, error.message)
            assertFalse(error.message.orEmpty().contains("PRIVATE"))
        }
    }

    @Test fun aCoreProofKeepsCountsAndBindingSeparateFromNativeSelection() {
        val proof = call(result().toString())!!
        assertEquals("b".repeat(64), proof.digest)
        assertEquals(WorkingLibrarySiteProof("tapas", 1, 2, 1, true), proof.sites.single())
        assertFalse(temporary.root.resolve("library-locations.json").exists())
        assertNull(call(JSONObject().put("v", 1).put("ok", true).put("result", JSONObject.NULL).toString(), true))
        failure { call(JSONObject().put("v", 1).put("ok", true).put("result", JSONObject.NULL).toString()) }
    }

    @Test fun malformedCountsProvidersAndProofsAreNotAcceptedAsVerifiedLibraries() {
        for (kind in listOf("digest", "negative", "fractional", "boolean-count", "unknown-site", "extra-field", "cloud-count", "duplicate", "missing")) {
            val value = result(); val result = value.getJSONObject("result")
            val row = result.getJSONArray("sites").getJSONObject(0)
            when (kind) {
                "digest" -> result.put("digest", "PRIVATE")
                "negative" -> row.put("episodes", -1)
                "fractional" -> row.put("episodes", 1.5)
                "boolean-count" -> row.put("episodes", true)
                "unknown-site" -> row.put("key", "PRIVATE")
                "extra-field" -> row.put("path", "PRIVATE")
                "cloud-count" -> row.put("cloud_only", 3)
                "duplicate" -> result.getJSONArray("sites").put(JSONObject(row.toString()))
                "missing" -> row.remove("account_bound")
            }
            failure { call(value.toString()) }
        }
        failure { call("PRIVATE not JSON") }
    }

    @Test fun cancellationAndUntrustedFailureMessagesNeverLeakPaths() {
        for ((code, expected) in listOf("library_import_cancelled" to LibraryCaptureFailure.CANCELLED,
            "library_import_changed" to LibraryCaptureFailure.CHANGED, "library_import_incomplete" to LibraryCaptureFailure.INCOMPLETE,
            "library_import_invalid" to LibraryCaptureFailure.INCOMPLETE, "PRIVATE" to LibraryCaptureFailure.PRIVATE_COPY)) {
            failure(expected) { call(JSONObject().put("v", 1).put("ok", false).put("code", code).put("message", "PRIVATE").toString()) }
        }
        val cancellation = LibraryCaptureCancellation()
        var requests = 0
        val preparation = WorkingLibraryPreparation(setOf("tapas"), { _, _ -> requests++; result().toString() })
        cancellation.cancel()
        failure(LibraryCaptureFailure.CANCELLED) {
            preparation.prepare(temporary.root, temporary.root, pending, cancellation)
        }
        assertEquals(0, requests)
        val late = LibraryCaptureCancellation()
        val cancelled = WorkingLibraryPreparation(setOf("tapas"), { _, control -> control.cancel(); result().toString() })
        failure(LibraryCaptureFailure.CANCELLED) { cancelled.prepare(temporary.root, temporary.root, pending, late) }
    }
}
