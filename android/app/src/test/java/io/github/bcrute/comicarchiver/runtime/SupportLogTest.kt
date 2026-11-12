package io.github.bcrute.comicarchiver.runtime

import org.junit.Assert.assertEquals
import org.junit.Test

class SupportLogTest {
    @Test
    fun thePhonesSystemIsNamedInLettersTheCoreAccepts() {
        assertEquals("Android 15 (API 35)", SupportLog.system("15", 35))
        assertEquals("Android 16 QPR1 Beta (API 36)", SupportLog.system("16 QPR1 Beta", 36))
        assertEquals("Android (API 29)", SupportLog.system(null, 29))
        assertEquals("Android (API 29)", SupportLog.system("  ", 29))
        // A maker's own release text never carries a line, a path or a long story into the details.
        assertEquals("Android 15xdata (API 35)", SupportLog.system("15\nx/data(\u00e9)", 35))
        assertEquals("Android ${"9".repeat(20)} (API 35)", SupportLog.system("9".repeat(64), 35))
    }
}
