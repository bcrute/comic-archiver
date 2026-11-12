package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState

/**
 * The whole adaptation between the frozen controller DTOs and the screens: `AppState`
 * flows in, screen states flow out, actions flow back through `AppController` at
 * assembly. Every policy decision stays in the controller — these functions only
 * translate shapes and words.
 */

/** Controller `Notice` (code + public words) → screen notice. Unknown codes still render. */
fun Notice.toNoticeUi(): NoticeUi = NoticeUi(
    issue = PublicIssue.fromWireName(code) ?: PublicIssue.JOB_FAILED,
    detail = message.takeIf { it.isNotBlank() },
)

/** The desktop's words for a sign-in to an account the library was not saved from. */
fun accountMismatchText(site: String) =
    "That $site account does not match this saved library. Sign in to the original account."

fun AppState.toSignInScreenUi(): SignInScreenUi = SignInScreenUi(
    busy = signIn is SignInState.Page || signIn is SignInState.Settling,
    restoring = signIn is SignInState.Restoring,
    siteName = sites.firstOrNull { it.key == site }?.name ?: if (site == "tapas") "Tapas" else site,
    origin = signInOrigin,
    controlsEnabled = workspaceReady && !libraryBusy && !siteBusy && !signOutPending && sites.firstOrNull { it.key == site }?.signInAvailable != false,
    notice = (signIn as? SignInState.Failed)?.let {
        if (it.code == "account_mismatch") NoticeUi(PublicIssue.LOGIN_REQUIRED, accountMismatchText(
            sites.firstOrNull { choice -> choice.key == site }?.name ?: if (site == "tapas") "Tapas" else site))
        else NoticeUi(PublicIssue.fromWireName(it.code) ?: PublicIssue.JOB_FAILED)
    } ?: notice?.toNoticeUi(),
)

fun AppState.toDownloadsScreenUi(): DownloadsScreenUi = DownloadsScreenUi(
    signedIn = signIn is SignInState.SignedIn,
    controlsEnabled = workspaceReady && !libraryBusy && !siteBusy && !signOutPending && !startingAll,
    siteName = sites.firstOrNull { it.key == site }?.name ?: if (site == "tapas") "Tapas" else site,
    allSites = allSitesCard(),
    siteCards = siteCards.sortedByDescending { it.site == site },
    observations = observations,
    statusReadFailed = statusReadFailed,
    admittedAtMillis = admittedAtMillis,
    cloud = cloud,
    cloudCheckedAtMillis = cloudCheckedAtMillis,
    cloudReadFailed = cloudReadFailed,
    download = downloadView(),
    remaining = library.sumOf { (it.episodeCount - it.savedCount).coerceAtLeast(0) },
    notice = notice?.toNoticeUi()
        ?: (download as? DownloadState.Failed)?.let {
            NoticeUi(PublicIssue.fromWireName(it.code) ?: PublicIssue.JOB_FAILED)
        },
)

private fun AppState.allSitesCard(): AllSitesCardUi? {
    val offered = sites.filter { it.enabled }
    if (offered.size < 2) return null
    val counts = allSites?.let { counted ->
        val missing = offered.filter { it.key !in counted.sites }.map { it.name }
        "${counted.episodes} saved ${if (counted.episodes == 1L) "episode" else "episodes"} · " +
            "${counted.files} ${if (counted.files == 1L) "file" else "files"}" +
            if (missing.isEmpty()) "" else " (not counted: ${missing.joinToString(", ")})"
    }
    // The shown site's own state is the newest word on it; the others carry their mark.
    val shownSaving = download is DownloadState.Planning || download is DownloadState.Running
    val saving = offered.associate { it.key to if (it.key == site) shownSaving else it.saving }
    return AllSitesCardUi(counts, offered.map { "${it.name}: ${if (saving[it.key] == true) "Saving" else "Not saving"}" },
        saving.values.any { it }, startingAll)
}

private fun AppState.downloadView(): DownloadsViewUi = when (val d = download) {
    DownloadState.Held -> DownloadsViewUi.Held(HoldReasonUi.NOT_STARTED)
    DownloadState.Planning -> DownloadsViewUi.Running(doneUnits = 0, totalUnits = 0, description = "Starting…")
    is DownloadState.Running -> DownloadsViewUi.Running(d.done, d.total, d.description, d.detail)
    is DownloadState.Paused -> DownloadsViewUi.Held(holdReasonOf(d.reason))
    is DownloadState.Finished -> DownloadsViewUi.Finished(d.summary)
    is DownloadState.Failed -> DownloadsViewUi.Held(HoldReasonUi.FAILED)
}

/** Pause reasons arrive as public codes; map the two known holds, default to user-paused. */
private fun holdReasonOf(reason: String): HoldReasonUi = when (reason) {
    BetaCodes.METERED_HOLD -> HoldReasonUi.METERED_OR_UNKNOWN_NETWORK
    BetaCodes.CONNECTION_UNVERIFIED -> HoldReasonUi.CONNECTION_UNVERIFIED
    else -> HoldReasonUi.PAUSED_BY_USER
}

fun LibraryEntry.toSeriesUi(): LibrarySeriesUi = LibrarySeriesUi(
    seriesId = seriesId,
    title = title,
    formatLabel = when (kind) {
        LibraryKind.COMIC -> "CBZ"
        LibraryKind.NOVELS -> "EPUB"
    },
    episodeCount = episodeCount,
    savedCount = savedCount,
)

fun AppState.toLibraryUi(): List<LibrarySeriesUi> = library.map { it.toSeriesUi() }
