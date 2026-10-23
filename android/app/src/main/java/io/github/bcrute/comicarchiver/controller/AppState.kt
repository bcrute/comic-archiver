package io.github.bcrute.comicarchiver.controller

/** Frozen beta DTOs (docs/android-beta-contracts.md §1). The UI renders these; it never owns policy. */

data class AppState(
    val site: String,
    val signIn: SignInState,
    val download: DownloadState,
    val library: List<LibraryEntry>,
    val notice: Notice?,
    val settings: DownloadSettings? = null,
    val settingsBusy: Boolean = false,
    val settingsNotice: Notice? = null,
    val catalog: CatalogState = CatalogState(),
    val theme: AppTheme = AppTheme.SYSTEM,
    val cloud: CloudStatus? = null,
    val cloudBusy: Boolean = false,
    val cloudNotice: Notice? = null,
    val sites: List<SiteChoice> = emptyList(),
    val siteBusy: Boolean = false,
    val signInOrigin: String? = null,
    val signOutPending: Boolean = false,
    val libraryId: String = "default",
    val libraryBusy: Boolean = false,
    val workspaceReady: Boolean = true,
)

data class SiteChoice(val key: String, val name: String, val enabled: Boolean, val reason: String? = null,
                      val signInAvailable: Boolean = enabled)

enum class AppTheme(val label: String) { SYSTEM("System"), LIGHT("Light"), DARK("Dark") }

data class SpeedChoice(val key: String, val label: String, val available: Boolean)

data class DownloadSettings(
    val wifiOnly: Boolean,
    val speed: String,
    val speeds: List<SpeedChoice>,
    val connection: String,
    val metered: Boolean?,
    val allowed: Boolean,
)

sealed interface SignInState {
    data object SignedOut : SignInState

    /** Checking the saved WebView session without opening or navigating a sign-in page. */
    data object Restoring : SignInState

    /** The sign-in page currently shown; the URL is the site's own page. */
    data class Page(val url: String) : SignInState

    /** Waiting for the platform cookie store to settle after a step. */
    data object Settling : SignInState

    data class SignedIn(val site: String) : SignInState
    data class Failed(val code: String) : SignInState
}

sealed interface DownloadState {
    /**
     * The only state at cold start, after process death and after update. Every transition
     * into Running requires the go() action; nothing resumes on its own.
     */
    data object Held : DownloadState
    data object Planning : DownloadState
    data class Running(val done: Int, val total: Int, val description: String? = null,
                       val detail: String? = null) : DownloadState
    data class Paused(val reason: String) : DownloadState
    data class Finished(val summary: String) : DownloadState
    data class Failed(val code: String) : DownloadState
}

enum class LibraryKind { COMIC, NOVELS }

data class LibraryEntry(
    val seriesId: String,
    val title: String,
    val kind: LibraryKind,
    val episodeCount: Int,
    val savedCount: Int,
)

/** User-visible words only: no paths, hosts, tokens or stack traces. */
data class Notice(val code: String, val message: String)

/** Public error and hold words (contract §7); each maps to one notice and one recovery action. */
object BetaCodes {
    const val LOGIN_REQUIRED = "login_required"
    const val METERED_HOLD = "metered_hold"
    const val CONNECTION_UNVERIFIED = "connection_unverified"
    const val STORAGE_LOW = "storage_low"
    const val SOURCE_REFUSED = "source_refused"
    const val JOB_FAILED = "job_failed"
    const val EXPORT_INTERRUPTED = "export_interrupted"
    const val WORKER_OFFLINE = "worker_offline"
    const val UNSUPPORTED_SITE = "unsupported_site"
}
