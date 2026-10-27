package io.github.bcrute.comicarchiver.ui

import android.content.Context
import android.os.Build
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.unit.dp
import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

/**
 * The beta's truthful limitations, in plain words: what this build does not do, stated
 * where the person can see it before relying on any of it. Update together with the
 * public roadmap; never soften a limitation to make a date.
 */
@Composable
fun LimitationsScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current.applicationContext
    val identity by produceState<InstalledIdentity?>(null, context) {
        value = withContext(Dispatchers.IO) { installedIdentity(context) }
    }
    val browser = LocalUriHandler.current
    var helpUnavailable by remember { mutableStateOf(false) }
    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Comic Archiver", style = MaterialTheme.typography.headlineSmall)
        Text(identity?.version ?: "Reading installed version…")
        Text(identity?.fingerprint ?: "Reading APK fingerprint…")
        Text("Include this version and fingerprint when reporting a problem.",
            style = MaterialTheme.typography.bodySmall)
        Text("What this beta does not do", style = MaterialTheme.typography.headlineSmall)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Limitation("Choose a source site above. Each site keeps its sign-in and library separate.")
            Limitation("On devices without isolated browser profiles, saved libraries remain available; sign-in needs an updated Android System WebView.")
            Limitation("Tapas Recent and Free Episodes discovery and Fast mode need an updated Android System WebView. Settings shows whether Fast can run on this device.")
            Limitation("Saving uses unmetered connections by default. Settings lets you allow mobile data.")
            Limitation("Comic Archiver saves CBZ and EPUB files. Use Open to read a saved file in another app.")
            Limitation("Reading positions and bookmarks stay with the working library. Export copies finished archives; it does not copy reading progress.")
            Limitation(
                "After an app update or process restart, saving waits until you choose Start saving. " +
                    "Returning to an active save keeps its progress visible.",
            )
            Limitation(
                "Export copies finished archives into a folder you choose and verifies each copy. " +
                    "Export keeps your originals in the app.",
            )
            Limitation(
                "Opening a working library makes a separate copy in the app. The selected source " +
                    "is unchanged; refreshing it makes another working copy.",
            )
            Limitation(
                "Uninstalling removes app-private libraries and sign-ins. Files already copied " +
                    "to your chosen export folder or verified in your B2 bucket stay there.",
            )
            Limitation(
                "Long saves can be stopped by the system after a while; choosing Start saving " +
                    "continues them.",
            )
            Limitation("B2 storage uses your own private bucket. Connect and pass the upload/readback test before choosing it as a destination.")
            Limitation("Other cloud providers, image-quality options and listing services are not supported yet.")
            Limitation("Tapas closes at the end of March 2027; save what you bought before then.")
        }
        TextButton(onClick = {
            helpUnavailable = try {
                browser.openUri("https://tapas.io/newsfeed/271")
                false
            } catch (_: Exception) {
                true
            }
        }) { Text("Read the official Tapas closure FAQ") }
        if (helpUnavailable) Text("Could not open the FAQ. Check that a browser is available and try again.")
    }
}

private data class InstalledIdentity(val version: String, val fingerprint: String)

/** Read the installed Android package, not the shared Python core's version.
 * APK IO runs off the UI thread, stops on leaving About, and never exposes paths. */
@Suppress("DEPRECATION")
private suspend fun installedIdentity(context: Context): InstalledIdentity {
    val info = try {
        context.packageManager.getPackageInfo(context.packageName, 0)
    } catch (_: Exception) { null }
    val version = info?.let {
        val code = if (Build.VERSION.SDK_INT >= 28) it.longVersionCode else it.versionCode.toLong()
        val name = it.versionName?.takeIf(String::isNotBlank)?.let { name -> "Version $name" }
            ?: "Version unavailable"
        "$name (build $code)"
    } ?: "Installed version unavailable"
    val fingerprint = try {
        val digest = MessageDigest.getInstance("SHA-256")
        val apk = info?.applicationInfo?.sourceDir ?: throw IllegalStateException()
        File(apk).inputStream().use { stream ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                currentCoroutineContext().ensureActive()
                val count = stream.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        "APK fingerprint: " + digest.digest().take(8).joinToString("") { "%02x".format(it) }
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (_: Exception) { "APK fingerprint unavailable" }
    return InstalledIdentity(version, fingerprint)
}

@Composable
private fun Limitation(text: String) {
    Text("· $text", style = MaterialTheme.typography.bodyMedium)
}
