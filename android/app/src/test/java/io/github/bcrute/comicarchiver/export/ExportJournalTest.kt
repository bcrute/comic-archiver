package io.github.bcrute.comicarchiver.export

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** File-journal format tests: round trip, escaping, and tolerance of damage. */
class ExportJournalTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private fun entry(
        episodeId: String = "ep|1",
        seriesId: String = "Ser\\ies",
        name: String = "ep\n1.cbz",
    ) = JournalEntry(
        episodeId = episodeId,
        seriesId = seriesId,
        kind = ExportKind.EPUB,
        requestedName = name,
        actualName = "actual name.epub",
        bytes = 12L,
        sha256 = "a".repeat(64),
        state = JournalState.COPYING,
        targetUri = "content://tree/x|y",
        attempt = 3,
        failure = null,
    )

    @Test
    fun `round trips every field including separator characters`() {
        val file = File(tmp.root, "j.txt")
        val store = FileJournalStore(file)
        val journal = ExportJournal.of(
            listOf(
                entry(),
                entry(episodeId = "ep2").copy(
                    state = JournalState.VERIFIED,
                    targetUri = null,
                    failure = ExportFailure.STORAGE_LOW,
                ),
            ),
        )
        store.save(journal)

        val loaded = FileJournalStore(file).load()
        val first = loaded.get("ep|1")!!
        assertEquals("Ser\\ies", first.seriesId)
        assertEquals("ep\n1.cbz", first.requestedName)
        assertEquals(JournalState.COPYING, first.state)
        assertEquals("content://tree/x|y", first.targetUri)
        assertEquals(3, first.attempt)
        assertNull(first.failure)
        val second = loaded.get("ep2")!!
        assertEquals(JournalState.VERIFIED, second.state)
        assertNull(second.targetUri)
        assertEquals(ExportFailure.STORAGE_LOW, second.failure)
    }

    @Test
    fun `missing journal file loads empty`() {
        val journal = FileJournalStore(File(tmp.root, "absent.txt")).load()
        assertTrue(journal.all().isEmpty())
    }

    @Test
    fun `wrong or missing header loads empty rather than guessing the format`() {
        val file = File(tmp.root, "j.txt")
        file.writeText("something else v9\nnope\n")
        assertTrue(FileJournalStore(file).load().all().isEmpty())
    }

    @Test
    fun `corrupt lines are skipped, healthy lines survive`() {
        val file = File(tmp.root, "j.txt")
        FileJournalStore(file).save(ExportJournal.of(listOf(entry(episodeId = "good"))))
        // Append garbage after the healthy entries, as a torn write would leave.
        file.appendText("||||not an entry\ncompletely-broken\n")
        val loaded = FileJournalStore(file).load()
        assertEquals(1, loaded.all().size)
        assertEquals("good", loaded.get("good")?.episodeId)
    }

    @Test
    fun `empty optional fields round trip as nulls`() {
        val file = File(tmp.root, "j.txt")
        val e = entry().copy(targetUri = null, failure = null)
        FileJournalStore(file).save(ExportJournal.of(listOf(e)))
        val loaded = FileJournalStore(file).load().get(e.episodeId)!!
        assertNull(loaded.targetUri)
        assertNull(loaded.failure)
    }

    @Test fun `version one entries remain readable and are written as version two`() {
        val file = File(tmp.root, "legacy.txt")
        file.writeText("comicarchiver-export-journal v1\n" +
            "ep1|series|CBZ|one.cbz|one.cbz|3|${"a".repeat(64)}|VERIFIED|content://fake/tree/doc-1|1|\n")
        val store = FileJournalStore(file)
        val legacy = store.load().get("ep1")!!
        assertNull(legacy.targetTreeUri)
        val migrated = legacy.copy(targetTreeUri = "content://fake/tree")
        store.save(ExportJournal.of(listOf(migrated)))
        assertEquals("comicarchiver-export-journal v2", file.readLines().first())
        assertEquals(migrated, store.load().get("ep1"))
    }
}

/** Name policy for display names sent to providers. */
class SafeNameTest {

    @Test
    fun `strips separators and control characters`() {
        assertEquals("a_b_c", SafeName.of("a/b\\c", "fallback"))
        assertEquals("a_b_c_d_e_f_g_h", SafeName.of("a:b*c?d\"e<f>g|h", "fallback"))
        assertEquals("a_b", SafeName.of("a\u0000b", "fallback"))
    }

    @Test
    fun `trims edge dots and whitespace and falls back when empty`() {
        assertEquals("name", SafeName.of("  name.. ", "fallback"))
        assertEquals("fallback", SafeName.of(" / ", "fallback"))
        assertEquals("fallback", SafeName.of("", "fallback"))
    }

    @Test
    fun `bounds length`() {
        val long = "x".repeat(500)
        assertEquals(96, SafeName.of(long, "fallback").length)
    }

    @Test
    fun `bounds length without losing the archive extension`() {
        // The core's episode name: "NNN - <title up to 80> [<12 hex>].cbz".
        val episode = "001 - " + "t".repeat(80) + " [0123456789ab].cbz"
        val bounded = SafeName.of(episode, "fallback")
        assertEquals(96, bounded.length)
        assertEquals(ExportKind.CBZ, ExportKind.fromFilename(bounded))
        assertEquals(ExportKind.EPUB, ExportKind.fromFilename(SafeName.of("x".repeat(200) + ".epub", "fallback")))
    }
}
