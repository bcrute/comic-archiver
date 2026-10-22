package io.github.bcrute.comicarchiver.signin

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The sign-in journey's landing-host boundary, pinned off-device: only the site's own
 * hosts, over HTTPS, by exact match. The mobile mirror may finish sign-in pages; nothing
 * that merely contains or resembles those names may, and landing hosts never widen the
 * cookie seams (those answer for cookieHosts alone, by construction elsewhere).
 */
class SignInLandingTest {

    @Test
    fun android_fallback_uses_the_authenticated_mobile_cookie_origin() {
        val site = WebViewSignInEngine.BETA_SITES.getValue("tapas")
        assertTrue(site.home == "https://m.tapas.io")
        assertTrue(site.cookieHosts == setOf("m.tapas.io"))
        assertTrue(SignInLanding.admits("tapas.io", https = true, site = site))
    }

    private val tapas = SiteAddress(
        "https://tapas.io", "/account/signin",
        setOf("tapas.io"), setOf("m.tapas.io"),
    )

    @Test
    fun the_site_itself_and_its_mobile_mirror_admit_a_finished_page() {
        assertTrue(SignInLanding.admits("tapas.io", https = true, site = tapas))
        assertTrue(SignInLanding.admits("m.tapas.io", https = true, site = tapas))
        assertTrue(SignInLanding.admits("M.TAPAS.IO", https = true, site = tapas))
    }

    @Test
    fun lookalikes_and_other_hosts_never_admit() {
        assertFalse(SignInLanding.admits("evilmtapas.io", https = true, site = tapas))
        assertFalse(SignInLanding.admits("m.tapas.io.evil.com", https = true, site = tapas))
        assertFalse(SignInLanding.admits("nottapas.io", https = true, site = tapas))
        assertFalse(SignInLanding.admits("tapas.io.evil.com", https = true, site = tapas))
        assertFalse(SignInLanding.admits("localhost", https = true, site = tapas))
    }

    @Test
    fun plain_http_and_a_missing_host_never_admit() {
        assertFalse(SignInLanding.admits("tapas.io", https = false, site = tapas))
        assertFalse(SignInLanding.admits("m.tapas.io", https = false, site = tapas))
        assertFalse(SignInLanding.admits(null, https = true, site = tapas))
        assertFalse(SignInLanding.admits(null, https = false, site = tapas))
    }

    @Test
    fun a_site_without_extra_landing_hosts_admits_only_its_cookie_hosts() {
        val noMirror = SiteAddress(
            "https://www.tappytoon.com", "/en/login",
            setOf("www.tappytoon.com", "api-global.tappytoon.com"),
        )
        assertTrue(SignInLanding.admits("www.tappytoon.com", https = true, site = noMirror))
        assertTrue(SignInLanding.admits("api-global.tappytoon.com", https = true, site = noMirror))
        assertFalse(SignInLanding.admits("m.tappytoon.com", https = true, site = noMirror))
    }
}
