package io.github.bcrute.comicarchiver.runtime

import android.app.Application
import android.content.Intent
import android.net.Uri
import org.robolectric.RuntimeEnvironment
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34])
class FolderGrantsTest {
    private val resolver = RuntimeEnvironment.getApplication().contentResolver
    private val read = Intent.FLAG_GRANT_READ_URI_PERMISSION
    private val write = Intent.FLAG_GRANT_WRITE_URI_PERMISSION

    private fun tree(name: String) =
        Uri.parse("content://com.android.externalstorage.documents/tree/primary%3A$name")

    @Test
    fun releasesOnlyWhatNothingRecords() {
        resolver.takePersistableUriPermission(tree("Exports"), read or write)
        resolver.takePersistableUriPermission(tree("Old"), read)
        resolver.takePersistableUriPermission(tree("Opened"), read)

        val released = FolderGrants.releaseUnrecorded(resolver) { it == tree("Exports") || it == tree("Opened") }

        assertEquals(1, released)
        assertEquals(setOf(tree("Exports"), tree("Opened")), resolver.persistedUriPermissions.map { it.uri }.toSet())
    }

    @Test
    fun keepsEveryGrantWhenAllAreRecorded() {
        resolver.takePersistableUriPermission(tree("Library"), read)
        assertEquals(0, FolderGrants.releaseUnrecorded(resolver) { true })
        assertEquals(1, resolver.persistedUriPermissions.size)
    }
}
