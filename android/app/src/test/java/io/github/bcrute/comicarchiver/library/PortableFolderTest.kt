package io.github.bcrute.comicarchiver.library

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.provider.DocumentsContract.Document
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

internal class FakePortableDocuments : PortableDocuments {
    var allowed = true
    var rootDocument = PortableDocument("root", "Project", Document.MIME_TYPE_DIR, null, null)
    val entries = mutableMapOf<String, List<PortableDocument>>()
    val values = mutableMapOf<String, JSONObject>()
    val readNames = mutableListOf<String>()
    val queried = mutableListOf<String>()
    val opened = mutableListOf<String>()
    var beforeRoot: (() -> Unit)? = null
    override fun retain(tree: Uri) { check(allowed) }
    override fun readable(tree: Uri) = allowed
    override fun root(tree: Uri): PortableDocument { beforeRoot?.invoke(); check(allowed); return rootDocument }
    override fun children(tree: Uri, parent: PortableDocument): List<PortableDocument> {
        queried.add(parent.id); return entries[parent.id] ?: emptyList()
    }
    override fun metadata(tree: Uri, document: PortableDocument, maximum: Int): PortableMetadata {
        readNames.add(document.name)
        val value = values[document.id]
        return PortableMetadata(value, value?.toString()?.toByteArray()?.size ?: 0)
    }
    override fun current(tree: Uri, document: PortableDocument) = allowed && opened.none { it == "changed" }
    override fun open(tree: Uri, document: PortableDocument): ArchiveOpenResult {
        opened.add(document.id); return ArchiveOpenResult.REQUESTED
    }
    fun file(id: String, name: String, flags: Int = 0, bytes: Long? = 20) =
        PortableDocument(id, name, "application/octet-stream", bytes, 1700000000, flags)
    fun directory(id: String, name: String) = PortableDocument(id, name, Document.MIME_TYPE_DIR, null, null)
    fun simple() {
        entries["root"] = listOf(file("comic", "One.cbz"), file("novel", "Two.epub"))
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class PortableFolderTest {
    private val tree = Uri.parse("content://fixture.documents/tree/root")

    @Test fun projectLibraryAndSidecarsAreReadWithoutEnteringPrivateDirectoriesOrArchiveBytes() {
        val docs = FakePortableDocuments()
        docs.entries["root"] = listOf(docs.directory("library", "Library"), docs.directory("session", ".comic-session"))
        docs.entries["library"] = listOf(docs.directory("series", "Series"), docs.directory("state", ".archive-state"))
        docs.entries["series"] = listOf(docs.file("comic", "One.cbz"), docs.file("details", "One.json"),
            docs.file("series-info", "series.json"), docs.file("secret", "state.json"))
        docs.values["details"] = JSONObject().put("title", "Chapter one")
        val result = PortableFolderScanner(docs).scan(tree)
        assertEquals(listOf("root", "library", "series"), docs.queried)
        assertEquals(setOf("One.json", "series.json"), docs.readNames.toSet())
        assertEquals(1, result.manifest.getJSONArray("folders").length())
        assertFalse(result.manifest.toString().contains("state.json"))
        assertFalse(result.manifest.toString().contains("content://"))
    }

    @Test fun exportedSeriesRequiresNoSidecarAndKeysStayStableUntilTheSelectedTreeChanges() {
        val docs = FakePortableDocuments().apply { simple() }
        val scanner = PortableFolderScanner(docs)
        val first = scanner.scan(tree)
        assertEquals(first.documents.keys, scanner.scan(tree).documents.keys)
        assertTrue(first.documents.keys.intersect(scanner.scan(Uri.parse("content://other.documents/tree/root"))
            .documents.keys).isEmpty())
        assertTrue(docs.readNames.isEmpty())
    }

    @Test fun partialVirtualHiddenAndZeroLengthFilesAreNotOfferedAsFinishedArchives() {
        val docs = FakePortableDocuments()
        docs.entries["root"] = listOf(docs.file("partial", "Partial.cbz", Document.FLAG_PARTIAL),
            docs.file("virtual", "Virtual.epub", Document.FLAG_VIRTUAL_DOCUMENT), docs.file("hidden", ".hidden.cbz"),
            docs.file("empty", "Empty.cbz", bytes = 0), docs.file("valid", "Valid.epub", bytes = null))
        val result = PortableFolderScanner(docs).scan(tree)
        assertEquals(listOf("Valid.epub"), result.documents.values.map { it.name })
        assertTrue(result.manifest.getJSONArray("folders").getJSONObject(0).getJSONArray("files")
            .getJSONObject(0).isNull("bytes"))
        docs.rootDocument = docs.rootDocument.copy(flags = Document.FLAG_PARTIAL)
        assertThrows(IllegalStateException::class.java) { PortableFolderScanner(docs).scan(tree) }
    }

    @Test fun repeatedOrAmbiguousDocumentIdentitiesAndRevokedGrantsFailBeforeAnEmptyCatalogCanBeClaimed() {
        val docs = FakePortableDocuments().apply { simple() }
        docs.entries["root"] = listOf(docs.file("same", "One.cbz"), docs.file("same", "Two.epub"))
        assertThrows(IllegalStateException::class.java) { PortableFolderScanner(docs).scan(tree) }
        docs.allowed = false; docs.queried.clear()
        assertThrows(IllegalStateException::class.java) { PortableFolderScanner(docs).scan(tree) }
        assertTrue(docs.queried.isEmpty())
    }

    @Test fun pickerAndReaderGrantOnlyReadAccessToTheExactDocument() {
        val context = RuntimeEnvironment.getApplication()
        val picker = ReadOnlyLibraryTree().createIntent(context, null)
        assertEquals(0, picker.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertTrue(picker.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
        val target = Uri.parse("content://fixture.documents/tree/root/document/root%2FOne.cbz")
        var intent: Intent? = null
        assertEquals(ArchiveOpenResult.REQUESTED, requestArchiveOpen(context, target, "cbz") { intent = it })
        assertEquals(target, intent!!.data)
        assertEquals(target, intent!!.clipData!!.getItemAt(0).uri)
        assertEquals(0, intent!!.flags and (Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION))
        assertEquals(ArchiveOpenResult.UNAVAILABLE, requestArchiveOpen(context, tree, "tar.zst") { fail("No legacy reader intent") })
        assertEquals(ArchiveOpenResult.UNAVAILABLE, requestArchiveOpen(context, Uri.parse("file:///private/archive.cbz"), "cbz") { fail("No raw file intent") })
    }

    @Test fun portableMetadataDropsCredentialsSourceUrlsAndNestedPrivateRecords() {
        val original = JSONObject().put("title", "One").put("format", "cbz").put("episode_number", 2)
            .put("source_url", "https://private.invalid/account").put("cookie", "PRIVATE")
            .put("session", JSONObject().put("token", "PRIVATE"))
        val projected = projectPortableMetadata(original)
        assertEquals(setOf("title", "format", "episode_number"), projected.keys().asSequence().toSet())
        assertFalse(projected.toString().contains("PRIVATE"))
        assertFalse(projected.toString().contains("private.invalid"))
        assertTrue(original.has("cookie"))
    }
}
