package io.github.bcrute.comicarchiver.runtime

import android.content.ContentResolver
import android.content.Intent
import android.net.Uri

/** Persisted folder permissions outlive the feature that took them, and Android caps how
 * many an app may hold. One folder can serve export, the opened library and a working
 * library at once, so no feature releases its own; the app releases at start-up, before
 * any picker result can arrive, every permission that nothing records any more. */
internal object FolderGrants {
    fun releaseUnrecorded(resolver: ContentResolver, recorded: (Uri) -> Boolean): Int {
        var released = 0
        for (grant in resolver.persistedUriPermissions) {
            if (recorded(grant.uri)) continue
            val flags = (if (grant.isReadPermission) Intent.FLAG_GRANT_READ_URI_PERMISSION else 0) or
                (if (grant.isWritePermission) Intent.FLAG_GRANT_WRITE_URI_PERMISSION else 0)
            try {
                resolver.releasePersistableUriPermission(grant.uri, flags)
                released++
            } catch (_: SecurityException) {
                // Already gone; nothing is held.
            }
        }
        return released
    }
}
