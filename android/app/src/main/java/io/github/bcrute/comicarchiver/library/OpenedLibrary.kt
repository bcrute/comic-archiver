package io.github.bcrute.comicarchiver.library

import android.content.Context
import android.net.Uri
import android.os.SystemClock
import io.github.bcrute.comicarchiver.controller.CatalogQuery
import io.github.bcrute.comicarchiver.controller.CatalogState
import io.github.bcrute.comicarchiver.controller.FileCatalog
import io.github.bcrute.comicarchiver.runtime.PythonBridge
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.UUID
import kotlin.coroutines.coroutineContext

data class FolderLibraryState(val selected: Boolean = false, val label: String? = null,
    val choosing: Boolean = false, val needsFolder: Boolean = false,
    val catalog: CatalogState = CatalogState(), val message: String? = null)

/** A read-only library source independent of provider sign-in, Go and export destination.
 * Selection commits only after its complete catalog loads. Late results never replace
 * a newly chosen/closed folder, and a missing grant never becomes an empty library. */
internal class OpenedLibrary(context: Context, private val bridge: PythonBridge,
    private val scope: CoroutineScope,
    private val documents: PortableDocuments = ResolverPortableDocuments(context.applicationContext),
) {
    private val preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val cache = context.cacheDir.resolve("opened-library")
    @Volatile private var tree: Uri? = preferences.getString("tree", null)?.let(Uri::parse)
    private var selection = 0L
    private var request = 0L
    private var pickerGeneration = UUID.randomUUID().toString()
    private data class Cached(val tree: Uri, val value: PortableSnapshot, val file: File, val time: Long)
    @Volatile private var cached: Cached? = null
    private var pending: Job? = null
    private val _state = MutableStateFlow(FolderLibraryState(selected = tree != null,
        label = preferences.getString("label", null)))
    val state: StateFlow<FolderLibraryState> = _state

    init {
        // Only disposable public-fact caches; never a source document or private library.
        cache.listFiles()?.filter { it.name.matches(Regex("[a-f0-9-]{36}\\.json(?:\\.tmp)?")) }?.forEach { it.delete() }
    }

    fun ensureLoaded() { if (tree != null && _state.value.catalog.page == null && !_state.value.catalog.loading) load() }

    fun pickerTicket(): String = pickerGeneration

    fun select(uri: Uri, ticket: String? = null) {
        if (ticket != null && ticket != pickerGeneration) return
        pickerGeneration = UUID.randomUUID().toString()
        val chosen = ++selection
        val query = CatalogQuery()
        val prior = _state.value
        val version = ++request
        _state.value = prior.copy(choosing = true, message = null,
            catalog = prior.catalog.copy(loading = true, error = null, openingId = null))
        pending?.cancel()
        pending = scope.launch(Dispatchers.IO) {
            var candidate: Cached? = null
            try {
                documents.retain(uri)
                val loaded = scan(uri)
                candidate = loaded
                val page = page(loaded, query)
                withContext(Dispatchers.Main) {
                    if (selection != chosen || request != version) return@withContext
                    check(preferences.edit().putString("tree", uri.toString()).putString("label", loaded.value.label).commit())
                    cached?.file?.delete(); cached = loaded; tree = uri
                    _state.value = FolderLibraryState(selected = true, label = loaded.value.label,
                        catalog = CatalogState(query = query.copy(offset = page.offset), page = page))
                }
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) {
                withContext(Dispatchers.Main) {
                    if (selection == chosen && request == version) _state.value = prior.copy(choosing = false,
                        message = "The folder could not be opened. Choose an available Library or series folder.",
                        catalog = prior.catalog.copy(loading = false, openingId = null))
                }
            } finally { if (cached !== candidate) candidate?.file?.delete() }
        }
    }

    private suspend fun scan(uri: Uri): Cached {
        coroutineContext.ensureActive()
        val activity = coroutineContext
        val value = PortableFolderScanner(documents).scan(uri) { activity.ensureActive() }
        cache.mkdirs()
        val target = cache.resolve(UUID.randomUUID().toString() + ".json")
        val scratch = cache.resolve(target.name + ".tmp")
        try {
            val body = value.manifest.toString().toByteArray(Charsets.UTF_8)
            if (body.size > 64 * 1024 * 1024) throw IOException("Catalog exceeds limit")
            scratch.writeBytes(body)
            check(scratch.renameTo(target))
            return Cached(uri, value, target.canonicalFile, SystemClock.elapsedRealtime())
        } finally { scratch.delete() }
    }

    private fun page(source: Cached, query: CatalogQuery) = FileCatalog.parse(bridge.dispatch("portable_catalog",
        JSONObject().put("manifest_path", source.file.absolutePath).put("options", query.json(false)).toString()))

    fun load(query: CatalogQuery = _state.value.catalog.query, refresh: Boolean = false) {
        val selected = tree ?: return
        if (_state.value.choosing) return
        if (_state.value.catalog.loading && query == _state.value.catalog.query && !refresh) return
        val chosen = selection; val version = ++request
        _state.value = _state.value.copy(message = null, catalog = _state.value.catalog.copy(query = query,
            loading = true, error = null, feedback = null, feedbackFor = null))
        pending?.cancel()
        pending = scope.launch(Dispatchers.IO) {
            var source: Cached? = null
            try {
                check(documents.readable(selected))
                val loaded = cached?.takeIf { it.tree == selected && !refresh && SystemClock.elapsedRealtime() - it.time < 15_000 }
                    ?: scan(selected)
                source = loaded
                val page = page(loaded, query)
                withContext(Dispatchers.Main) {
                    if (selection != chosen || version != request || tree != selected) return@withContext
                    if (cached !== loaded) cached?.file?.delete()
                    cached = loaded
                    _state.value = _state.value.copy(needsFolder = false, catalog = CatalogState(
                        query = query.copy(offset = page.offset, limit = page.limit), page = page))
                }
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) {
                val missing = try { !documents.readable(selected) } catch (_: Exception) { true }
                withContext(Dispatchers.Main) {
                    if (selection == chosen && version == request && tree == selected) {
                        _state.value = _state.value.copy(needsFolder = missing, catalog = _state.value.catalog.copy(
                            loading = false, error = if (missing) "Folder access was lost. Choose the folder again."
                            else "The folder could not be read. Refresh files or choose an available folder."))
                    }
                }
            } finally { if (source !== cached) source?.file?.delete() }
        }
    }

    fun close() {
        selection++; request++
        pickerGeneration = UUID.randomUUID().toString()
        pending?.cancel(); pending = null
        tree = null; cached?.file?.delete(); cached = null
        preferences.edit().clear().apply()
        _state.value = FolderLibraryState()
        // Do not release a shared system grant here: it may also serve the separate export
        // destination. The next start releases it if nothing records it any more.
    }

    fun open(id: String) {
        val source = cached ?: return
        if (_state.value.choosing || _state.value.catalog.loading || _state.value.catalog.openingId != null) return
        if (_state.value.catalog.page?.rows?.none { it.openId == id } != false) return
        val document = source.value.documents[id] ?: return
        val chosen = selection
        _state.value = _state.value.copy(catalog = _state.value.catalog.copy(openingId = id,
            feedback = "Preparing a reading copy…", feedbackFor = id))
        pending?.cancel()
        pending = scope.launch(Dispatchers.IO) {
            var prepared: PreparedPortableOpen? = null
            var launched = false
            try {
                val activity = coroutineContext
                if (documents.current(source.tree, document)) prepared = documents.prepare(source.tree, document) { activity.ensureActive() }
                withContext(Dispatchers.Main) {
                    if (chosen != selection || tree != source.tree || cached !== source) return@withContext
                    val result = try { prepared?.launch() ?: ArchiveOpenResult.UNAVAILABLE }
                        catch (_: Exception) { ArchiveOpenResult.UNAVAILABLE }
                    launched = result == ArchiveOpenResult.REQUESTED
                    val message = when (result) {
                        ArchiveOpenResult.REQUESTED -> "Sent to your device's reader."
                        ArchiveOpenResult.NO_READER -> "No app could open this file. Choose a CBZ or EPUB reader and try again."
                        else -> "This file changed or is unavailable. Refresh files or choose the folder again."
                    }
                    _state.value = _state.value.copy(catalog = _state.value.catalog.copy(
                        openingId = null, feedback = message, feedbackFor = id))
                }
            } catch (cancelled: CancellationException) { throw cancelled }
              catch (_: Exception) {
                withContext(Dispatchers.Main) {
                    if (chosen == selection && tree == source.tree && cached === source)
                        _state.value = _state.value.copy(catalog = _state.value.catalog.copy(openingId = null,
                            feedback = "The reading copy could not be prepared. Free some space, refresh files or choose the folder again.",
                            feedbackFor = id))
                }
            } finally { if (!launched) prepared?.discard() }
        }
    }

    companion object {
        private const val PREFS = "comicarchiver_opened_library"

        /** The folder this library records, which startup keeps its permission for. */
        internal fun recordedTree(context: Context): String? =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("tree", null)
    }
}
