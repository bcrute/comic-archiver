package io.github.bcrute.comicarchiver.worker

/**
 * The site workspaces the foreground service is keeping alive. Every site may be saving at
 * once, as on the desktop, under the one notification; the service ends with the last of them.
 */
object ActiveDownloads {
    private val roots = LinkedHashSet<String>()

    @Synchronized fun add(root: String) { roots.add(root) }

    /** True when that was the last one, so the service has nothing left to keep alive. */
    @Synchronized fun remove(root: String): Boolean { roots.remove(root); return roots.isEmpty() }

    @Synchronized fun all(): List<String> = roots.toList()

    @Synchronized fun contains(root: String): Boolean = root in roots

    @Synchronized fun clear() { roots.clear() }
}
