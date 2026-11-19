package io.github.bcrute.comicarchiver.ui

import android.content.Context
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.ui.semantics.Role
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python
import java.io.File
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** Device-local opt-in; all filesystem/Python/network work runs off the UI thread. */
class UpdateSettings(private val context: Context, private val scope: CoroutineScope) {
    var automatic by mutableStateOf(false)
        private set
    var busy by mutableStateOf(false)
        private set
    var available by mutableStateOf(false)
        private set
    var message by mutableStateOf("Automatic update checks are off. Check manually whenever you choose.")
        private set
    private val path get() = File(context.filesDir, "updates.json").absolutePath
    private fun module() = Python.getInstance().getModule("comic_archive.updates")

    fun load() = run {
        automatic = withContext(Dispatchers.IO) { module().callAttr("automatic", path).toBoolean() }
        // The shared checker refuses any request unless this opt-in is already stored.
        result(false)
    }

    fun check() = run { result(true) }

    fun chooseAutomatic(enabled: Boolean) = run {
        withContext(Dispatchers.IO) { module().callAttr("set_automatic", path, enabled) }
        automatic = enabled
        result(false)
    }

    private fun run(work: suspend () -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try { work() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { message = "Could not check or save update settings. Try again when connected." }
            finally { busy = false }
        }
    }

    @Suppress("DEPRECATION")
    private suspend fun result(force: Boolean) {
        val result = withContext(Dispatchers.IO) {
            val current = context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "unknown"
            JSONObject(module().callAttr("check_json", current, path, force).toString())
        }
        available = result.optString("status") == "available"
        message = when (result.optString("status")) {
            "available" -> "Comic Archiver ${result.getString("latest")} is available."
            "current" -> "You have the latest version."
            "disabled" -> "Automatic update checks are off. Check manually whenever you choose."
            else -> "Could not check for updates. Try again when connected."
        }
    }
}

@Composable
fun rememberUpdateSettings(ready: Boolean): UpdateSettings {
    val context = LocalContext.current.applicationContext
    val scope = rememberCoroutineScope()
    val settings = remember(context, scope) { UpdateSettings(context, scope) }
    LaunchedEffect(ready) {
        if (ready) while (true) {
            settings.load()
            delay(24 * 60 * 60 * 1000L)
        }
    }
    return settings
}

@Composable
fun UpdateControls(settings: UpdateSettings, manual: Boolean) {
    val browser = LocalUriHandler.current
    var failed by remember { mutableStateOf(false) }
    if (manual || settings.available) Column(Modifier.padding(16.dp)) {
        Text(if (settings.busy) "Checking for updates…" else settings.message)
        if (manual) {
            Row(Modifier.fillMaxWidth().toggleable(value = settings.automatic,
                enabled = !settings.busy, role = Role.Checkbox, onValueChange = settings::chooseAutomatic)) {
                Checkbox(checked = settings.automatic, onCheckedChange = null, enabled = !settings.busy)
                Text("Check automatically once a day")
            }
            Text("Checks contact comicarchiver.com. No account or library information is sent.")
            TextButton(onClick = settings::check, enabled = !settings.busy) { Text("Check for updates") }
        }
        if (settings.available) TextButton(onClick = {
            failed = try { browser.openUri("https://comicarchiver.com/download.html"); false }
                     catch (_: Exception) { true }
        }) { Text("Get the update") }
        if (failed) Text("Could not open the download page. Try again.")
    }
}
