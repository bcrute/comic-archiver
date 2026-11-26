package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.SiteCard
import org.junit.Assert.*
import org.junit.Test

class SiteCardTest {
    private fun card(site: String = "tapas", download: String = "stopped", listOnly: Boolean = false,
                     hasEpisodes: Boolean = false, hasList: Boolean = false) =
        SiteCard(site, "Site", "", "", "", "", emptyList(), download = download, listOnly = listOnly,
            hasEpisodes = hasEpisodes, hasList = hasList)

    @Test fun a_list_that_appears_without_episodes_makes_the_library_read_its_lists_again() {
        // A cleared list shown again on a hold, or with series listed and no episode recorded.
        assertTrue(card(download = "held", hasList = true).outdatesLists(card(download = "running")))
        assertTrue(card(hasList = true).outdatesLists(card()))
    }

    @Test fun episodes_appearing_or_a_list_only_fetch_stopping_make_the_library_read_its_lists_again() {
        assertTrue(card(hasEpisodes = true, hasList = true).outdatesLists(card(hasList = true)))
        assertTrue(card(listOnly = true, hasList = true).outdatesLists(card(download = "running", listOnly = true, hasList = true)))
    }

    @Test fun the_lists_are_kept_while_nothing_new_is_listed_and_for_another_sites_card() {
        assertFalse(card(hasList = true).outdatesLists(card(hasList = true)))
        assertFalse(card().outdatesLists(card(hasEpisodes = true, hasList = true)))
        assertFalse(card(hasEpisodes = true, hasList = true).outdatesLists(card(site = "webtoon")))
    }
}
