package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp

/**
 * Preview gallery over [UiFixtures], mapped through the same [UiMapping.kt] the working
 * app uses. Every preview renders the fixture label on screen so a screenshot can never
 * be mistaken for the working app. Preview-only; nothing here is referenced by screens
 * or wiring.
 */
@Preview(name = "Sign-in (fixture)", showBackground = true)
@Composable
fun SignInScreenFixturePreview() {
    FixtureScaffold {
        SignInScreen(UiFixtures.coldStart.toSignInScreenUi(), onStartSignIn = {})
    }
}

@Preview(name = "Downloads held (fixture)", showBackground = true)
@Composable
fun DownloadsHeldFixturePreview() {
    FixtureScaffold {
        DownloadsScreen(UiFixtures.heldAfterRestart.toDownloadsScreenUi(), onGo = {}, onPause = {})
    }
}

@Preview(name = "Downloads metered hold (fixture)", showBackground = true)
@Composable
fun DownloadsMeteredFixturePreview() {
    FixtureScaffold {
        DownloadsScreen(UiFixtures.meteredHold.toDownloadsScreenUi(), onGo = {}, onPause = {})
    }
}

@Preview(name = "Downloads running (fixture)", showBackground = true)
@Composable
fun DownloadsRunningFixturePreview() {
    FixtureScaffold {
        DownloadsScreen(UiFixtures.running.toDownloadsScreenUi(), onGo = {}, onPause = {})
    }
}

@Preview(name = "Downloads failed (fixture)", showBackground = true)
@Composable
fun DownloadsFailedFixturePreview() {
    FixtureScaffold {
        DownloadsScreen(UiFixtures.failed.toDownloadsScreenUi(), onGo = {}, onPause = {})
    }
}

@Preview(name = "Library export running (fixture)", showBackground = true)
@Composable
fun LibraryExportRunningFixturePreview() {
    FixtureScaffold {
        LibraryScreen(
            UiFixtures.library,
            UiFixtures.exportRunning,
            onPickFolder = {},
            onStartExport = {},
            onStopExport = {},
        )
    }
}

@Preview(name = "Library export grant lost (fixture)", showBackground = true)
@Composable
fun LibraryExportGrantLostFixturePreview() {
    FixtureScaffold {
        LibraryScreen(
            UiFixtures.library,
            UiFixtures.exportGrantLost,
            onPickFolder = {},
            onStartExport = {},
            onStopExport = {},
        )
    }
}

@Preview(name = "Limitations (fixture)", showBackground = true)
@Composable
fun LimitationsFixturePreview() {
    FixtureScaffold { LimitationsScreen() }
}

/** Wraps every preview with the unmissable fixture label. */
@Composable
private fun FixtureScaffold(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize()) {
        content()
        AssistChip(
            onClick = {},
            label = { Text(UiFixtures.LABEL) },
            modifier = Modifier.padding(8.dp),
        )
    }
}
