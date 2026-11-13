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
import androidx.compose.material3.OutlinedButton
import androidx.compose.ui.platform.testTag
import io.github.bcrute.comicarchiver.controller.SupportNote
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
import io.github.bcrute.comicarchiver.words.say

/**
 * The beta's truthful limitations, in plain words: what this build does not do, stated
 * where the person can see it before relying on any of it. Update together with the
 * public roadmap; never soften a limitation to make a date.
 */
@Composable
fun LimitationsScreen(modifier: Modifier = Modifier, onLicenses: () -> Unit = {},
                      supportNote: SupportNote? = null, supportBusy: Boolean = false,
                      onCopyDetails: () -> Unit = {}, onSaveLog: () -> Unit = {}) {
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
        Text(identity?.version ?: say("about.reading_installed_version"))
        Text(identity?.fingerprint ?: say("about.reading_apk_fingerprint"))
        Text(say("about.include_this_version_and_fingerprint_when"),
            style = MaterialTheme.typography.bodySmall)
        Text(LICENSE_SUMMARY, style = MaterialTheme.typography.bodyMedium)
        TextButton(onClick = onLicenses) { Text(say("app.licenses")) }
        Text(SUPPORT_DETAILS, style = MaterialTheme.typography.bodySmall)
        Text(SUPPORT_LOG, style = MaterialTheme.typography.bodySmall)
        OutlinedButton(onClick = onCopyDetails, enabled = !supportBusy,
            modifier = Modifier.testTag("support-copy")) { Text(say("about.copy_details_for_support")) }
        OutlinedButton(onClick = onSaveLog, enabled = !supportBusy,
            modifier = Modifier.testTag("support-save-log")) { Text(say("about.save_log_for_support")) }
        supportNote?.let {
            Text(it.text, modifier = Modifier.testTag("support-note"),
                color = if (it.failed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
        }
        Text(say("about.what_this_beta_does_not_do"), style = MaterialTheme.typography.headlineSmall)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Limitation(say("about.choose_a_source_site_above_each"))
            Limitation(say("about.on_devices_without_isolated_browser_profiles"))
            Limitation(say("about.tapas_recent_and_free_episodes_discovery"))
            Limitation(say("about.saving_uses_unmetered_connections_by_default"))
            Limitation(say("about.comic_archiver_saves_cbz_and_epub"))
            Limitation(say("about.reading_positions_and_bookmarks_stay_with"))
            Limitation(
                say("about.after_an_app_update_or_process"),
            )
            Limitation(
                say("about.export_copies_finished_archives_into_a"),
            )
            Limitation(
                say("about.opening_a_working_library_makes_a"),
            )
            Limitation(
                say("about.uninstalling_removes_app_private_libraries_and"),
            )
            Limitation(
                say("about.long_saves_can_be_stopped_by"),
            )
            Limitation(say("about.b2_storage_uses_your_own_private"))
            Limitation(say("about.saving_from_tapas_needs_an_up"))
            Limitation(say("about.other_cloud_providers_image_quality_options"))
            Limitation(say("about.tapas_closes_at_the_end_of"))
        }
        TextButton(onClick = {
            helpUnavailable = try {
                browser.openUri("https://tapas.io/newsfeed/271")
                false
            } catch (_: Exception) {
                true
            }
        }) { Text(say("about.read_the_official_tapas_closure_faq")) }
        if (helpUnavailable) Text(say("about.could_not_open_the_faq_check"))
    }
}

/** The desktop's own words above its two support buttons. */
internal val SUPPORT_DETAILS get() = say("about.asking_for_help_this_copies_a")
internal val SUPPORT_LOG get() = say("about.asked_for_the_apps_log_save")

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
        val name = it.versionName?.takeIf(String::isNotBlank)?.let { name -> say("about.version", "name" to name) }
            ?: say("about.version_unavailable")
        say("about.build", "name" to name, "code" to code)
    } ?: say("about.installed_version_unavailable")
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
        say("about.apk_fingerprint") + digest.digest().take(8).joinToString("") { "%02x".format(it) }
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (_: Exception) { say("about.apk_fingerprint_unavailable") }
    return InstalledIdentity(version, fingerprint)
}

@Composable
private fun Limitation(text: String) {
    Text("· $text", style = MaterialTheme.typography.bodyMedium)
}
