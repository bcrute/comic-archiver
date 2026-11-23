package io.github.bcrute.comicarchiver.signin

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The same names and addresses the desktop sign-in window's rule is tested with. */
class PrivateDestinationTest {
    @Test
    fun browserResourcesRequirePublicHttpsOnTheDefaultPortOrAnInlineDocument() {
        for (url in listOf("https://tapas.io/", "https://accounts.google.com:443/signin",
            "https://[2606:4700:4700::1111]/")) {
            assertTrue(url, PrivateDestination.allowsHttps(url))
            assertTrue(url, PrivateDestination.allowsResource(url))
        }
        for (url in listOf("https://tapas.io:8443/", "https://tapas.io:/", "https://name@tapas.io/",
            "http://tapas.io/", "https://device.local/", "https:///missing", "blob")) {
            assertFalse(url, PrivateDestination.allowsHttps(url))
            assertFalse(url, PrivateDestination.allowsResource(url))
        }
        for (url in listOf("data:text/plain,inline", "blob:https://tapas.io/page", "about:srcdoc")) {
            assertFalse(url, PrivateDestination.allowsHttps(url))
            assertTrue(url, PrivateDestination.allowsResource(url))
        }
    }

    @Test
    fun localNamesAndEveryNonPublicAddressArePrivateAsOnTheDesktop() {
        for (host in listOf("localhost", "app.localhost.", "local", "printer.local", "home.arpa",
            "router.home.arpa.", "printer", "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.169.254",
            "172.16.0.1", "192.168.1.10", "224.0.0.1", "255.255.255.255", "::", "[::1]", "fe80::1",
            "fd00::1", "2001:db8::1", "::ffff:192.168.1.10", "LOCALHOST", "192.0.2.7", "198.18.0.1",
            "198.51.100.4", "203.0.113.9", "240.0.0.1", "ff02::1", "2002:c0a8:1::1", "64:ff9b::7f00:1"))
            assertTrue(host, PrivateDestination.matches(host))
    }

    @Test
    fun publicNamesAndAddressesAreNot() {
        for (host in listOf("tapas.io", "accounts.google.com", "localhost.example.com",
            "printer.local.example.com", "8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "", "100.63.0.1", "::ffff:8.8.8.8",
            "2606:4700:4700:0:0:0:0:1111",
            "100.128.0.1", "172.32.0.1", "192.167.1.1", "198.20.0.1", "m.tapas.io."))
            assertFalse(host, PrivateDestination.matches(host))
        assertFalse(PrivateDestination.matches(null))
    }

    @Test
    fun anAddressWrittenAnotherWayIsRefusedWithoutBeingLookedUp() {
        for (host in listOf("2130706433", "127.1", "0x7f.0.0.1", "0x7f000001", "999.1.1.1", "1.2.3.4.5",
            "fe80::1%wlan0", "1::2::3", "12345::1", "::ffff:999.1.1.1", "1:2:3:4:5:6:7:8:9", "g::1"))
            assertTrue(host, PrivateDestination.matches(host))
    }
}
