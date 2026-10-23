package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.export.ExportFailure

/**
 * One plain-words notice and one recovery action per public issue, exactly as the frozen
 * contract requires (section 7). These strings are the app's whole vocabulary for
 * failures: no paths, hosts, codes-from-the-wire, or stack traces are ever shown.
 */
data class IssueWords(val notice: String, val recoveryAction: String)

object NoticeText {

    private val WORDS: Map<PublicIssue, IssueWords> = mapOf(
        PublicIssue.LOGIN_REQUIRED to IssueWords(
            "Sign-in is needed before anything can be saved.",
            "Sign in to the site again.",
        ),
        PublicIssue.METERED_HOLD to IssueWords(
            "This network may be metered, so saving is paused.",
            "Connect to Wi-Fi or an unmetered network to continue.",
        ),
        PublicIssue.CONNECTION_UNVERIFIED to IssueWords(
            "The connection could not be verified as the site's own.",
            "Try again; if it keeps failing, sign in again.",
        ),
        PublicIssue.STORAGE_LOW to IssueWords(
            "There may not be enough space to finish writing.",
            "Free up space in the export folder and try again.",
        ),
        PublicIssue.SOURCE_REFUSED to IssueWords(
            "A saved file could not be read back.",
            "Try again; if it keeps failing, save that item again.",
        ),
        PublicIssue.JOB_FAILED to IssueWords(
            "Something did not finish.",
            "Try again.",
        ),
        PublicIssue.EXPORT_INTERRUPTED to IssueWords(
            "Export stopped before every file was verified.",
            "Pick the export folder again and continue; finished files are kept.",
        ),
        PublicIssue.WORKER_OFFLINE to IssueWords(
            "The saving engine is not running.",
            "Press Go to start it again.",
        ),
        PublicIssue.UNSUPPORTED_SITE to IssueWords(
            "This site's sign-in is unavailable on this device.",
            "Choose a supported site or update Android System WebView; saved files are kept.",
        ),
    )

    fun wordsFor(issue: PublicIssue): IssueWords =
        WORDS[issue] ?: IssueWords("Something did not finish.", "Try again.")
}

/**
 * Maps the exporter's internal failure codes onto the public issue set. Total function:
 * every internal code has exactly one public issue, so new internal codes must be added
 * here (enforced by the exhaustive `when`).
 */
fun publicIssueFor(failure: ExportFailure): PublicIssue = when (failure) {
    ExportFailure.VERIFICATION_MISMATCH -> PublicIssue.JOB_FAILED
    ExportFailure.SOURCE_REFUSED -> PublicIssue.SOURCE_REFUSED
    ExportFailure.TARGET_REFUSED -> PublicIssue.JOB_FAILED
    ExportFailure.STORAGE_LOW -> PublicIssue.STORAGE_LOW
    ExportFailure.GRANT_LOST -> PublicIssue.EXPORT_INTERRUPTED
    ExportFailure.INTERRUPTED -> PublicIssue.EXPORT_INTERRUPTED
    ExportFailure.MANIFEST_INVALID -> PublicIssue.JOB_FAILED
}

/**
 * A public wire code (bridge envelope or controller) to its issue; an unknown code still
 * renders, as job_failed — never as success and never with the raw code shown.
 */
fun exportIssueFromWireCode(code: String): PublicIssue =
    PublicIssue.fromWireName(code) ?: PublicIssue.JOB_FAILED
