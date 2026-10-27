package io.github.bcrute.comicarchiver.integration

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chaquo.python.Python
import io.github.bcrute.comicarchiver.signin.WebViewCookieStore
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Exercise the actual Python sequence → native overload → CookieManager path.
 * A Python stand-in for the store cannot detect Java collection conversion. */
@RunWith(AndroidJUnit4::class)
class CookieBridgeTest {
    @Test
    fun pythonResponseCookiesReachTheChapterAssetHost() {
        val api = "https://api-global.tappytoon.com/"
        val asset = "https://content-repository-cdn.tappytoon.com/fixture.jpg"
        val name = "comic_archiver_cookie_bridge_fixture"
        val store = WebViewCookieStore(setOf("api-global.tappytoon.com", "content-repository-cdn.tappytoon.com"))
        val python = Python.getInstance()
        val site = python.getModule("comic_archive.sites").callAttr("named", "tappytoon")
        val source = python.getModule("comic_archive.android_transport")
            .get("WebViewCookieSource")!!.call(store, site)
        try {
            source.callAttr("apply", api, arrayOf("$name=READY; Domain=tappytoon.com; Path=/; Secure; HttpOnly"))
            assertTrue(source.callAttr("header_for", asset).toString().contains("$name=READY"))
            assertFalse(source.callAttr("header_for", "https://www.webtoons.com/").toString().contains(name))
        } finally {
            source.callAttr("apply", api, arrayOf("$name=; Domain=tappytoon.com; Path=/; Secure; Max-Age=0"))
        }
    }
}
