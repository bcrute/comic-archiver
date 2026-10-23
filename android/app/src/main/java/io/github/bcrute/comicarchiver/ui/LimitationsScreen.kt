package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * The beta's truthful limitations, in plain words: what this build does not do, stated
 * where the person can see it before relying on any of it. Update together with the
 * public roadmap; never soften a limitation to make a date.
 */
@Composable
fun LimitationsScreen(modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("What this beta does not do", style = MaterialTheme.typography.headlineSmall)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Limitation("Choose a source site above. Each site keeps its sign-in and library separate.")
            Limitation("On devices without isolated browser profiles, saved libraries remain available; sign-in needs an updated Android System WebView.")
            Limitation("Tapas Recent and Free Episodes discovery and Fast mode need an updated Android System WebView. Settings shows whether Fast can run on this device.")
            Limitation("Saving uses unmetered connections by default. Settings lets you allow mobile data.")
            Limitation(
                "After an app update or process restart, saving waits until you press Go. " +
                    "Returning to an active save keeps its progress visible.",
            )
            Limitation(
                "Export copies files into one folder you choose; your originals stay in " +
                    "the app until every copy is verified.",
            )
            Limitation(
                "Long saves can be stopped by the system after a while; pressing Go " +
                    "continues them.",
            )
            Limitation("B2 storage uses your own private bucket. Connect and pass the upload/readback test before choosing it as a destination.")
            Limitation("Other cloud providers, image-quality options and listing services are not supported yet.")
            Limitation("Tapas closes at the end of March 2027; save what you bought before then.")
        }
    }
}

@Composable
private fun Limitation(text: String) {
    Text("· $text", style = MaterialTheme.typography.bodyMedium)
}
