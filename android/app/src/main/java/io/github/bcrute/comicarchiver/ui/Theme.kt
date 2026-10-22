package io.github.bcrute.comicarchiver.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import io.github.bcrute.comicarchiver.controller.AppTheme

/** Native appearance follows the device unless the user saves an explicit choice. */
@Composable
fun ComicArchiverTheme(choice: AppTheme, content: @Composable () -> Unit) {
    val dark = when (choice) {
        AppTheme.SYSTEM -> isSystemInDarkTheme()
        AppTheme.LIGHT -> false
        AppTheme.DARK -> true
    }
    MaterialTheme(colorScheme = if (dark) darkColorScheme() else lightColorScheme(), content = content)
}
