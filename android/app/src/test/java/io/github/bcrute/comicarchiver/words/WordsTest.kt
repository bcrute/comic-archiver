package io.github.bcrute.comicarchiver.words

import org.junit.Assert.assertEquals
import org.junit.Test

class WordsTest {
    @Test
    fun aPhraseIsTheSharedListsOwnWithItsNamedValuesFilledIn() {
        assertEquals("Stop", say("notification.stop"))
        assertEquals("Sign-in did not finish. Try Sign in to Webtoon again.", say("signin.failed", "site" to "Webtoon"))
        assertEquals("Sign in to the site again.", say("notice.login_required.action"))
    }

    @Test
    fun aCodeWithoutWordsShowsItselfRatherThanNothing() {
        assertEquals("no.such.code", say("no.such.code"))
    }
}
