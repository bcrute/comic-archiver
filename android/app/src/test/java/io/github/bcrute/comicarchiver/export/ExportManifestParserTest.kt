package io.github.bcrute.comicarchiver.export

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * Focused tests for the manifest schema consumption and the relpath confinement — the two
 * places where a wrong assumption would either fabricate exports or leak app-private
 * files. Uses org.json (requested as a testImplementation dependency for the JVM suite;
 * on device it is the platform's own classes).
 */
class ExportManifestParserTest {

    private fun entryJson(
        episodeId: String = "ep1",
        seriesId: String = "s1",
        kind: String = "CBZ",
        filename: String = "ep1.cbz",
        bytes: Long = 12,
        sha256: String = "a".repeat(64),
        relpath: String = "s1/ep1.cbz",
    ) = """{"seriesId":"$seriesId","episodeId":"$episodeId","kind":"$kind",
            "filename":"$filename","bytes":$bytes,"sha256":"$sha256","relpath":"$relpath"}"""
            .replace("\n", "")

    @Test
    fun `parses entries from result entries object`() {
        val envelope = """{"v":1,"ok":true,"result":{"entries":[${entryJson()}]}}"""
        val parsed = ExportManifestParser.parse(envelope)
        assertFalse(parsed.refused)
        assertEquals(1, parsed.entries.size)
        val entry = parsed.entries[0]
        assertEquals(ExportKind.CBZ, entry.kind)
        assertEquals("ep1", entry.episodeId)
        assertEquals(12L, entry.bytes)
        assertEquals("s1/ep1.cbz", entry.relpath)
    }

    @Test
    fun `tolerates a bare result array`() {
        val envelope = """{"v":1,"ok":true,"result":[${entryJson(kind = "epub")}]}"""
        val parsed = ExportManifestParser.parse(envelope)
        assertEquals(1, parsed.entries.size)
        assertEquals(ExportKind.EPUB, parsed.entries[0].kind)
    }

    @Test
    fun `kind matches case-insensitively`() {
        val parsed = ExportManifestParser.parse(
            """{"v":1,"ok":true,"result":{"entries":[${entryJson(kind = "cbz")}]}}""",
        )
        assertEquals(ExportKind.CBZ, parsed.entries.single().kind)
    }

    @Test
    fun `a refused envelope is a refusal, never an empty success`() {
        val parsed = ExportManifestParser.parse(
            """{"v":1,"ok":false,"code":"not_implemented",
                "message":"The operation is implemented by the native line."}""".replace("\n", ""),
        )
        assertTrue(parsed.refused)
        assertEquals("not_implemented", parsed.refusedCode)
        assertTrue(parsed.entries.isEmpty())
    }

    @Test
    fun `unusable entries are counted, not dropped silently`() {
        val badSha = entryJson(episodeId = "ep2", sha256 = "nothex")
        val badKind = entryJson(episodeId = "ep3", kind = "mobi")
        val missingField = """{"seriesId":"s1"}"""
        val parsed = ExportManifestParser.parse(
            """{"v":1,"ok":true,"result":{"entries":[${entryJson()},$badSha,$badKind,$missingField]}}""",
        )
        assertEquals(1, parsed.entries.size)
        assertEquals(3, parsed.invalid)
    }
}

/** The relpath confinement: only files inside the app-private Library may be read. */
class ResolveOriginalTest {

    @get:Rule
    val tmp = TemporaryFolder()

    // newFolder throws IOException when the folder already exists and the getter runs
    // more than once per test; mkdirs is the idempotent form of the same fixture.
    private val library: File
        get() = File(tmp.root, "Library").apply { mkdirs() }

    @Test
    fun `resolves a file under the library`() {
        val file = File(library, "s1/ep1.cbz").apply { parentFile.mkdirs(); writeBytes(byteArrayOf(1)) }
        assertEquals(file.canonicalFile, SafExportRuntime.resolveOriginal(library, "s1/ep1.cbz"))
    }

    @Test
    fun `refuses escapes, absolutes and blanks`() {
        assertNull(SafExportRuntime.resolveOriginal(library, "../secrets.json"))
        assertNull(SafExportRuntime.resolveOriginal(library, "/etc/hosts"))
        assertNull(SafExportRuntime.resolveOriginal(library, ""))
        assertNull(SafExportRuntime.resolveOriginal(library, "   "))
    }

    @Test
    fun `refuses the library root itself and missing files`() {
        assertNull(SafExportRuntime.resolveOriginal(library, "."))
        assertNull(SafExportRuntime.resolveOriginal(library, "s1/absent.cbz"))
    }
}
