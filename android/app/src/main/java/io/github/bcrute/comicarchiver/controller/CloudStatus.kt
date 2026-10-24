package io.github.bcrute.comicarchiver.controller

import org.json.JSONObject

/** Credentials live only in a request, never AppState, saved state, or diagnostic text. */
sealed interface CloudRequest {
    class Connect(val bucket: String, val keyId: String, val applicationKey: String) : CloudRequest {
        override fun toString() = "CloudRequest.Connect(private)"
    }
    data object Cancel : CloudRequest
    data object Test : CloudRequest
    data object Retry : CloudRequest
    data object Resume : CloudRequest
    data class Cleanup(val testId: String) : CloudRequest
    data class Destination(val provider: String) : CloudRequest
    data class Sharing(val shared: Boolean) : CloudRequest
}

fun CloudRequest.json(): JSONObject = JSONObject().also { request ->
    when (this) {
        is CloudRequest.Connect -> request.put("action", "connect").put("credentials", JSONObject()
            .put("bucket", bucket).put("key_id", keyId).put("application_key", applicationKey))
        CloudRequest.Cancel -> request.put("action", "cancel")
        CloudRequest.Test -> request.put("action", "test")
        CloudRequest.Retry -> request.put("action", "retry")
        CloudRequest.Resume -> request.put("action", "resume")
        is CloudRequest.Cleanup -> request.put("action", "cleanup").put("value", testId)
        is CloudRequest.Destination -> request.put("action", "destination").put("value", provider)
        is CloudRequest.Sharing -> request.put("action", "sharing").put("value", if (shared) "shared" else "separate")
    }
}

data class CloudDestination(val key: String, val label: String, val enabled: Boolean, val checked: Boolean)
data class CloudCleanup(val id: String, val label: String, val enabled: Boolean)
data class LibraryMetric(val value: String, val label: String)
data class LibraryPlace(val id: String, val label: String, val counts: String)
data class LibraryOverview(val available: Boolean, val metrics: List<LibraryMetric>,
                           val locations: List<LibraryPlace>, val summary: String)
data class CloudStatus(
    val title: String, val explanation: String, val status: String, val auth: String,
    val repair: String, val bucket: String, val suggestion: String, val connectLabel: String,
    val canConnect: Boolean, val showCancel: Boolean, val canCancel: Boolean,
    val canTest: Boolean, val canRetry: Boolean, val showSharing: Boolean,
    val canShare: Boolean, val shared: Boolean, val sharingHelp: String,
    val cleanups: List<CloudCleanup>, val destinations: List<CloudDestination>,
    val destination: String, val destinationHelp: String, val destinationLock: String,
    val deviceStored: String, val b2Stored: String, val message: String,
    val taskRunning: Boolean, val authStatus: String, val cost: String, val taskHeld: Boolean,
    val cleanupRemaining: Int,
    val overview: LibraryOverview? = null,
    val timeEstimate: String = "",
    val spaceEstimate: String = "",
) {
    companion object {
        fun parse(envelope: String): CloudStatus {
            val answer = JSONObject(envelope)
            require(answer.opt("ok") == true)
            val value = answer.getJSONObject("result")
            val b2 = value.getJSONObject("b2")
            fun text(source: JSONObject, key: String): String = (source.opt(key) as? String
                ?: throw IllegalArgumentException("Cloud text is incomplete")).also { require(it.length <= 6000) }
            fun flag(source: JSONObject, key: String): Boolean = source.opt(key) as? Boolean
                ?: throw IllegalArgumentException("Cloud state is incomplete")
            val cleanups = b2.getJSONArray("cleanup")
            require(cleanups.length() <= 200)
            val cleanupRows = (0 until cleanups.length()).map { index ->
                val row = cleanups.getJSONObject(index)
                val id = text(row, "test_id")
                require(id.matches(Regex("[a-f0-9-]{36}")))
                CloudCleanup(id, text(row, "text"), flag(row, "enabled"))
            }
            val destinations = value.getJSONArray("destinations")
            require(destinations.length() <= 2)
            val choices = (0 until destinations.length()).map { index ->
                val row = destinations.getJSONObject(index)
                val key = text(row, "key")
                require(key in setOf("device", "b2"))
                CloudDestination(key, text(row, "label"), flag(row, "enabled"), flag(row, "checked"))
            }
            require(choices.map { it.key }.distinct().size == choices.size)
            val destination = text(value, "destination")
            require(destination in setOf("device", "b2"))
            val authStatus = text(value, "auth_status")
            require(authStatus in setOf("idle", "running", "connected", "error", "cancelled"))
            val cleanupRemaining = value.getInt("cleanup_remaining")
            require(cleanupRemaining >= 0)
            val taskRunning = flag(value, "task_running")
            val taskHeld = flag(value, "task_held")
            require(!taskHeld || taskRunning)
            val overviewSource = value.optJSONObject("overview")
            require(!value.has("overview") || overviewSource != null)
            return CloudStatus(text(b2, "pill"), text(b2, "text"), text(b2, "status"),
                text(b2, "auth"), text(b2, "repair"), b2.optString("bucket").takeUnless { it == "null" } ?: "",
                text(value, "bucket_suggestion"), text(b2, "connect_text"), flag(b2, "connect_enabled"),
                flag(b2, "cancel_visible"), flag(b2, "cancel_enabled"), flag(b2, "test_enabled"),
                flag(b2, "retry_enabled"), flag(b2, "sharing_visible"), flag(b2, "sharing_enabled"),
                flag(b2, "shared"), text(b2, "sharing_help"), cleanupRows, choices, destination,
                text(value, "destination_help"), text(value, "destination_lock"), text(value, "device_stored"),
                text(value, "b2_stored"), text(value, "message"), taskRunning, authStatus,
                text(value, "cost"), taskHeld, cleanupRemaining,
                overviewSource?.let { overview ->
                    val metrics = overview.getJSONArray("metrics")
                    val locations = overview.getJSONArray("locations")
                    require(metrics.length() <= 4 && locations.length() <= 2)
                    val metricRows = (0 until metrics.length()).map { index ->
                        val metric = metrics.getJSONArray(index)
                        require(metric.length() == 2)
                        val count = metric.getString(0)
                        val label = metric.getString(1)
                        require(count.length <= 40 && (count == "—" || count.matches(Regex("[0-9][0-9,]*"))) && label in setOf("Saved episodes", "Saved files",
                            "Episodes on this device", "Cloud episode copies"))
                        LibraryMetric(count, label)
                    }
                    val places = (0 until locations.length()).map { index ->
                        val place = locations.getJSONObject(index)
                        val id = text(place, "id")
                        require(id in setOf("device", "b2"))
                        LibraryPlace(id, text(place, "label"), text(place, "counts"))
                    }
                    require(places.map { it.id }.distinct().size == places.size)
                    LibraryOverview(flag(overview, "available"), metricRows, places, text(overview, "summary"))
                }, if (value.has("time_estimate")) text(value, "time_estimate") else "",
                if (value.has("space_estimate")) text(value, "space_estimate") else "")
        }
    }
}
