package io.github.bcrute.comicarchiver.controller

import kotlinx.coroutines.flow.StateFlow

/**
 * Frozen beta controller contract.
 *
 * Ownership: the phone UI/export line consumes this interface and never implements or
 * extends it; the native/build/runtime line owns the single runtime implementation and its
 * dependencies (PythonBridge, SignInEngine, DownloadHost, and the CoroutineScope it runs on).
 * [requestExport] takes the persisted ACTION_OPEN_DOCUMENT_TREE folder grant URI; the export
 * journal and verification belong to the UI/export line's exporter fed by export_manifest.
 */
interface AppController {
    val state: StateFlow<AppState>

    fun selectSite(key: String)
    fun startSignIn()
    fun cancelSignIn()
    fun signOut()

    /** The only action that moves downloads toward Running. */
    fun go()
    fun pause()
    /** A site's card acts on its own site, as each desktop card does, whichever site the other screens show. */
    fun signInSite(key: String) {}
    fun goSite(key: String) {}
    fun pauseSite(key: String) {}
    fun speedSite(key: String, value: String) {}
    /** Start or continue every site that can now; the others stay as they are. */
    fun goAll() {}
    /** Pause every site that is saving. */
    fun pauseAll() {}
    fun stop()

    fun refreshSettings()
    fun setWifiOnly(value: Boolean)
    fun setDownloadSpeed(value: String)
    fun setDownloadTypes(value: Map<String, Boolean>) {}
    /** Read a site's recorded series, or a series' episodes: the first page, then the next on each call. */
    fun loadEpisodes(site: String, seriesId: String? = null) {}
    /** Email support, as the desktop does: save the app's log as one file and open a new email to
     *  support with it attached; without an email app the log goes to the phone's Downloads. */
    fun emailSupport() {}
    /** Forget every list read for the Library, so each is read again when opened. */
    fun refreshEpisodes() {}
    fun loadCatalog(query: CatalogQuery, refresh: Boolean = false)
    fun openSavedFile(id: String)
    fun setTheme(value: AppTheme)
    fun refreshCloud()
    fun cloudAction(request: CloudRequest)

    fun requestExport(folderUri: String)
    fun cancelExport()
    fun openNotice(notice: Notice)
}
