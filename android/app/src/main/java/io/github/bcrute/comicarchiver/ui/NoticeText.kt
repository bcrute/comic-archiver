package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.export.ExportFailure
import io.github.bcrute.comicarchiver.words.say

/**
 * One plain-words notice and one recovery action per public issue, from the shared words
 * (notice.<code> and notice.<code>.action): no paths, hosts, codes-from-the-wire, or stack
 * traces are ever shown.
 */
data class IssueWords(val notice: String, val recoveryAction: String)

object NoticeText {

    fun wordsFor(issue: PublicIssue): IssueWords =
        IssueWords(say("notice.${issue.wireName}"), say("notice.${issue.wireName}.action"))
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
