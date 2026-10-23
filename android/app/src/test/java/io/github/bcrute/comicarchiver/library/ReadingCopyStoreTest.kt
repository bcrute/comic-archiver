package io.github.bcrute.comicarchiver.library

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.concurrent.CancellationException

class ReadingCopyStoreTest {
    private fun work(run: (File) -> Unit) {
        val root = Files.createTempDirectory("reading-copy-").toFile()
        try { run(root) } finally { root.deleteRecursively() }
    }
    @Test fun exactBytesArePublishedOnlyAtCompletionAndOriginalRemainsUnchanged() = work { root ->
        val bytes = ByteArray(150000) { (it % 251).toByte() }
        val original = root.resolve("original.cbz").apply { writeBytes(bytes) }
        val cache = root.resolve("cache")
        var observedPartial = false
        val file = ReadingCopyStore(cache).stage("Original.cbz", bytes.size.toLong(), { original.inputStream() }) {
            observedPartial = observedPartial || cache.walkTopDown().any { it.name == "reading.part" }
            assertFalse(cache.walkTopDown().any { it.isFile && it.extension == "cbz" })
        }
        assertTrue(observedPartial)
        assertArrayEquals(bytes, file.readBytes())
        assertArrayEquals(bytes, original.readBytes())
        assertFalse(cache.walkTopDown().any { it.name.endsWith(".part") })
    }
    @Test fun truncationGrowthCancellationAndNoTemporarySpaceLeaveNoReadableCopy() = work { root ->
        val cases = listOf<Pair<Long?, () -> ByteArrayInputStream>>(20L to { ByteArrayInputStream(ByteArray(10)) },
            10L to { ByteArrayInputStream(ByteArray(20)) }, null to { ByteArrayInputStream(ByteArray(0)) })
        for ((index, case) in cases.withIndex()) {
            val cache = root.resolve("cache-$index")
            assertThrows(IOException::class.java) { ReadingCopyStore(cache).stage("One.cbz", case.first, case.second) }
            assertFalse(cache.walkTopDown().any { it.isFile })
        }
        val cancelled = root.resolve("cancelled")
        var checks = 0
        assertThrows(CancellationException::class.java) {
            ReadingCopyStore(cancelled).stage("One.cbz", null, { ByteArrayInputStream(ByteArray(200000)) }) {
                if (++checks >= 3) throw CancellationException()
            }
        }
        assertFalse(cancelled.walkTopDown().any { it.isFile })
        var sourceOpened = false
        assertThrows(IOException::class.java) {
            ReadingCopyStore(root.resolve("full")) { 0 }.stage("One.epub", 20,
                { sourceOpened = true; ByteArrayInputStream(ByteArray(20)) })
        }
        assertFalse(sourceOpened)
    }
    @Test fun oldTemporaryCopiesAreBoundedWithoutDeletingSourceArchivesOrUnownedFolders() = work { root ->
        val cache = root.resolve("cache").apply { mkdirs() }
        val unowned = cache.resolve("keep-me").apply { mkdirs(); resolve("notes.txt").writeText("KEEP") }
        val original = root.resolve("Original.epub").apply { writeBytes(byteArrayOf(1, 2, 3)) }
        val store = ReadingCopyStore(cache)
        repeat(6) {
            val file = store.stage("Original.epub", 3, { original.inputStream() })
            assertArrayEquals(original.readBytes(), file.readBytes())
        }
        assertEquals(3, cache.listFiles()!!.count { it.name.matches(Regex("[a-f0-9-]{36}")) })
        assertEquals("KEEP", unowned.resolve("notes.txt").readText())
        assertArrayEquals(byteArrayOf(1, 2, 3), original.readBytes())
    }
    @Test fun unsupportedNamesCannotWriteOutsideTheCacheAndLongNamesKeepTheirRealFormat() = work { root ->
        for (name in listOf("../One.cbz", ".hidden.cbz", "folder/One.epub", "state.json")) {
            assertThrows(IllegalArgumentException::class.java) {
                ReadingCopyStore(root.resolve("cache")).stage(name, 1, { ByteArrayInputStream(byteArrayOf(1)) })
            }
        }
        val file = ReadingCopyStore(root.resolve("cache")).stage("界".repeat(100) + ".epub", null,
            { ByteArrayInputStream(byteArrayOf(1)) })
        assertEquals("epub", file.extension)
        assertTrue(file.name.toByteArray(Charsets.UTF_8).size <= 240)
    }
}
