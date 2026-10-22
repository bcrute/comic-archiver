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

fun AppState.toSignInScreenUi(): SignInScreenUi = SignInScreenUi(
    busy = signIn is SignInState.Page || signIn is SignInState.Settling,
    restoring = signIn is SignInState.Restoring,
    notice = (signIn as? SignInState.Failed)?.let {
        NoticeUi(PublicIssue.fromWireName(it.code) ?: PublicIssue.JOB_FAILED)
    } ?: notice?.toNoticeUi(),
)

fun AppState.toDownloadsScreenUi(): DownloadsScreenUi = DownloadsScreenUi(
    signedIn = signIn is SignInState.SignedIn,
    download = downloadView(),
    remaining = library.sumOf { (it.episodeCount - it.savedCount).coerceAtLeast(0) },
    notice = notice?.toNoticeUi()
        ?: (download as? DownloadState.Failed)?.let {
            NoticeUi(PublicIssue.fromWireName(it.code) ?: PublicIssue.JOB_FAILED)
        },
)

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
