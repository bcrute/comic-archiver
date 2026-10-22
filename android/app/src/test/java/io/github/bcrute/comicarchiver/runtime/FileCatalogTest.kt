package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.FileCatalog
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class FileCatalogTest {
    private fun page() = JSONObject("""{"ok":true,"result":{
        "rows":[{"row_id":"${"a".repeat(64)}","open":"${"a".repeat(64)}","series":"Series","title":"One",
        "filename":"one.cbz","type":"Comic","size":"4 B","storage":"This device","saved_epoch":null,
        "file_time":true}],"total":1,"offset":0,"limit":50,"summary":"One saved episode.",
        "range":"Showing 1 of 1","notice":"Loaded","locations":[{"id":"device","label":"This device"}],
        "kinds":[{"key":"episodes","label":"Comics and novels"}],"sorts":[{"key":"series","label":"Series"}],
        "page_sizes":[50,100,200]}}""")

    @Test fun untrustedOpenIdentityAndImpossiblePageAreRefused() {
        val bad = page()
        bad.getJSONObject("result").getJSONArray("rows").getJSONObject(0).put("open", "b".repeat(64))
        assertThrows(IllegalArgumentException::class.java) { FileCatalog.parse(bad.toString()) }
        val short = page()
        short.getJSONObject("result").put("total", 0)
        assertThrows(IllegalArgumentException::class.java) { FileCatalog.parse(short.toString()) }
    }

    @Test fun unknownSaveTimeStaysUnknownAndDoesNotBecomeToday() {
        val value = FileCatalog.parse(page().toString())
        assertNull(value.rows.single().savedEpoch)
        assertTrue(value.rows.single().fileTime)
        assertEquals("a".repeat(64), value.rows.single().openId)
    }

    @Test fun metadataAndCloudRowsCanBeListedWithoutOfferingOpen() {
        val raw = page()
        val row = raw.getJSONObject("result").getJSONArray("rows").getJSONObject(0)
        row.put("row_id", JSONObject.NULL).put("open", JSONObject.NULL).put("storage", "Backblaze B2")
        assertNull(FileCatalog.parse(raw.toString()).rows.single().openId)
    }
}
