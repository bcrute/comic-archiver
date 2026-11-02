package io.github.bcrute.comicarchiver.ui

import android.content.Context
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Said on About and over the licenses, as on the desktop's About tab. */
internal const val LICENSE_SUMMARY = "Copyright 2026 Ben Crute. Comic Archiver is free software under the MIT " +
    "license. It is built with other people’s software, each under its own license; every one of those " +
    "licenses can be read here."

/** A license text the app carries, by its place under the licenses folder of its assets. */
internal data class LicenseText(val title: String, val path: String)

/** What a text is called, as on the desktop: other/LGPL-3.0.txt is LGPL-3.0, a package's text bears its name. */
internal fun licenseTitle(path: String): String = when {
    path == "LICENSE" -> "Comic Archiver (MIT license)"
    path == "THIRD-PARTY-NOTICES.md" -> "Third-party notices"
    path.startsWith("other/") -> path.removePrefix("other/").removeSuffix(".txt")
    else -> path.substringBefore('/') + ": " + path.substringAfterLast('/')
}

/**
 * Every text, in reading order: the app's own license and notices, the texts no package brings, then each
 * package's own. list names what a folder holds, and nothing for a file, as Android's assets do.
 */
internal fun licenseTexts(list: (String) -> List<String>): List<LicenseText> {
    fun files(folder: String): List<String> = list(folder).flatMap { name ->
        val path = if (folder.isEmpty()) name else "$folder/$name"
        if (list(path).isEmpty()) listOf(path) else files(path)
    }
    val rank = { path: String ->
        when {
            path == "LICENSE" -> 0
            path == "THIRD-PARTY-NOTICES.md" -> 1
            path.startsWith("other/") -> 2
            else -> 3
        }
    }
    return files("").sortedWith(compareBy<String>({ rank(it) }, { it })).map { LicenseText(licenseTitle(it), it) }
}

private fun assetTexts(context: Context): List<LicenseText> = licenseTexts { folder ->
    context.assets.list(if (folder.isEmpty()) "licenses" else "licenses/$folder")?.toList().orEmpty()
}

/** Each license the app carries, opened one at a time. The texts are read off the UI thread. */
@Composable
fun LicensesScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current.applicationContext
    val texts by produceState<List<LicenseText>?>(null, context) {
        value = withContext(Dispatchers.IO) { runCatching { assetTexts(context) }.getOrDefault(emptyList()) }
    }
    var open by rememberSaveable { mutableStateOf<String?>(null) }
    val text by produceState<String?>(null, open) {
        value = null
        value = open?.let { path ->
            withContext(Dispatchers.IO) {
                runCatching { context.assets.open("licenses/$path").bufferedReader().use { it.readText() } }.getOrNull()
            } ?: "This text could not be read."
        }
    }
    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Licenses", style = MaterialTheme.typography.headlineSmall)
        Text(LICENSE_SUMMARY, style = MaterialTheme.typography.bodyMedium)
        when (val found = texts) {
            null -> Text("Reading licenses…")
            else -> {
                if (found.isEmpty()) Text("The license texts could not be read.")
                found.forEach { item ->
                    TextButton(onClick = { open = if (open == item.path) null else item.path }) { Text(item.title) }
                    if (open == item.path) {
                        Text(text ?: "Reading…", style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
                    }
                }
            }
        }
    }
}
