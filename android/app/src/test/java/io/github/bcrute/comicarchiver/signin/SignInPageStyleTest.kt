package io.github.bcrute.comicarchiver.signin

import org.junit.Assert.*
import org.junit.Test

class SignInPageStyleTest {
    private val tapas = WebViewSignInEngine.BETA_SITES.getValue("tapas")

    @Test fun tapasHidesItsCookieNoticeOnItsOwnSignInPagesOnly() {
        assertEquals(listOf("#gdpr-cookie-message"), tapas.signInHidden)
        assertEquals(setOf("https://m.tapas.io", "https://tapas.io"), SignInPageStyle.origins(tapas))
        val script = requireNotNull(SignInPageStyle.script(tapas.signInHidden))
        assertTrue(script.contains("\"#gdpr-cookie-message{display:none!important}\""))
        // A style rule only: nothing is clicked, answered, sent or read back.
        listOf("click", "postMessage", "cookie", "fetch", "localStorage").forEach {
            assertFalse("The hide rule must not use $it", script.contains(it))
        }
    }

    @Test fun aSiteThatNamesNothingGetsNoScript() {
        assertNull(SignInPageStyle.script(emptyList()))
    }

    @Test fun onlyPlainIdsAndClassesAreAccepted() {
        listOf("#a", ".notice", "#gdpr-cookie_message2").forEach { assertNotNull(SignInPageStyle.script(listOf(it))) }
        listOf("", "div", "#", "#1a", "#a{display:block}", "#a,body", "#a body", "#a\"", "#a;", "*").forEach {
            assertThrows(IllegalArgumentException::class.java) { SignInPageStyle.script(listOf(it)) }
        }
    }
}
