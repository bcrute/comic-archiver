package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.EpisodeRow
import io.github.bcrute.comicarchiver.controller.EpisodeList
import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.CardRow
import io.github.bcrute.comicarchiver.controller.IssueCount
import io.github.bcrute.comicarchiver.controller.TypeCount
import io.github.bcrute.comicarchiver.controller.SiteCard
import io.github.bcrute.comicarchiver.controller.SiteChoice
import io.github.bcrute.comicarchiver.controller.SpeedOption
import io.github.bcrute.comicarchiver.controller.AllSites
import org.json.JSONObject
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.export.ExportFailure
import io.github.bcrute.comicarchiver.export.ExportSummary
import io.github.bcrute.comicarchiver.export.JournalEntry
import io.github.bcrute.comicarchiver.export.JournalState
import io.github.bcrute.comicarchiver.export.ExportKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Focused JVM tests for the UI vocabulary and the controller→screen mapping: the frozen
 * issue set, one notice + one recovery action per issue, the Held-only cold start, every
 * download DTO shape, and summary-to-panel mapping that never flattens a partial run
 * into success.
 */
class UiMappingTest {

    private val comic = LibraryEntry("s1", "Sample Comic", LibraryKind.COMIC, 3, 3)
    private val novel = LibraryEntry("s2", "Sample Novel", LibraryKind.NOVELS, 5, 2)

    private fun state(
        signIn: SignInState = SignInState.SignedIn("tapas"),
        download: DownloadState = DownloadState.Held,
        library: List<LibraryEntry> = listOf(comic, novel),
        notice: Notice? = null,
    ) = AppState("tapas", signIn, download, library, notice)

    @Test
    fun aSignInToAnotherAccountIsRefusedInTheDesktopsWords() {
        val ui = UiFixtures.coldStart.copy(signIn = SignInState.Failed("account_mismatch")).toSignInScreenUi()
        assertEquals(NoticeUi(PublicIssue.LOGIN_REQUIRED,
            "That Tapas account does not match this saved library. Sign in to the original account, " +
                "or choose a different library folder to save this account separately."), ui.notice)
        assertEquals(NoticeUi(PublicIssue.LOGIN_REQUIRED, "Tapas signed you out"),
            UiFixtures.coldStart.copy(signIn = SignInState.Failed("login_required")).toSignInScreenUi().notice)
        val signedOut = UiFixtures.coldStart.copy(download = DownloadState.Failed(BetaCodes.LOGIN_REQUIRED))
        assertEquals(null, signedOut.toDownloadsScreenUi().notice)
        assertEquals(NoticeUi(PublicIssue.JOB_FAILED),
            UiFixtures.coldStart.copy(download = DownloadState.Failed(BetaCodes.JOB_FAILED)).toDownloadsScreenUi().notice)
    }

    @Test
    fun aSignInThatEndedSaysWhatTheDesktopSaysAndGoingBackSaysNothing() {
        fun notice(code: String) = UiFixtures.coldStart.copy(signIn = SignInState.Failed(code)).toSignInScreenUi().notice
        assertEquals(null, notice("signin_cancelled"))
        assertEquals(NoticeUi(PublicIssue.JOB_FAILED, "Sign-in was not completed. Choose Sign in to Tapas to try again."),
            notice("signin_incomplete"))
        assertEquals(NoticeUi(PublicIssue.JOB_FAILED, "Sign-in did not finish. Try Sign in to Tapas again."),
            notice("signin_unavailable"))
        assertEquals(NoticeUi(PublicIssue.UNSUPPORTED_SITE), notice("unsupported_site"))
        val unreachable = UiFixtures.coldStart.copy(signIn = SignInState.Failed("signin_unreachable")).toSignInScreenUi()
        assertEquals(NoticeUi(PublicIssue.JOB_FAILED,
            "The Tapas sign-in page could not be opened.\n\nCheck your internet connection, then choose \"Try again\"."),
            unreachable.notice)
        assertEquals(true, unreachable.retry)
        assertEquals(false, UiFixtures.coldStart.copy(signIn = SignInState.Failed("signin_incomplete")).toSignInScreenUi().retry)
    }

    @Test
    fun aCardSaysWhatTheDesktopsCardSaysAndItsButtonFollowsTheControllerForTheSiteOnScreen() {
        val rows = listOf(CardRow("Status", "Checking and saving your episodes", false), CardRow("Saved", "1 episode", false),
            CardRow("Time left", "~8 hours", false), CardRow("Space", "~4.8 GB", false), CardRow("Speed", "Fast", false),
            CardRow("Download speed", "\u2014", false))
        val read = SiteCard("tapas", "Tapas", "Saving your episodes", "Working", "active", "", rows, download = "running",
            pill = "Working", action = "pause", actionText = "Pause", actionEnabled = true, signOut = true,
            speed = "fast", speedEnabled = true, speeds = listOf(SpeedOption("fast", "Fast")), selection = "Download: 2 types")
        val sites = listOf(SiteChoice("tapas", "Tapas", true), SiteChoice("webtoon", "Webtoon", true, saving = true),
            SiteChoice("tappytoon", "Tappytoon", false))
        val ui = state(download = DownloadState.Paused("user")).copy(sites = sites, siteCards = listOf(read),
            allSites = AllSites(listOf("tapas"), 1234567, 9)).toDownloadsScreenUi()
        assertEquals("1,234,567", ui.savedEpisodes)
        // A site the app cannot offer has no card.
        assertEquals(listOf("tapas", "webtoon"), ui.cards.map { it.site })
        val tapas = ui.cards[0]
        assertEquals(listOf("active", "Working", "1", "~8 hours", "~4.8 GB", "\u2014", "Checking and saving your episodes",
            "Download: 2 types", "fast"), listOf(tapas.appearance, tapas.pill, tapas.saved, tapas.timeLeft, tapas.space,
            tapas.rate, tapas.status, tapas.selection, tapas.speed))
        // Pause was just pressed: the card read before it still says Pause, the button does not.
        assertEquals(CardAction.GO to "Continue", tapas.action to tapas.actionText)
        assertTrue(tapas.actionEnabled && tapas.signOut && tapas.signOutEnabled && tapas.speedEnabled && tapas.selectionEnabled)
        // A site saving while another is shown offers Pause and keeps its sign-in until it is paused;
        // one not read yet has the desktop's empty card.
        val webtoon = ui.cards[1]
        assertEquals(CardAction.PAUSE to "Pause", webtoon.action to webtoon.actionText)
        assertFalse(webtoon.signOutEnabled || webtoon.speedEnabled)
        assertTrue(webtoon.selectionEnabled)
        assertEquals(listOf("neutral", "Not signed in", "\u2014", "Download: All types"),
            listOf(webtoon.appearance, webtoon.pill, webtoon.saved, webtoon.selection))
        assertTrue(ui.pauseAllVisible && ui.startAllEnabled)
        val idle = state().toDownloadsScreenUi()
        assertNull(idle.savedEpisodes)
        assertFalse(idle.pauseAllVisible)
        // Nothing can be pressed while the library is being changed.
        val busy = state().copy(libraryBusy = true).toDownloadsScreenUi()
        assertFalse(busy.startAllEnabled || busy.cards.single().actionEnabled)
        // Another site's card is that site's own word, and a site known to be saving offers Pause whatever it read.
        val other = read.copy(site = "webtoon", action = "go", actionText = "Start")
        assertEquals(CardAction.PAUSE, state().copy(sites = sites, siteCards = listOf(other)).toDownloadsScreenUi().cards[1].action)
        val ready = state().copy(sites = sites.map { it.copy(saving = false) }, siteCards = listOf(other)).toDownloadsScreenUi().cards[1]
        assertEquals(CardAction.GO to "Start", ready.action to ready.actionText)
        assertTrue(ready.signOut && ready.signOutEnabled)
        // A card that says to sign in is a site that is not signed in, whatever else it carries.
        val out = state().copy(sites = sites.map { it.copy(saving = false) },
            siteCards = listOf(other.copy(action = "signin", actionText = "Sign in"))).toDownloadsScreenUi().cards[1]
        assertEquals(CardAction.SIGN_IN to "Sign in", out.action to out.actionText)
    }

    @Test
    fun restoring_session_shows_status_without_mounting_a_blank_signin_page() {
        val ui = UiFixtures.coldStart.copy(signIn = SignInState.Restoring).toSignInScreenUi()
        assertTrue(ui.restoring)
        assertFalse(ui.busy)
        val card = UiFixtures.coldStart.copy(signIn = SignInState.Restoring).toDownloadsScreenUi().cards.single()
        assertEquals(CardAction.SIGN_IN to false, card.action to card.actionEnabled)
        assertEquals("Sign in" to "Checking saved sign-in\u2026", card.actionText to card.message)
    }

    @Test
    fun `the public issue set is exactly the frozen contract set`() {
        assertEquals(
            setOf(
                "login_required", "metered_hold", "connection_unverified", "storage_low",
                "source_refused", "job_failed", "export_interrupted", "worker_offline",
                "unsupported_site",
            ),
            PublicIssue.entries.map { it.wireName }.toSet(),
        )
    }

    @Test
    fun `every issue has one non-empty notice and one recovery action`() {
        for (issue in PublicIssue.entries) {
            val words = NoticeText.wordsFor(issue)
            assertTrue("notice blank for ${issue.wireName}", words.notice.isNotBlank())
            assertTrue("action blank for ${issue.wireName}", words.recoveryAction.isNotBlank())
        }
    }

    @Test
    fun `cold start offers sign in, not a download`() {
        val card = UiFixtures.coldStart.toDownloadsScreenUi().cards.single()
        assertEquals(CardAction.SIGN_IN, card.action)
        assertFalse(card.signOut)
    }

    @Test
    fun `planning offers pause, never go`() {
        assertEquals(CardAction.PAUSE, state(download = DownloadState.Planning).toDownloadsScreenUi().cards.single().action)
    }

    @Test
    fun `a paused download offers continue whatever paused it`() {
        for (reason in listOf(BetaCodes.METERED_HOLD, BetaCodes.CONNECTION_UNVERIFIED, "anything_else")) {
            val card = state(download = DownloadState.Paused(reason)).toDownloadsScreenUi().cards.single()
            assertEquals(CardAction.GO to "Continue", card.action to card.actionText)
        }
    }

    @Test
    fun `a finished download offers start again`() {
        val card = state(download = DownloadState.Finished("37 items saved")).toDownloadsScreenUi().cards.single()
        assertEquals(CardAction.GO to "Start", card.action to card.actionText)
    }

    @Test
    fun `a failed download surfaces a notice and stays held`() {
        val ui = state(download = DownloadState.Failed(BetaCodes.WORKER_OFFLINE))
            .toDownloadsScreenUi()
        assertEquals(CardAction.GO, ui.cards.single().action)
        assertEquals(PublicIssue.WORKER_OFFLINE, ui.notice?.issue)
    }

    @Test
    fun `an unknown code still renders, as job_failed`() {
        val ui = state(notice = Notice("something_new", "Words.")).toDownloadsScreenUi()
        assertEquals(PublicIssue.JOB_FAILED, ui.notice?.issue)
        assertEquals("Words.", ui.notice?.detail)
    }

    @Test
    fun `sign-in screen derives busy and failure notice from sign-in state`() {
        val idle = state(signIn = SignInState.SignedOut).toSignInScreenUi()
        assertFalse(idle.busy)
        assertNull(idle.notice)

        val busy = state(signIn = SignInState.Settling).toSignInScreenUi()
        assertTrue(busy.busy)

        val failed = state(signIn = SignInState.Failed(BetaCodes.LOGIN_REQUIRED))
            .toSignInScreenUi()
        assertEquals(PublicIssue.LOGIN_REQUIRED, failed.notice?.issue)
    }

    @Test
    fun `library rows label the format from the controller kind`() {
        val rows = state().toLibraryUi()
        assertEquals("CBZ", rows[0].formatLabel)
        assertEquals("EPUB", rows[1].formatLabel)
        assertEquals(5, rows[1].episodeCount)
        assertEquals(2, rows[1].savedCount)
    }

    @Test
    fun `every internal export failure maps to exactly one public issue`() {
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.VERIFICATION_MISMATCH))
        assertEquals(PublicIssue.SOURCE_REFUSED, publicIssueFor(ExportFailure.SOURCE_REFUSED))
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.TARGET_REFUSED))
        assertEquals(PublicIssue.STORAGE_LOW, publicIssueFor(ExportFailure.STORAGE_LOW))
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, publicIssueFor(ExportFailure.GRANT_LOST))
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, publicIssueFor(ExportFailure.INTERRUPTED))
        assertEquals(PublicIssue.JOB_FAILED, publicIssueFor(ExportFailure.MANIFEST_INVALID))
    }

    @Test
    fun `summary mapping never calls an interrupted run complete`() {
        val summary = ExportSummary(
            verified = 3, failed = 0, alreadyVerified = 2,
            failures = emptyList(), interrupted = true, grantLost = false,
        )
        val panel = ExportPanelStateUi.fromSummary(summary, total = 9)
        assertEquals(ExportPhaseUi.INTERRUPTED, panel.phase)
        assertEquals(PublicIssue.EXPORT_INTERRUPTED, panel.lastIssue)
    }

    @Test
    fun `grant loss maps to a re-pick phase`() {
        val summary = ExportSummary(
            verified = 1, failed = 0, alreadyVerified = 0,
            failures = emptyList(), interrupted = true, grantLost = true,
        )
        assertEquals(ExportPhaseUi.GRANT_LOST, ExportPanelStateUi.fromSummary(summary, 9).phase)
    }

    @Test
    fun `failures surface with the failed count and their public issue`() {
        val failedEntry = JournalEntry(
            episodeId = "ep1", seriesId = "s1", kind = ExportKind.CBZ,
            requestedName = "ep1.cbz", actualName = "ep1.cbz", bytes = 1, sha256 = "0".repeat(64),
            state = JournalState.FAILED, targetUri = null, attempt = 1,
            failure = ExportFailure.STORAGE_LOW,
        )
        val panel = ExportPanelStateUi.fromSummary(
            ExportSummary(0, 1, 0, listOf(failedEntry), interrupted = false, grantLost = false),
            total = 2,
        )
        assertEquals(ExportPhaseUi.FINISHED_WITH_FAILURES, panel.phase)
        assertEquals(1, panel.progress?.failed)
        assertEquals(PublicIssue.STORAGE_LOW, panel.lastIssue)
    }

    @Test
    fun `a fully verified run maps to all verified`() {
        val panel = ExportPanelStateUi.fromSummary(
            ExportSummary(4, 0, 5, emptyList(), interrupted = false, grantLost = false),
            total = 9,
        )
        assertEquals(ExportPhaseUi.ALL_VERIFIED, panel.phase)
        assertNull(panel.lastIssue)
        assertEquals(9, panel.progress!!.verified + panel.progress.alreadyVerified)
    }

    @Test
    fun `fixtures are labelled and built on the frozen DTOs`() {
        assertTrue(UiFixtures.LABEL.contains("FIXTURE"))
        // The fixtures carry the controller's frozen DTOs (the UI types are only what
        // UiMapping produces from them at render time).
        assertTrue(UiFixtures.running.download is DownloadState.Running)
    }
    @Test
    fun `site cards are read whole or left out, and a refusal gives none`() {
        val card = """{"site":"webtoon","name":"Webtoon","title":"Ready to save","state":"Ready","appearance":"neutral",
            "message":"","rows":[{"name":"Status","value":"Ready to save","warning":false},
            {"name":"Speed","value":"Medium (a slower speed may help)","warning":true}],
            "issues":[{"reason":"Download errors","count":"2"}],"types":[{"label":"Free","saved":"1,200","waiting":"—"}]}"""
        val cards = SiteCard.parse(JSONObject("""{"ok":true,"result":{"cards":[$card,
            {"site":"tapas","name":"Tapas","title":"Ready to save","state":"Ready","rows":[]},
            {"site":"tappytoon","name":"Tappytoon","title":3,"state":"Ready","appearance":"neutral","rows":[]},
            {"site":"tapas","name":"Tapas","title":"t","state":"Ready","appearance":"neutral","rows":[{"name":"Status"}]},
            {"site":"tapas","name":"Tapas","title":"t","state":"Ready","appearance":"neutral","rows":[],"issues":[{"reason":"x","count":2}]},
            {"site":"tapas","name":"Tapas","title":"t","state":"Ready","appearance":"neutral","rows":[],"types":[{"label":"Free","saved":"1"}]}]}}"""))
        assertEquals(listOf(SiteCard("webtoon", "Webtoon", "Ready to save", "Ready", "neutral", "", listOf(
            CardRow("Status", "Ready to save", false), CardRow("Speed", "Medium (a slower speed may help)", true)),
            listOf(IssueCount("Download errors", "2")), listOf(TypeCount("Free", "1,200", "—")))), cards)
        // The desktop card's own parts are read with it.
        val whole = SiteCard.parse(JSONObject("""{"ok":true,"result":{"cards":[{"site":"tapas","name":"Tapas","title":"t",
            "state":"Finished","appearance":"complete","rows":[],"pill":"Done","tone":"success","status":"Could not refresh.",
            "status_error":true,"action":"go","action_text":"Start","action_enabled":true,"signout":true,"signout_enabled":true,
            "speed":"slow","speed_enabled":true,"speeds":[{"key":"slow","label":"Slow"}],"selection":"Download: All types"}]}}"""))
        assertEquals(listOf(SiteCard("tapas", "Tapas", "t", "Finished", "complete", "", emptyList(), pill = "Done", tone = "success",
            status = "Could not refresh.", statusError = true, action = "go", actionText = "Start", actionEnabled = true,
            signOut = true, signOutEnabled = true, speed = "slow", speedEnabled = true, speeds = listOf(SpeedOption("slow", "Slow")),
            selection = "Download: All types")), whole)
        assertNull(SiteCard.parse(JSONObject("""{"ok":false,"result":{"cards":[$card]}}""")))
        assertNull(SiteCard.parse(JSONObject("""{"ok":true,"result":{}}""")))
        assertEquals(emptyList<SiteCard>(), SiteCard.parse(JSONObject("""{"ok":true,"result":{"cards":[]}}""")))
    }


    @Test fun anEpisodeListPageIsAddedToWhatWasReadAndAnUnreadableOneIsRefused() {
        fun answer(items: String, next: String = "null", empty: String = "") = org.json.JSONObject(
            """{"ok":true,"result":{"items":$items,"next_offset":$next,"empty":"$empty"}}""")
        val row = """{"id":"42","title":"Alpha","status":"1 / 3 saved","format":"CBZ","access":"Free"}"""
        val first = EpisodeList.parse(answer("[$row]", "200"), EpisodeList(loading = true))!!
        assertEquals(EpisodeList(listOf(EpisodeRow("42", "Alpha", "1 / 3 saved", "CBZ", "Free")), 200, loaded = true), first)
        // The next page follows the first, and a row already listed is not listed twice.
        val second = EpisodeList.parse(answer(
            """[$row,{"id":"43","title":"Beta","status":"0 / 0 saved","format":"Unknown","access":"Unknown"}]"""), first)!!
        assertEquals(listOf("42", "43"), second.rows.map { it.id })
        assertNull(second.nextOffset)
        assertEquals("Not indexed yet", EpisodeList.parse(answer("[]", empty = "Not indexed yet"), EpisodeList())!!.empty)
        assertEquals("tapas", EpisodeList.key("tapas")); assertEquals("tapas/42", EpisodeList.key("tapas", "42"))
        for (bad in listOf(org.json.JSONObject("""{"ok":false,"code":"refused"}"""),
            org.json.JSONObject("""{"ok":true,"result":{"next_offset":null}}"""),
            answer("[$row]", "\"200\""), answer("[$row]", "0"), answer("[7]"),
            answer("""[{"id":"","title":"A","status":"S","format":"F","access":"A"}]"""),
            answer("""[{"id":"42","title":"A","status":"S","format":"F"}]""")))
            assertNull(EpisodeList.parse(bad, EpisodeList()))
    }
}
