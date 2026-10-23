package io.github.bcrute.comicarchiver.library

import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.ParcelFileDescriptor
import androidx.core.content.FileProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File
import java.io.FileNotFoundException
import java.nio.file.Files
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryLocations
import io.github.bcrute.comicarchiver.runtime.WorkingLibrarySource
import io.github.bcrute.comicarchiver.runtime.WorkingLibraryStart

/** Real manifest/provider/path handling and temporary grant flags; a captured launcher
 * does not claim that an external reader rendered the archive. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class ArchiveViewerTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val root get() = File(context.filesDir, "workspace/Library")
    private fun archive(name: String = "Series One/chapter.cbz"): File = File(root, name).apply {
        parentFile!!.mkdirs(); writeText("synthetic archive bytes")
    }
    private fun uri(file: File) = FileProvider.getUriForFile(context, context.packageName + ".archives", file)
    private fun provider() = ArchiveFileProvider().also {
        it.attachInfo(context, context.packageManager.resolveContentProvider(
            context.packageName + ".archives", PackageManager.GET_META_DATA)!!)
    }

    @Before fun attachProviderForThisApplicationDirectory() {
        // Android attaches manifest providers before activities. Robolectric gives
        // each test a new filesDir while AndroidX's authority cache survives; real
        // attachInfo invalidates that cache for this application's current root.
        provider()
    }

    @Test fun viewerPassesOnlyTheSelectedContentUriWithReadPermission() {
        val file = archive()
        val intents = mutableListOf<Intent>()
        val result = ArchiveViewer(context, root) { intents.add(it) }.open("Series One/chapter.cbz", "cbz")
        assertEquals(ArchiveOpenResult.REQUESTED, result)
        val intent = intents.single()
        assertEquals(Intent.ACTION_VIEW, intent.action)
        assertEquals("content", intent.data!!.scheme)
        assertEquals("application/vnd.comicbook+zip", intent.type)
        assertEquals(uri(file), intent.data)
        assertEquals(intent.data, intent.clipData!!.getItemAt(0).uri)
        assertTrue(intent.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
        assertEquals(0, intent.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertEquals(0, intent.flags and Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
        assertFalse(intent.data.toString().contains(context.filesDir.absolutePath))
    }

    @Test fun managedGrantsKeepTheirOriginalLibraryAfterRefreshingAndRefusePrivateOrPendingPaths() {
        val locations = WorkingLibraryLocations(context.filesDir)
        val source = WorkingLibrarySource("a".repeat(64), "content://fixture.documents/tree/source")
        fun create(bytes: String): Pair<String, File> {
            val ticket = (locations.begin("Copy", source, refresh = true) as WorkingLibraryStart.Copy).pending
            val file = locations.copyRoot(ticket.id).resolve("Library/Series/chapter.cbz")
            file.parentFile!!.mkdirs(); file.writeText(bytes)
            locations.verified(ticket.id, "b".repeat(64)); val ready = locations.complete(ticket.id)
            return ready.id to locations.root(ready.id).resolve("Library/Series/chapter.cbz")
        }
        val (oldId, old) = create("old public bytes"); val oldUri = uri(old)
        val (newId, fresh) = create("new public bytes"); locations.select(newId)
        val provider = provider()
        fun read(address: Uri) = ParcelFileDescriptor.AutoCloseInputStream(provider.openFile(address, "r")).use { it.readBytes() }
        assertEquals("managed-libraries", oldUri.pathSegments.first())
        assertEquals(oldId, oldUri.pathSegments[1])
        assertNotEquals(oldUri, uri(fresh))
        assertArrayEquals(old.readBytes(), read(oldUri)); assertArrayEquals(fresh.readBytes(), read(uri(fresh)))
        val hidden = locations.root(oldId).resolve(".archive-state/private.cbz").apply { parentFile!!.mkdirs(); writeText("PRIVATE") }
        assertThrows(FileNotFoundException::class.java) { provider.openFile(uri(hidden), "r") }
        val pending = (locations.begin("Pending", source, refresh = true) as WorkingLibraryStart.Copy).pending
        val staged = locations.copyRoot(pending.id).resolve("Library/Series/chapter.cbz").apply { parentFile!!.mkdirs(); writeText("pending") }
        assertThrows(FileNotFoundException::class.java) { provider.openFile(uri(staged), "r") }
        assertThrows(FileNotFoundException::class.java) { provider.openFile(oldUri, "rw") }
    }

    @Test fun noReaderIsReportedAndNoUnsupportedOrMissingFileIsLaunched() {
        archive()
        val viewer = ArchiveViewer(context, root) { throw ActivityNotFoundException() }
        assertEquals(ArchiveOpenResult.NO_READER, viewer.open("Series One/chapter.cbz", "cbz"))
        assertEquals(ArchiveOpenResult.UNAVAILABLE, viewer.open("Series One/chapter.cbz", "epub"))
        assertEquals(ArchiveOpenResult.UNAVAILABLE, viewer.open("../.comic-session/android.json", "cbz"))
        assertEquals(ArchiveOpenResult.UNAVAILABLE, viewer.open("missing.cbz", "cbz"))
    }

    @Test fun providerServesArchiveBytesAndRefusesWriteAndPrivateState() {
        val file = archive()
        val provider = provider()
        val address = uri(file)
        assertEquals("application/vnd.comicbook+zip", provider.getType(address))
        val bytes = ParcelFileDescriptor.AutoCloseInputStream(provider.openFile(address, "r")).use { it.readBytes() }
        assertArrayEquals(file.readBytes(), bytes)
        assertThrows(FileNotFoundException::class.java) { provider.openFile(address, "rw") }
        val privateFile = File(root, ".archive-state/state.cbz").apply { parentFile!!.mkdirs(); writeText("private fixture") }
        assertThrows(FileNotFoundException::class.java) { provider.openFile(uri(privateFile), "r") }
        assertThrows(FileNotFoundException::class.java) {
            provider.openFile(Uri.parse("content://${context.packageName}.archives/library/../state.cbz"), "r")
        }
    }

    @Test fun aFileReplacedWithAnInternalSymlinkAfterUriCreationIsRefusedAtOpenTime() {
        val file = archive()
        val address = uri(file)
        val privateFile = File(root, ".archive-state/private").apply { parentFile!!.mkdirs(); writeText("private fixture") }
        assertTrue(file.delete())
        Files.createSymbolicLink(file.toPath(), privateFile.toPath())
        assertNull(resolveArchive(root, "Series One/chapter.cbz"))
        assertThrows(FileNotFoundException::class.java) { provider().openFile(address, "r") }
    }

    @Test fun providerRootsOpenOnlyTheirOwnFinishedArchivesAndPreserveTheLegacyUri() {
        val provider = provider()
        val roots = mapOf("library" to File(context.filesDir, "workspace/Library"),
            "webtoon-library" to File(context.filesDir, "workspace/Webtoon/Library"),
            "tappytoon-library" to File(context.filesDir, "workspace/Tappytoon/Library"),
            "opened-cache" to File(context.cacheDir, "opened-archives"))
        roots.forEach { (identity, selected) ->
            val file = File(selected, "Same Series/chapter.cbz").apply {
                parentFile!!.mkdirs(); writeText("$identity fixture")
            }
            val address = uri(file)
            assertEquals(identity, address.pathSegments.first())
            val bytes = ParcelFileDescriptor.AutoCloseInputStream(provider.openFile(address, "r")).use { it.readBytes() }
            assertArrayEquals(file.readBytes(), bytes)
            val intents = mutableListOf<Intent>()
            assertEquals(ArchiveOpenResult.REQUESTED,
                ArchiveViewer(context, selected) { intents.add(it) }.open("Same Series/chapter.cbz", "cbz"))
            assertEquals(address, intents.single().data)
            assertEquals(Intent.FLAG_GRANT_READ_URI_PERMISSION,
                intents.single().flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION))
            assertThrows(FileNotFoundException::class.java) { provider.openFile(address, "w") }
            val hidden = File(selected, ".archive-state/private.cbz").apply {
                parentFile!!.mkdirs(); writeText("private fixture")
            }
            assertThrows(FileNotFoundException::class.java) { provider.openFile(uri(hidden), "r") }
            assertThrows(FileNotFoundException::class.java) {
                provider.openFile(Uri.parse("content://${context.packageName}.archives/$identity/../.comic-session/state.cbz"), "r")
            }
        }
    }
}
