package io.github.bcrute.comicarchiver.ui

import io.github.bcrute.comicarchiver.controller.AppState
import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.controller.DownloadState
import io.github.bcrute.comicarchiver.controller.LibraryEntry
import io.github.bcrute.comicarchiver.controller.LibraryKind
import io.github.bcrute.comicarchiver.controller.Notice
import io.github.bcrute.comicarchiver.controller.SignInState
import io.github.bcrute.comicarchiver.controller.SiteChoice
import io.github.bcrute.comicarchiver.words.say

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
    say("signin.account_mismatch", "site" to site)

/**
 * What a sign-in that ended says, in the desktop's words. Going back says nothing, as on the desktop.
 */
internal fun signInNotice(code: String, site: String): NoticeUi? = when (code) {
    "signin_cancelled" -> null
    "account_mismatch" -> NoticeUi(PublicIssue.LOGIN_REQUIRED, accountMismatchText(site))
    // The site ended the sign-in, or the saved one no longer works: the desktop's card says the same.
    "login_required" -> NoticeUi(PublicIssue.LOGIN_REQUIRED, say("dashboard.signed_you_out", "site" to site))
    "signin_unreachable" -> NoticeUi(PublicIssue.JOB_FAILED, say("desktop.the_sign_in_page_could_not", "site" to site))
    "signin_incomplete" -> NoticeUi(PublicIssue.JOB_FAILED, say("signin.incomplete", "site" to site))
    else -> PublicIssue.fromWireName(code)?.let { NoticeUi(it) }
        ?: NoticeUi(PublicIssue.JOB_FAILED, say("signin.failed", "site" to site))
}

fun AppState.toSignInScreenUi(): SignInScreenUi = SignInScreenUi(
    busy = signIn is SignInState.Page || signIn is SignInState.Settling,
    restoring = signIn is SignInState.Restoring,
    siteName = sites.firstOrNull { it.key == site }?.name ?: if (site == "tapas") "Tapas" else site,
    origin = signInOrigin,
    controlsEnabled = workspaceReady && !libraryBusy && !siteBusy && !signOutPending && sites.firstOrNull { it.key == site }?.signInAvailable != false,
    retry = (signIn as? SignInState.Failed)?.code == "signin_unreachable",
    notice = (signIn as? SignInState.Failed)?.let {
        signInNotice(it.code, sites.firstOrNull { choice -> choice.key == site }?.name ?: if (site == "tapas") "Tapas" else site)
    } ?: notice?.toNoticeUi(),
)

/** The desktop's dashboard: the saved library over every site, then a card for each site the app offers. */
fun AppState.toDownloadsScreenUi(): DownloadsScreenUi {
    val controls = workspaceReady && !libraryBusy && !siteBusy && !signOutPending && !startingAll
    val offered = sites.filter { it.enabled }.ifEmpty { listOf(SiteChoice(site, if (site == "tapas") "Tapas" else site, true)) }
    val cards = offered.map { siteCard(it, controls) }
    return DownloadsScreenUi(
        savedEpisodes = allSites?.let { String.format(java.util.Locale.US, "%,d", it.episodes) },
        cards = cards,
        startAllEnabled = controls && !settingsBusy,
        pauseAllVisible = startingAll || cards.any { it.action == CardAction.PAUSE },
        // A download the site signed out of says so on its card, not here as well.
        notice = notice?.toNoticeUi()
            ?: (download as? DownloadState.Failed)?.takeIf { it.code != BetaCodes.LOGIN_REQUIRED }?.let {
                NoticeUi(PublicIssue.fromWireName(it.code) ?: PublicIssue.JOB_FAILED)
            },
    )
}

/**
 * One site's card. Its words are the card the desktop's own model wrote for the site; a site not
 * read yet has the desktop's empty card. What its button does is the controller's newest word for
 * the site on screen, so a press of Start or Pause is never contradicted by a card read just before it.
 */
private fun AppState.siteCard(choice: SiteChoice, controls: Boolean): SiteCardUi {
    val card = siteCards.firstOrNull { it.site == choice.key }
    val shown = choice.key == site
    fun row(name: String) = card?.rows?.firstOrNull { it.name == name }
    val restoring = shown && signIn is SignInState.Restoring
    val checking = restoring || shown && (signIn is SignInState.Page || signIn is SignInState.Settling)
    val signedIn = if (shown) signIn is SignInState.SignedIn || signIn is SignInState.Restoring && card?.signOut == true
        else card?.signOut == true && card.action != "signin"
    val saving = if (shown) download is DownloadState.Planning || download is DownloadState.Running
        else choice.saving || card?.action == "pause"
    val action = if (saving) CardAction.PAUSE else if (!signedIn) CardAction.SIGN_IN else CardAction.GO
    val failed = (signIn as? SignInState.Failed)?.takeIf { shown }?.let { signInNotice(it.code, choice.name)?.detail }
    val status = row(say("dashboard.status"))
    return SiteCardUi(
        site = choice.key,
        name = choice.name,
        appearance = card?.appearance ?: "neutral",
        pill = card?.pill ?: say("dashboard.not_signed_in"),
        saved = row(say("settings.saved"))?.value?.removeSuffix(" episodes")?.removeSuffix(" episode") ?: NO_VALUE,
        timeLeft = row(say("dashboard.time_left"))?.value ?: NO_VALUE,
        space = row(say("dashboard.space"))?.value ?: NO_VALUE,
        rate = row(say("dashboard.download_speed"))?.value ?: NO_VALUE,
        status = status?.value ?: NO_VALUE,
        statusWarning = status?.warning == true,
        message = failed ?: card?.status?.takeIf { it.isNotBlank() }
            ?: if (restoring) say("signin.checking_saved_sign_in") else "",
        messageError = failed != null || card?.statusError == true,
        selection = card?.selection?.takeIf { it.isNotBlank() } ?: say("window.download_all_types"),
        selectionEnabled = controls && !settingsBusy,
        listOnly = card?.listOnly == true, hasEpisodes = card?.hasEpisodes == true, hasList = card?.hasList == true,
        action = action,
        actionText = when (action) {
            CardAction.SIGN_IN -> if (checking && !restoring) say("dashboard.signing_in_2") else say("dashboard.sign_in")
            CardAction.PAUSE -> say("downloads.pause")
            CardAction.GO -> card?.actionText?.takeIf { card.action == "go" && it.isNotBlank() }
                ?: if (shown && download is DownloadState.Paused) say("dashboard.continue") else say("dashboard.start")
        },
        actionEnabled = controls && when (action) {
            CardAction.SIGN_IN -> !checking && choice.signInAvailable
            CardAction.PAUSE -> true
            CardAction.GO -> !checking && !settingsBusy && (card == null || card.action != "go" || card.actionEnabled)
        },
        signOut = if (shown) signIn is SignInState.SignedIn else card?.signOut == true,
        signOutEnabled = controls && !saving,
        speed = card?.speed.orEmpty(),
        speedEnabled = controls && signedIn && card?.speedEnabled == true,
        speeds = card?.speeds.orEmpty(),
    )
}

/** A card's field with nothing to show yet, as the desktop marks it. */
private const val NO_VALUE = "—"

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
