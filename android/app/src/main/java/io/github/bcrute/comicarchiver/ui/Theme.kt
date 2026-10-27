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
    ) else lightColorScheme(
        primary = Color(0xFF17665B), onPrimary = Color.White,
        primaryContainer = Color(0xFFD3EEE6), onPrimaryContainer = Color(0xFF00201A),
        surface = Color(0xFFFAFAF6), background = Color(0xFFFAFAF6),
    )
    MaterialTheme(colorScheme = colors, content = content)
}
