package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.export.ExportSummary

/**
 * The beta's public issue codes, exactly the frozen contract's set (docs/
 * android-beta-contracts.md section 7): one UI notice and one recovery action each, and
 * none is ever reported as success.
 */
enum class PublicIssue(val wireName: String) {
    LOGIN_REQUIRED("login_required"),
    METERED_HOLD("metered_hold"),
    CONNECTION_UNVERIFIED("connection_unverified"),
    STORAGE_LOW("storage_low"),
    SOURCE_REFUSED("source_refused"),
    JOB_FAILED("job_failed"),
    EXPORT_INTERRUPTED("export_interrupted"),
    WORKER_OFFLINE("worker_offline"),
    UNSUPPORTED_SITE("unsupported_site");

    companion object {
        fun fromWireName(name: String): PublicIssue? =
            entries.firstOrNull { it.wireName == name }
    }
}

/**
 * A user-visible notice. `detail`, when present, is plain words for the person using the
 * app — never a path, host, token, URI or stack trace.
 */
data class NoticeUi(val issue: PublicIssue, val detail: String? = null)

/** Why downloads are held. Held is the only shape a cold start, restart or update can produce. */
enum class HoldReasonUi {
    /** The app has never been told to go. */
    NOT_STARTED,

    /** The person pressed Pause. */
    PAUSED_BY_USER,

    /** The network is metered or unknown; the beta only moves data unmetered. */
    METERED_OR_UNKNOWN_NETWORK,

    /** The connection could not be verified as the site's own. */
    CONNECTION_UNVERIFIED,

    /** The process restarted (death or update); nothing resumes without Go. */
    RESTARTED_HELD_FOR_GO,
}

/** The one transfer in flight, in plain words and coarse numbers. */
data class ActiveTransferUi(
    val label: String,
    val doneUnits: Int,
    val totalUnits: Int,
)

/**
 * What the downloads screen renders. The UI renders controller state; it owns no download
 * policy — Go, Pause, holds and admission all live in the controller.
 *
 * ADAPTATION SEAM (native line): map the frozen `com.comicarchive.app.controller`
 * AppController DTOs onto this class at assembly; this file must not grow policy.
 */
data class DownloadsStateUi(
    val signedIn: Boolean,
    val running: Boolean,
    val holdReason: HoldReasonUi?,
    val queueDepth: Int,
    val active: ActiveTransferUi?,
    val notice: NoticeUi?,
) {
    init {
        require(running == (holdReason == null)) {
            "exactly one of running or a hold reason must describe the state"
        }
    }

    companion object {
        /**
         * The only state an app start may construct: signed out or not, downloads held,
         * nothing in flight. Every transition into running is the Go action elsewhere.
         */
        fun coldStart(signedIn: Boolean = false): DownloadsStateUi = DownloadsStateUi(
            signedIn = signedIn,
            running = false,
            holdReason = HoldReasonUi.RESTARTED_HELD_FOR_GO,
            queueDepth = 0,
            active = null,
            notice = null,
        )
    }
}

/** One series row in the library: finished archives the controller reports. */
data class LibrarySeriesUi(
    val seriesId: String,
    val title: String,
    val episodeCount: Int,
    val cbzCount: Int,
    val epubCount: Int,
)

/** Where the export panel stands. No phase here means success on its own. */
enum class ExportPhaseUi {
    /** No export folder picked yet. */
    NOT_PICKED,

    /** A folder is picked; nothing running. */
    READY,

    /** Copying and verifying now. */
    RUNNING,

    /** The tree grant was revoked; the person must pick the folder again. */
    GRANT_LOST,

    /** Stopped partway (user stop, service limit, process death); resumable. */
    INTERRUPTED,

    /** Ran to the end but some files failed; nothing partial was called complete. */
    FINISHED_WITH_FAILURES,

    /** Every archive verified (this run or already, per the journal). */
    ALL_VERIFIED,
}

data class ExportProgressUi(
    val verified: Int,
    val failed: Int,
    val alreadyVerified: Int,
    val total: Int,
)

data class ExportPanelStateUi(
    val phase: ExportPhaseUi,
    val progress: ExportProgressUi? = null,
    val lastIssue: PublicIssue? = null,
) {
    companion object {
        /**
         * Renders an [ExportSummary] into panel state. A partial run is never flattened
         * into a success phase: interrupted or failing runs surface as recoverable phases.
         */
        fun fromSummary(summary: ExportSummary, total: Int): ExportPanelStateUi {
            val phase = when {
                summary.grantLost -> ExportPhaseUi.GRANT_LOST
                summary.interrupted -> ExportPhaseUi.INTERRUPTED
                summary.failed > 0 -> ExportPhaseUi.FINISHED_WITH_FAILURES
                else -> ExportPhaseUi.ALL_VERIFIED
            }
            return ExportPanelStateUi(
                phase = phase,
                progress = ExportProgressUi(
                    verified = summary.verified,
                    failed = summary.failed,
                    alreadyVerified = summary.alreadyVerified,
                    total = total,
                ),
                lastIssue = when (phase) {
                    ExportPhaseUi.GRANT_LOST, ExportPhaseUi.INTERRUPTED ->
                        PublicIssue.EXPORT_INTERRUPTED
                    ExportPhaseUi.FINISHED_WITH_FAILURES ->
                        summary.failures.lastOrNull()?.failure?.let(::publicIssueFor)
                            ?: PublicIssue.JOB_FAILED
                    else -> null
                },
            )
        }
    }
}
