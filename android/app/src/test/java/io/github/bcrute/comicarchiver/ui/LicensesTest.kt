package io.github.bcrute.comicarchiver.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class LicensesTest {
    private val folders = mapOf(
        "" to listOf("httpx", "other", "THIRD-PARTY-NOTICES.md", "LICENSE"),
        "httpx" to listOf("LICENSE.md"),
        "other" to listOf("python-3.13.txt", "Apache-2.0.txt"),
    )

    @Test fun textsReadInTheDesktopsOrderWithItsNames() {
        val texts = licenseTexts { folders[it].orEmpty() }
        assertEquals(
            listOf(
                LicenseText("Comic Archiver (MIT license)", "LICENSE"),
                LicenseText("Third-party notices", "THIRD-PARTY-NOTICES.md"),
                LicenseText("Apache-2.0", "other/Apache-2.0.txt"),
                LicenseText("python-3.13", "other/python-3.13.txt"),
                LicenseText("httpx: LICENSE.md", "httpx/LICENSE.md"),
            ),
            texts,
        )
    }

    @Test fun noAssetsMeanNoTexts() {
        assertEquals(emptyList<LicenseText>(), licenseTexts { emptyList() })
    }
}
