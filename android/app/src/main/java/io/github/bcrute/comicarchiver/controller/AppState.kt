package io.github.bcrute.comicarchiver.controller

import io.github.bcrute.comicarchiver.words.say

/** Frozen beta DTOs. The UI renders these; it never owns policy. */

data class AppState(
    val site: String,
    val signIn: SignInState,
    val download: DownloadState,
    val library: List<LibraryEntry>,
    val notice: Notice?,
    val settings: DownloadSettings? = null,
    val settingsBusy: Boolean = false,
    val settingsNotice: Notice? = null,
    val catalog: CatalogState = CatalogState(),
    val theme: AppTheme = AppTheme.SYSTEM,
    val cloud: CloudStatus? = null,
    val cloudBusy: Boolean = false,
    val cloudNotice: Notice? = null,
    val sites: List<SiteChoice> = emptyList(),
    /** The Library's recorded episodes, by [EpisodeList.key]; read only as each site or series is opened. */
    val episodes: Map<String, EpisodeList> = emptyMap(),
    /** What Copy details or Save log for support last answered, and whether one is being made. */
    val supportNote: SupportNote? = null,
    val supportBusy: Boolean = false,
    val siteBusy: Boolean = false,
    val signInOrigin: String? = null,
    val signOutPending: Boolean = false,
    val libraryId: String = "default",
    val libraryBusy: Boolean = false,
    val workspaceReady: Boolean = true,
    val observations: DownloadObservations? = null,
    val statusReadFailed: Boolean = false,
    val admittedAtMillis: Long? = null,
    val cloudCheckedAtMillis: Long? = null,
    val cloudReadFailed: Boolean = false,
    /** Start all is going from site to site; the screens wait for it. */
    val startingAll: Boolean = false,
    val allSites: AllSites? = null,
    /** Each opened site's card, worded by the same model as the desktop's. */
    val siteCards: List<SiteCard> = emptyList(),
)

/** The line under the support buttons: the desktop's own words, and whether it reports a failure. */
data class SupportNote(val text: String, val failed: Boolean = false)

/** The desktop's words for its two support buttons, and the phone's for a copy that failed. */
object SupportWords {
    val COPIED get() = say("app.copied_paste_it_into_your_message")
    val NOT_COPIED get() = say("app.the_details_could_not_be_copied")
    val LOG_FAILED get() = say("app.the_log_could_not_be_saved")
    fun logSaved(name: String) = say("app.saved_in_your_downloads_folder_as", "name" to name)
}

/** Saved-file counts over every site of the working library, and the sites they cover. */
data class AllSites(val sites: List<String>, val episodes: Long, val files: Long)

/** One site's card as the desktop would show it: its state in a word, its title and its rows. */
data class SiteCard(val site: String, val name: String, val title: String, val state: String,
                    val appearance: String, val message: String, val rows: List<CardRow>,
                    /** What Settings lists for the site: its download issues, and saved and pending by episode type. */
                    val issues: List<IssueCount> = emptyList(), val types: List<TypeCount> = emptyList(),
                    /** The download's own word for where it was when the card was read: running, stopped, finished. */
                    val download: String = "") {
    companion object {
        /** The cards of a site_cards answer, or null when it is not one. A card missing a part is left out. */
        fun parse(envelope: org.json.JSONObject): List<SiteCard>? {
            if (envelope.opt("ok") != true) return null
            val cards = envelope.optJSONObject("result")?.optJSONArray("cards") ?: return null
            return (0 until cards.length()).mapNotNull { index ->
                val card = cards.optJSONObject(index) ?: return@mapNotNull null
                val rows = card.optJSONArray("rows") ?: return@mapNotNull null
                val words = listOf("site", "name", "title", "state", "appearance").map { card.opt(it) as? String ?: return@mapNotNull null }
                SiteCard(words[0], words[1], words[2], words[3], words[4], card.opt("message") as? String ?: "",
                    (0 until rows.length()).map { at ->
                        val row = rows.optJSONObject(at) ?: return@mapNotNull null
                        CardRow(row.opt("name") as? String ?: return@mapNotNull null,
                            row.opt("value") as? String ?: return@mapNotNull null, row.opt("warning") == true)
                    },
                    (card.optJSONArray("issues") ?: org.json.JSONArray()).let { issues ->
                        (0 until issues.length()).map { at ->
                            val issue = issues.optJSONObject(at) ?: return@mapNotNull null
                            IssueCount(issue.opt("reason") as? String ?: return@mapNotNull null,
                                issue.opt("count") as? String ?: return@mapNotNull null)
                        }
                    },
                    (card.optJSONArray("types") ?: org.json.JSONArray()).let { types ->
                        (0 until types.length()).map { at ->
                            val type = types.optJSONObject(at) ?: return@mapNotNull null
                            TypeCount(type.opt("label") as? String ?: return@mapNotNull null,
                                type.opt("saved") as? String ?: return@mapNotNull null,
                                type.opt("waiting") as? String ?: return@mapNotNull null)
                        }
                    },
                    card.opt("download") as? String ?: "")
            }
        }
    }
}

data class CardRow(val name: String, val value: String, val warning: Boolean)

/** One line of the Library's recorded episodes, in the desktop's words: a series under its site, or an episode under its series. */
data class EpisodeRow(val id: String, val title: String, val status: String, val format: String, val access: String)

/** What has been read under one site, or under one of its series. */
data class EpisodeList(
    val rows: List<EpisodeRow> = emptyList(),
    /** Where the next page starts; null when everything is listed. */
    val nextOffset: Int? = null,
    val loading: Boolean = false,
    val failed: Boolean = false,
    val loaded: Boolean = false,
    /** What to say when nothing is listed. */
    val empty: String = "",
) {
    companion object {
        /** The place of a site's series list, or of one series' episode list. */
        fun key(site: String, seriesId: String? = null): String = if (seriesId == null) site else "$site/$seriesId"

        /** One page of an episode_inventory answer added to what was read before, or null when it is not one. */
        fun parse(envelope: org.json.JSONObject, before: EpisodeList): EpisodeList? {
            if (envelope.opt("ok") != true) return null
            val result = envelope.optJSONObject("result") ?: return null
            val items = result.optJSONArray("items") ?: return null
            val next = when (val value = result.opt("next_offset")) {
                null, org.json.JSONObject.NULL -> null
                is Int -> value.takeIf { it > 0 } ?: return null
                else -> return null
            }
            val rows = (0 until items.length()).map { index ->
                val item = items.optJSONObject(index) ?: return null
                val words = listOf("id", "title", "status", "format", "access").map { item.opt(it) as? String ?: return null }
                if (words[0].isBlank()) return null
                EpisodeRow(words[0], words[1], words[2], words[3], words[4])
            }
            val known = before.rows.map { it.id }.toSet()
            return EpisodeList(before.rows + rows.filter { it.id !in known }, next, loaded = true,
                empty = result.opt("empty") as? String ?: "")
        }
    }
}

/** A download issue and how many episodes have it, as the desktop's Settings lists them. */
data class IssueCount(val reason: String, val count: String)

/** An episode type with its saved and pending counts, already worded. */
data class TypeCount(val label: String, val saved: String, val waiting: String)

data class SiteChoice(val key: String, val name: String, val enabled: Boolean, val reason: String? = null,
                      val signInAvailable: Boolean = enabled,
                      /** This site is saving now, whichever site is being shown. */
                      val saving: Boolean = false)

enum class AppTheme(val label: String) { SYSTEM(say("app.system")), LIGHT(say("app.light")), DARK(say("app.dark")) }

data class SpeedChoice(val key: String, val label: String, val available: Boolean)

/** One of the site's episode types, in the desktop's words, and whether it is downloaded. */
data class DownloadTypeChoice(val key: String, val label: String, val help: String, val chosen: Boolean)

data class DownloadSettings(
    val wifiOnly: Boolean,
    val speed: String,
    val speeds: List<SpeedChoice>,
    val connection: String,
    val metered: Boolean?,
    val allowed: Boolean,
    val downloadTypes: List<DownloadTypeChoice> = emptyList(),
)

sealed interface SignInState {
    data object SignedOut : SignInState

    /** Checking the saved WebView session without opening or navigating a sign-in page. */
    data object Restoring : SignInState

    /** The sign-in page currently shown; the URL is the site's own page. */
    data class Page(val url: String) : SignInState

    /** Waiting for the platform cookie store to settle after a step. */
    data object Settling : SignInState

    data class SignedIn(val site: String) : SignInState
    data class Failed(val code: String) : SignInState
}

sealed interface DownloadState {
    /**
     * The only state at cold start, after process death and after update; a plan the user
     * paused then reads as Paused. Every transition into Running requires the go() action;
     * nothing resumes on its own.
     */
    data object Held : DownloadState
    data object Planning : DownloadState
    data class Running(val done: Int, val total: Int, val description: String? = null,
                       val detail: String? = null) : DownloadState
    data class Paused(val reason: String) : DownloadState
    data class Finished(val summary: String) : DownloadState
    data class Failed(val code: String) : DownloadState
}

enum class LibraryKind { COMIC, NOVELS }

data class LibraryEntry(
    val seriesId: String,
    val title: String,
    val kind: LibraryKind,
    val episodeCount: Int,
    val savedCount: Int,
)

/** User-visible words only: no paths, hosts, tokens or stack traces. */
data class Notice(val code: String, val message: String)

/** Public error and hold words (contract §7); each maps to one notice and one recovery action. */
object BetaCodes {
    const val LOGIN_REQUIRED = "login_required"
    const val METERED_HOLD = "metered_hold"
    const val CONNECTION_UNVERIFIED = "connection_unverified"
    const val STORAGE_LOW = "storage_low"
    const val SOURCE_REFUSED = "source_refused"
    const val JOB_FAILED = "job_failed"
    const val EXPORT_INTERRUPTED = "export_interrupted"
    const val WORKER_OFFLINE = "worker_offline"
    const val UNSUPPORTED_SITE = "unsupported_site"
}
