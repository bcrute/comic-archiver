package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.export.ExportSummary
import io.github.bcrute.comicarchiver.controller.SpeedOption

/**
 * The beta's public issue codes, exactly the frozen contract's set — wire-identical to
 * controller `BetaCodes`: one UI notice and
 * one recovery action each, and none is ever reported as success.
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

/**
 * What the Downloads tab renders: the desktop's dashboard, derived from the controller's
 * `AppState` by [io.github.bcrute.comicarchiver.ui.toDownloadsScreenUi]. The saved library
 * over every site comes first, then one card for each site with its own buttons. The UI
 * owns no download policy — sign-in, Start, Pause, holds and admission all live in the
 * controller.
 */
data class DownloadsScreenUi(
    /** Saved episodes over every site, already worded; null before they are counted. */
    val savedEpisodes: String?,
    val cards: List<SiteCardUi>,
    val startAllEnabled: Boolean,
    val pauseAllVisible: Boolean,
    val notice: NoticeUi?,
)

/** What a card's one button does now, as on the desktop: signing in and saving never both need doing. */
enum class CardAction { SIGN_IN, GO, PAUSE }

/** One site's card, laid out as the desktop's: its state in a pill, what is saved, the estimates, speed and buttons. */
data class SiteCardUi(
    val site: String,
    val name: String,
    /** Which of the desktop's card palettes to draw with: neutral, active, complete, paused, waiting, attention. */
    val appearance: String,
    val pill: String,
    val saved: String,
    val timeLeft: String,
    val space: String,
    val rate: String,
    val status: String,
    val statusWarning: Boolean,
    /** The line under the status: what a sign-in or a change just answered. Blank when there is nothing to say. */
    val message: String,
    val messageError: Boolean,
    val selection: String,
    val selectionEnabled: Boolean,
    val action: CardAction,
    val actionText: String,
    val actionEnabled: Boolean,
    val signOut: Boolean,
    val signOutEnabled: Boolean,
    /** The chosen speed's key; blank before the site is signed in. */
    val speed: String,
    val speedEnabled: Boolean,
    val speeds: List<SpeedOption>,
    val listOnly: Boolean = false, val hasEpisodes: Boolean = false, val hasList: Boolean = false,
)

/** What the sign-in screen renders, derived from `AppState` by [toSignInScreenUi]. */
data class SignInScreenUi(
    /** A sign-in page or settle wait is in flight. */
    val busy: Boolean,
    val notice: NoticeUi?,
    val restoring: Boolean = false,
    val siteName: String = "Tapas",
    val origin: String? = null,
    val controlsEnabled: Boolean = true,
    /** The sign-in page could not be opened: the button offers to try again, as the desktop's does. */
    val retry: Boolean = false,
)

/** One series row in the library: what the controller reports as finished. */
data class LibrarySeriesUi(
    val seriesId: String,
    val title: String,
    /** The archive format this series exports as, shown as words ("CBZ" / "EPUB"). */
    val formatLabel: String,
    val episodeCount: Int,
    val savedCount: Int,
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
