package io.github.bcrute.comicarchiver.library

import java.io.File
import java.io.IOException
import java.io.InputStream
import java.util.UUID

/** At most three unused reader copies, never imports into the download library.
 * Publish only after EOF/size checks; partial or cancelled files have no readable
 * archive name. Explicit owners survive eviction until they release their copy;
 * external readers may instead retain an open descriptor after eviction. */
internal class ReadingCopyStore(private val directory: File,
    private val prune: (List<File>) -> Unit = { copies -> copies.drop(2).forEach { it.deleteRecursively() } },
    private val available: (File) -> Long = { it.usableSpace }) {
    fun stage(name: String, expectedBytes: Long?, source: () -> InputStream,
        checkActive: () -> Unit = {}): File {
        val kind = name.substringAfterLast('.', "")
        require(archiveMime(kind) != null)
        require(name.isNotBlank() && !name.startsWith('.') && name.none { it == '/' || it == '\\' || it.code < 32 })
        require(expectedBytes == null || expectedBytes > 0)
        checkActive()
        check(directory.mkdirs() || directory.isDirectory)
        val root = directory.canonicalFile
        val old = root.listFiles()?.filter { it.isDirectory && it.name.matches(Regex("[a-f0-9-]{36}")) &&
            it.canonicalFile == it.absoluteFile }?.sortedByDescending { it.lastModified() } ?: emptyList()
        // Keep two unused copies while preparing the third. Live owners are
        // independent of this cache budget and retire after their actual I/O.
        prune(old)
        val budget = (available(root) - 16L * 1024 * 1024).coerceAtLeast(0)
        if (expectedBytes != null && expectedBytes > budget) throw IOException("Temporary reading space unavailable")
        val folder = root.resolve(UUID.randomUUID().toString())
        check(folder.mkdir())
        val filename = if (name.toByteArray(Charsets.UTF_8).size <= 240) name else "Saved archive.$kind"
        val scratch = folder.resolve("reading.part")
        val result = folder.resolve(filename)
        var finished = false
        try {
            var count = 0L
            source().use { input ->
                scratch.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        checkActive()
                        val size = input.read(buffer)
                        if (size < 0) break
                        if (size == 0) throw IOException("Reading source made no progress")
                        count += size
                        if (count > budget || (expectedBytes != null && count > expectedBytes))
                            throw IOException("Reading source changed or temporary space is unavailable")
                        output.write(buffer, 0, size)
                    }
                }
            }
            checkActive()
            if (count == 0L || (expectedBytes != null && expectedBytes != count))
                throw IOException("Reading source is incomplete")
            if (!scratch.renameTo(result)) throw IOException("Reading copy could not be published")
            finished = true
            return result
        } finally { if (!finished) folder.deleteRecursively() }
    }
}
