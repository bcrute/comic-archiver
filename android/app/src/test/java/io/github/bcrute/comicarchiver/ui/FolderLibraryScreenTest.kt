package io.github.bcrute.comicarchiver.ui

import android.app.Application
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import io.github.bcrute.comicarchiver.controller.CatalogState
import io.github.bcrute.comicarchiver.library.FolderLibraryState
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], qualifiers = "w393dp-h1000dp")
class FolderLibraryScreenTest {
    @get:Rule val compose = createComposeRule()
    @Test fun signedOutFolderEntryOffersSelectionWithoutAnyDownloadOrExportAction() {
        var choose = 0; var load = 0
        compose.setContent { MaterialTheme { FolderLibraryScreen(FolderLibraryState(), { load++ },
            { choose++ }, {}, { _, _ -> }, {}) } }
        compose.onNodeWithText("Open library folder").performScrollTo().performClick()
        assertEquals(1, choose)
        assertEquals(1, load)
    }
    @Test fun interruptedFolderChoiceCanBeClosedAndRevokedAccessOffersRepick() {
        var close = 0
        compose.setContent { MaterialTheme { FolderLibraryScreen(FolderLibraryState(selected = true,
            label = "Portable library", choosing = true, needsFolder = true, catalog = CatalogState()),
            {}, {}, { close++ }, { _, _ -> }, {}) } }
        compose.onNodeWithText("Reading folder…").assertIsDisplayed()
        compose.onNodeWithText("Change library folder").assertIsNotEnabled()
        compose.onNodeWithText("Choose the folder again to restore access.").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("Close folder").performScrollTo().performClick()
        assertEquals(1, close)
    }
}
