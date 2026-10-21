package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState

/**
 * Sample controller states and their mapped screen states, for previews and tests ONLY.
 * Every value here is a labelled fixture built on the frozen DTOs: nothing in this object
 * is wired into the working app, and no screen may read these as live data. The working
 * app maps real `AppState` values through [UiMapping.kt].
 */
object UiFixtures {

    const val LABEL = "FIXTURE — sample data, not a live app"

    private val comic = LibraryEntry("s1", "Sample Comic", LibraryKind.COMIC, 3, 3)
    private val novel = LibraryEntry("s2", "Sample Novel", LibraryKind.NOVELS, 5, 2)

    val coldStart = AppState(
        site = "tapas",
        signIn = SignInState.SignedOut,
        download = DownloadState.Held,
        library = emptyList(),
        notice = null,
    )

    val heldAfterRestart = coldStart.copy(
        signIn = SignInState.SignedIn("tapas"),
        library = listOf(comic, novel),
    )

    val meteredHold = heldAfterRestart.copy(
        download = DownloadState.Paused(BetaCodes.METERED_HOLD),
    )

    val running = heldAfterRestart.copy(
        download = DownloadState.Running(done = 34, total = 80),
    )

    val failed = heldAfterRestart.copy(
        download = DownloadState.Failed(BetaCodes.WORKER_OFFLINE),
    )

    val library = listOf(comic, novel).map { it.toSeriesUi() }

    val exportNotPicked = ExportPanelStateUi(ExportPhaseUi.NOT_PICKED)

    val exportRunning = ExportPanelStateUi(
        ExportPhaseUi.RUNNING,
        ExportProgressUi(verified = 2, failed = 0, alreadyVerified = 1, total = 9),
    )

    val exportGrantLost = ExportPanelStateUi(
        ExportPhaseUi.GRANT_LOST,
        ExportProgressUi(verified = 2, failed = 0, alreadyVerified = 1, total = 9),
        PublicIssue.EXPORT_INTERRUPTED,
    )

    val exportDone = ExportPanelStateUi(
        ExportPhaseUi.ALL_VERIFIED,
        ExportProgressUi(verified = 7, failed = 0, alreadyVerified = 2, total = 9),
    )

    val noticeSample = Notice(BetaCodes.STORAGE_LOW, "Not enough room to finish writing.")
}
