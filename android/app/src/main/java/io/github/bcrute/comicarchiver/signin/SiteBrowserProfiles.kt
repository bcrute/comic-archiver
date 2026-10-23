package io.github.bcrute.comicarchiver.signin

import android.os.Looper
import android.webkit.CookieManager
import android.webkit.WebStorage
import android.webkit.WebView
import androidx.webkit.Profile
import androidx.webkit.ProfileStore
import androidx.webkit.WebStorageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature

/** A provider's browser authority; obtain/bind only on the main thread.
 * Tapas retains the existing default profile and login. Other providers never
 * fall back to it. A switch needs a fresh WebView bound before any other use. */
class SiteBrowserProfiles(private val sites: Map<String, SiteAddress>) {
    private val opened = mutableMapOf<String, SiteBrowserProfile>()

    fun supportsIsolatedSites(): Boolean = try {
        WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE) &&
            WebViewFeature.isFeatureSupported(WebViewFeature.DELETE_BROWSING_DATA)
    } catch (_: Exception) { false }

    fun open(key: String): SiteBrowserProfile {
        check(Looper.myLooper() == Looper.getMainLooper())
        val address = sites[key] ?: throw IllegalArgumentException("Choose a supported site.")
        if (key != "tapas" && !supportsIsolatedSites())
            throw IllegalStateException("Update Android System WebView to use isolated site sign-ins.")
        return opened.getOrPut(key) { SiteBrowserProfile(key, address) }
    }
}

class SiteBrowserProfile internal constructor(val key: String, address: SiteAddress) {
    private val profileName = if (key == "tapas") null else "comic-archiver-$key-v1"
    private val named: Profile by lazy {
        ProfileStore.getInstance().getOrCreateProfile(checkNotNull(profileName))
    }
    val cookies = WebViewCookieStore(address.cookieHosts,
        managerProvider = { if (profileName == null) CookieManager.getInstance() else named.cookieManager },
        clearProfile = ::clear)

    /** This must be the first operation on a newly constructed WebView. */
    fun bind(webView: WebView) {
        check(Looper.myLooper() == Looper.getMainLooper())
        profileName?.let { WebViewCompat.setProfile(webView, it) }
    }

    private fun clear(done: (Boolean) -> Unit) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DELETE_BROWSING_DATA)) {
            done(false) // No global-cookie fallback that could clear another provider.
            return
        }
        val storage = if (profileName == null) WebStorage.getInstance() else named.webStorage
        WebStorageCompat.deleteBrowsingData(storage) { done(true) }
    }
}
