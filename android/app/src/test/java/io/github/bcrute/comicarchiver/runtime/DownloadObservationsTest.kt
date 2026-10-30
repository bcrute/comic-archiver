package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.CloudStatus
import io.github.bcrute.comicarchiver.controller.DownloadObservations
import io.github.bcrute.comicarchiver.controller.ObservedWorker
import io.github.bcrute.comicarchiver.ui.cloudEnvelope
import io.github.bcrute.comicarchiver.ui.dashboardAge
import io.github.bcrute.comicarchiver.ui.dashboardDelayed
import io.github.bcrute.comicarchiver.ui.dashboardDuration
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DownloadObservationsTest {
    private fun observation(result: String) = DownloadObservations.parse(
        JSONObject("""{"ok":true,"result":$result}"""), 1234)

    @Test fun publicObservationsProjectCountsListsRetryAndWorker() {
        val value = observation("""{"worker":{"running":true,"state":"busy","private":"SECRET"},
            "account":{"status":"running","phase":"lists","lists":{"checked":2,"total":4,"current":"recent"},
                "retry":{"seconds":120,"reason":"source"}},"counts":{"saved_episodes":53,"observed_series":780},
            "progress":{"age_seconds":4,"run_id":"SECRET","message":"SECRET"}}""")!!
        assertEquals(ObservedWorker.WORKING, value.worker)
        assertEquals(53L, value.savedEpisodes)
        assertEquals(780L, value.observedSeries)
        assertEquals(2L, value.listsChecked)
        assertEquals(4L, value.listsTotal)
        assertEquals("Recent", value.currentList)
        assertEquals(120L, value.retrySeconds)
        assertEquals("source", value.retryReason)
        assertEquals(4L, value.activityAgeSeconds)
        assertEquals(1234L, value.receivedAtMillis)
        assertFalse(value.toString().contains("SECRET"))
    }

    @Test fun malformedAndUnknownStatusCannotSupplyAnObservation() {
        for (body in listOf("{}", """{"ok":false}""", """{"ok":true,"result":{}}""",
            """{"ok":true,"result":{"worker":{"running":"true"},"account":{"status":"running"}}}""",
            """{"ok":true,"result":{"worker":{"running":true},"account":{"status":"SECRET"}}}""")) {
            assertNull(DownloadObservations.parse(JSONObject(body), 1))
        }
    }

    @Test fun invalidCountersAndLabelsDoNotTurnIntoPublicProgress() {
        val value = observation("""{"worker":{"running":true,"state":"SECRET"},
            "account":{"status":"running","phase":"lists","lists":{"checked":8,"total":4,"current":"SECRET"},
                "retry":{"seconds":false,"reason":"SECRET"}},
            "counts":{"saved_episodes":-1,"observed_series":"SECRET"},"progress":{"age_seconds":1.5}}""")!!
        assertEquals(ObservedWorker.AVAILABLE, value.worker)
        assertNull(value.savedEpisodes); assertNull(value.observedSeries); assertNull(value.activityAgeSeconds)
        assertNull(value.listsChecked); assertNull(value.listsTotal); assertNull(value.currentList)
        assertNull(value.retrySeconds); assertNull(value.retryReason)
        assertFalse(value.toString().contains("SECRET"))
    }

    @Test fun notDownloadedCountsArriveWholeAndInSettingsOrderOrNotAtAll() {
        fun counts(notDownloaded: String) = observation("""{"worker":{"running":false},"account":{"status":"stopped"},
            "counts":{"saved_episodes":3,"not_downloaded":$notDownloaded}}""")!!.notDownloaded
        val value = counts("""{"failed":6,"unsupported":5,"novel":4,"temporary_access":3,"scheduled":2,"locked":1}""")!!
        assertEquals(DownloadObservations.NOT_DOWNLOADED, value.keys.toList())
        assertEquals(listOf(1L, 2L, 3L, 4L, 5L, 6L), value.values.toList())
        for (partial in listOf("{}", """{"locked":1}""",
            """{"locked":-1,"scheduled":0,"temporary_access":0,"novel":0,"unsupported":0,"failed":0}""",
            """{"locked":"1","scheduled":0,"temporary_access":0,"novel":0,"unsupported":0,"failed":0}""",
            """{"locked":1,"scheduled":0,"temporary_access":0,"novel":0,"unsupported":0,"SECRET":0}""",
            """{"locked":1,"scheduled":0,"temporary_access":0,"novel":0,"unsupported":0,"failed":0,"SECRET":0}""")) {
            assertNull(partial, counts(partial))
        }
        assertNull(observation("""{"worker":{"running":false},"account":{"status":"stopped"}}""")!!.notDownloaded)
    }

    @Test fun workerStatesRemainObservationsBetweenRuns() {
        for ((worker, expected) in listOf(
            """{"running":false,"state":"busy"}""" to ObservedWorker.OFF,
            """{"running":true,"state":"ready"}""" to ObservedWorker.READY,
            """{"running":true,"stop_requested":true,"state":"busy"}""" to ObservedWorker.STOPPING,
        )) {
            val value = observation("""{"worker":$worker,"account":{"status":"stopped"},
                "counts":{"saved_episodes":0,"observed_series":2147483648},"progress":{}}""")!!
            assertEquals(expected, value.worker)
            assertEquals(0L, value.savedEpisodes)
            assertEquals(2147483648L, value.observedSeries)
            assertNull(value.currentList); assertNull(value.retrySeconds)
        }
        assertNotNull(observation("""{"worker":{"running":false},"account":{"configured":false}}"""))
    }

    private fun overviewEnvelope(): JSONObject = JSONObject(cloudEnvelope()).also {
        it.getJSONObject("result").put("overview", JSONObject("""{"available":true,
            "metrics":[["53","Saved episodes"],["108","Saved files"],["—","Cloud episode copies"]],
            "locations":[{"id":"device","label":"This device","counts":"53 episodes · 108 files"},
                {"id":"b2","label":"Backblaze B2","counts":"2 episodes · 4 files · Not connected"}],
            "summary":"Recorded saved-library counts."}"""))
            .put("time_estimate", "About an hour of saving left; estimates may change.")
            .put("space_estimate", "There may not be enough space for the remaining files.")
    }

    @Test fun cloudOverviewParsesBoundedMetricAndLocationRows() {
        val value = CloudStatus.parse(overviewEnvelope().toString())
        assertTrue(value.overview!!.available)
        assertEquals(listOf("53", "108", "—"), value.overview!!.metrics.map { it.value })
        assertEquals(listOf("device", "b2"), value.overview!!.locations.map { it.id })
        assertTrue(value.overview!!.locations.last().counts.contains("Not connected"))
        assertTrue(value.timeEstimate.contains("estimates may change"))
        assertTrue(value.spaceEstimate.contains("not be enough space"))
    }

    @Test fun oldCloudEnvelopeKeepsUnknownCountsAndMalformedUpdatesAreRefused() {
        val old = CloudStatus.parse(cloudEnvelope())
        assertNull(old.overview); assertEquals("", old.timeEstimate); assertEquals("", old.spaceEstimate)
        val changes: List<(JSONObject) -> Unit> = listOf(
            { it.getJSONArray("metrics").getJSONArray(0).put(0, "PRIVATE") },
            { it.getJSONArray("metrics").getJSONArray(0).put(1, "PRIVATE") },
            { it.getJSONArray("locations").getJSONObject(0).put("id", "PRIVATE") },
            { it.getJSONArray("locations").getJSONObject(1).put("id", "device") },
            { it.put("available", "true") },
        )
        for (change in changes) {
            val envelope = overviewEnvelope()
            change(envelope.getJSONObject("result").getJSONObject("overview"))
            assertThrows(Exception::class.java) { CloudStatus.parse(envelope.toString()) }
        }
        val extra = overviewEnvelope()
        extra.getJSONObject("result").put("time_estimate", JSONArray())
        assertThrows(Exception::class.java) { CloudStatus.parse(extra.toString()) }
        val wrongOverview = overviewEnvelope()
        wrongOverview.getJSONObject("result").put("overview", JSONArray())
        assertThrows(Exception::class.java) { CloudStatus.parse(wrongOverview.toString()) }
    }

    @Test fun clockAgesObservationsWithoutFabricatingFreshActivity() {
        assertEquals(0L, dashboardAge(999, 1000))
        assertEquals(29L, dashboardAge(30_999, 1000))
        assertFalse(dashboardDelayed(false, 30_999, 1000))
        assertTrue(dashboardDelayed(false, 31_000, 1000))
        assertTrue(dashboardDelayed(true, 1000, null))
        assertFalse(dashboardDelayed(false, 1000, null))
        assertEquals("1h 1m 1s", dashboardDuration(3661))
        assertEquals("0s", dashboardDuration(-1))
    }
}
