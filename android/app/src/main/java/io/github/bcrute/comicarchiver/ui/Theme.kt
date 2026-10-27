package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import io.github.bcrute.comicarchiver.controller.AppTheme

/** Native appearance follows the device unless the user saves an explicit choice. */
@Composable
fun ComicArchiverTheme(choice: AppTheme, content: @Composable () -> Unit) {
    val dark = when (choice) {
        AppTheme.SYSTEM -> isSystemInDarkTheme()
        AppTheme.LIGHT -> false
        AppTheme.DARK -> true
    }
    val colors = if (dark) darkColorScheme(
        primary = Color(0xFFA2D4C8), onPrimary = Color(0xFF04382F),
        primaryContainer = Color(0xFF215047), onPrimaryContainer = Color(0xFFC0F0E3),
        surface = Color(0xFF111614), background = Color(0xFF111614),
        onSurface = Color(0xFFE1E7E0), onSurfaceVariant = Color(0xFFBEC9C0),
        secondaryContainer = Color(0xFF2F5147), onSecondaryContainer = Color(0xFFC0F0E3),
        surfaceContainer = Color(0xFF1A231E), surfaceContainerLow = Color(0xFF19211D),
        surfaceContainerHigh = Color(0xFF232D27), surfaceContainerHighest = Color(0xFF28332D),
    ) else lightColorScheme(
        primary = Color(0xFF17665B), onPrimary = Color.White,
        primaryContainer = Color(0xFFD3EEE6), onPrimaryContainer = Color(0xFF00201A),
        surface = Color(0xFFFAFAF6), background = Color(0xFFFAFAF6),
        onSurface = Color(0xFF18211B), onSurfaceVariant = Color(0xFF3F4D43),
        secondaryContainer = Color(0xFFD3EEE6), onSecondaryContainer = Color(0xFF00201A),
        surfaceContainer = Color(0xFFEEF2EC), surfaceContainerLow = Color(0xFFF3F6F0),
        surfaceContainerHigh = Color(0xFFE7ECE5), surfaceContainerHighest = Color(0xFFDEE6DC),
    )
    MaterialTheme(colorScheme = colors, content = content)
}
