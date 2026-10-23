package io.github.bcrute.comicarchiver.browser

import org.junit.Assert.*
import org.junit.Test

class BrowserAgentTest {
    @Test fun requestingDesktopLayoutKeepsTheActualBrowserVersionWithoutTheMobileTokens() {
        val actual = "Mozilla/5.0 (Linux; Android 14; Test Build/ABC; wv) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Version/4.0 Chrome/140.0.7339.1 Mobile Safari/537.36"
        assertEquals("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/140.0.7339.1 Safari/537.36", NativeBrowserFactory.desktopAgent(actual))
        val desktop = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.7339.1 Safari/537.36"
        assertEquals(desktop, NativeBrowserFactory.desktopAgent(desktop))
    }

    @Test fun missingBrowserIdentityAndHeaderControlCharactersDoNotProduceAnInventedAgent() {
        listOf("", "Unknown browser", "Chrome/140.0\r\nPRIVATE", "Chrome/140.0\u0000").forEach { value ->
            try { NativeBrowserFactory.desktopAgent(value); fail("Unsupported identity was admitted") }
            catch (error: ScriptRequestException) {
                assertEquals(ScriptFailure.NOT_READY, error.failure)
                assertFalse(error.message.orEmpty().contains("PRIVATE"))
            }
        }
    }
}
