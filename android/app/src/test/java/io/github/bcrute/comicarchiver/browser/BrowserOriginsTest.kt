package io.github.bcrute.comicarchiver.browser

import org.junit.Assert.*
import org.junit.Test

class BrowserOriginsTest {
    @Test fun scriptAuthorityRequiresAnExactHttpsOriginWithoutCredentialsOrWildcards() {
        assertTrue(IsolatedScriptChannel.exactHttpsOrigin("https://tapas.io"))
        assertTrue(IsolatedScriptChannel.exactHttpsOrigin("https://reader.example"))
        listOf("http://tapas.io", "https://*.tapas.io", "*", "null", "about:blank",
            "file:///reader", "content://reader", "https://person:secret@tapas.io",
            "https://tapas.io/", "https://tapas.io/path", "https://tapas.io?query",
            "https://tapas.io#fragment", "https://tapas.io:443", "https://tapas.io:8443",
            "https://tapas.io\\@other.example", "https://tapas.io\n").forEach { origin ->
            assertFalse("Unsupported authority shape was admitted", IsolatedScriptChannel.exactHttpsOrigin(origin))
        }
    }
}
