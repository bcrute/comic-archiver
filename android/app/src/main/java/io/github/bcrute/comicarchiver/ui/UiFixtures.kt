package io.github.bcrute.comicarchiver.ui

/**
 * Sample states for previews and tests ONLY. Every value here is a labelled fixture:
 * nothing in this object is wired into the working app, and no screen may read these as
 * live data. The working app renders controller state supplied by app assembly.
 */
object UiFixtures {

    const val LABEL = "FIXTURE — sample data, not a live app"

    val signedOut = DownloadsStateUi.coldStart()

    val heldAfterRestart = DownloadsStateUi.coldStart(signedIn = true).copy(
        queueDepth = 12,
        notice = NoticeUi(PublicIssue.WORKER_OFFLINE),
    )

    val meteredHold = DownloadsStateUi(
        signedIn = true,
        running = false,
        holdReason = HoldReasonUi.METERED_OR_UNKNOWN_NETWORK,
        queueDepth = 4,
        active = null,
        notice = null,
    )

    val running = DownloadsStateUi(
        signedIn = true,
        running = true,
        holdReason = null,
        queueDepth = 27,
        active = ActiveTransferUi("Episode 12", doneUnits = 34, totalUnits = 80),
        notice = null,
    )

    val librarySeries = listOf(
        LibrarySeriesUi("s1", "Sample Comic", episodeCount = 3, cbzCount = 3, epubCount = 0),
        LibrarySeriesUi("s2", "Sample Novel", episodeCount = 5, cbzCount = 0, epubCount = 5),
    )

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
}
