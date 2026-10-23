package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.signin.SiteAddress
import java.io.File

/** Native handles for the core's sites.folder mapping; never rendered as UI text. */
data class SiteWorkspace(val key: String, val name: String, val base: File, val root: File) {
    val session: File get() = File(root, ".comic-session")
    val library: File get() = File(root, "Library")
    val network: File get() = File(root, "net.json")

    companion object {
        fun from(base: File, key: String, address: SiteAddress): SiteWorkspace {
            val folder = address.workspaceFolder
            require(if (key == "tapas") folder.isEmpty()
                    else folder.matches(Regex("[A-Za-z0-9][A-Za-z0-9 _-]{0,40}"))) {
                "The site's workspace mapping is unavailable."
            }
            return SiteWorkspace(key, address.name.ifBlank { key }, base,
                if (folder.isEmpty()) base else File(base, folder))
        }
    }
}
