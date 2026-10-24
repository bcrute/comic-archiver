package io.github.bcrute.comicarchiver.controller

import org.json.JSONArray
import org.json.JSONObject

data class CatalogQuery(val query: String = "", val location: String = "all", val kind: String = "episodes",
                        val sort: String = "saved_desc", val offset: Int = 0, val limit: Int = 50) {
    fun json(refresh: Boolean) = JSONObject().put("query", query).put("location", location)
        .put("kind", kind).put("sort", sort).put("offset", offset).put("limit", limit).put("refresh", refresh)
}
data class CatalogChoice(val key: String, val label: String)
data class SavedFileItem(val key: String, val series: String, val title: String, val filename: String,
                         val type: String, val size: String, val storage: String, val savedEpoch: Double?,
                         val fileTime: Boolean, val openId: String?, val b2Copy: Boolean = false)
data class SavedFilePage(val rows: List<SavedFileItem>, val total: Int, val offset: Int, val limit: Int,
                         val summary: String, val range: String, val notice: String,
                         val locations: List<CatalogChoice>, val kinds: List<CatalogChoice>,
                         val sorts: List<CatalogChoice>, val pageSizes: List<Int>)
data class CatalogState(val query: CatalogQuery = CatalogQuery(), val page: SavedFilePage? = null,
                        val loading: Boolean = false, val error: String? = null,
                        val openingId: String? = null, val feedback: String? = null,
                        val feedbackFor: String? = null)

object FileCatalog {
    fun parse(envelope: String): SavedFilePage {
        val answer = JSONObject(envelope)
        require(answer.optBoolean("ok"))
        val value = answer.getJSONObject("result")
        val total = value.getInt("total")
        val offset = value.getInt("offset")
        val limit = value.getInt("limit")
        require(total >= 0 && offset >= 0 && limit in 1..200)
        val rows = value.getJSONArray("rows")
        val items = (0 until rows.length()).map { index ->
            val row = rows.getJSONObject(index)
            val id = row.optString("row_id").takeIf { it.matches(Regex("[a-f0-9]{64}")) }
            val open = row.optString("open").takeIf { it.matches(Regex("[a-f0-9]{64}")) }
            require(open == null || open == id)
            val b2Copy = row.optBoolean("b2_copy")
            require(!b2Copy || open == null)
            val epoch = (row.opt("saved_epoch") as? Number)?.toDouble()?.takeIf { it.isFinite() }
            SavedFileItem(id ?: "row-$index", row.getString("series"), row.getString("title"),
                row.getString("filename"), row.getString("type"), row.getString("size"),
                row.getString("storage"), epoch, row.optBoolean("file_time"), open, b2Copy)
        }
        require(items.size <= limit && offset + items.size <= total)
        require(items.map { it.key }.distinct().size == items.size)
        fun choices(array: JSONArray, locations: Boolean = false): List<CatalogChoice> =
            (0 until array.length()).map { index ->
                val item = array.getJSONObject(index)
                CatalogChoice(item.getString(if (locations) "id" else "key"), item.getString("label"))
            }.also { require(it.map { choice -> choice.key }.distinct().size == it.size) }
        val sizes = value.getJSONArray("page_sizes")
        val pageSizes = (0 until sizes.length()).map { sizes.getInt(it) }
        require(pageSizes.isNotEmpty() && pageSizes.all { it in 1..200 })
        return SavedFilePage(items, total, offset, limit, value.getString("summary"), value.getString("range"),
            value.getString("notice"), choices(value.getJSONArray("locations"), true),
            choices(value.getJSONArray("kinds")), choices(value.getJSONArray("sorts")), pageSizes)
    }
}
