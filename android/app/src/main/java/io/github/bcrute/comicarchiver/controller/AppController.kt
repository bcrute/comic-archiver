package io.github.bcrute.comicarchiver.controller

import kotlinx.coroutines.flow.StateFlow

/**
 * Frozen beta controller contract (docs/android-beta-contracts.md §1).
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
    fun stop()

    fun refreshSettings()
    fun setWifiOnly(value: Boolean)
    fun setDownloadSpeed(value: String)

    fun requestExport(folderUri: String)
    fun cancelExport()
    fun openNotice(notice: Notice)
}
