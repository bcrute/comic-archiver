package io.github.bcrute.comicarchiver.runtime

import android.system.ErrnoException
import android.system.OsConstants
import android.system.StructStat
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.shadows.ShadowLinux
import java.io.FileDescriptor
import java.io.FileInputStream
import java.io.IOException
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.NoSuchFileException
import java.nio.file.Paths
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.FileTime
import java.util.Collections
import java.util.IdentityHashMap

/** Host filesystem support for the location tests, not Android syscall qualification.
 * Robolectric 4.17 opens directories with RandomAccessFile, returns stat(null) for
 * fstat, and follows links in lstat. Use real host directory channels/force and
 * NOFOLLOW attributes; leave the product's Android syscall path unchanged.
 */
@Implements(className = "libcore.io.Linux", isInAndroidSdk = false, minSdk = 26)
class WorkingLibraryFileSystemShadow : ShadowLinux() {
    private data class Directory(val channel: FileChannel, val stat: StructStat)
    private val directories = Collections.synchronizedMap(IdentityHashMap<FileDescriptor, Directory>())

    private fun attributes(path: String): StructStat {
        try {
            val values = Files.readAttributes(Paths.get(path), "unix:mode,size,lastModifiedTime", LinkOption.NOFOLLOW_LINKS)
            return StructStat(0, 0, (values.getValue("mode") as Number).toInt(), 0, 0, 0, 0,
                (values.getValue("size") as Number).toLong(), 0,
                (values.getValue("lastModifiedTime") as FileTime).toMillis() / 1000, 0, 0, 0)
        } catch (error: NoSuchFileException) { throw ErrnoException("lstat", OsConstants.ENOENT, error) }
          catch (error: IOException) { throw ErrnoException("lstat", OsConstants.EIO, error) }
    }

    @Implementation
    override fun lstat(path: String): StructStat = attributes(path)

    @Implementation
    override fun open(path: String, flags: Int, mode: Int): FileDescriptor {
        if (!Files.exists(Paths.get(path), LinkOption.NOFOLLOW_LINKS)) return super.open(path, flags, mode)
        val entry = attributes(path)
        if ((flags and OsConstants.O_NOFOLLOW) != 0 && OsConstants.S_ISLNK(entry.st_mode))
            throw ErrnoException("open", OsConstants.ELOOP)
        if (!OsConstants.S_ISDIR(entry.st_mode)) return super.open(path, flags, mode)
        if ((flags and OsConstants.O_ACCMODE) != OsConstants.O_RDONLY) throw ErrnoException("open", OsConstants.EISDIR)
        try {
            val channel = FileChannel.open(Paths.get(path), StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)
            return FileDescriptor().also { directories[it] = Directory(channel, entry) }
        } catch (error: IOException) { throw ErrnoException("open", OsConstants.EIO, error) }
    }

    @Implementation
    override fun fstat(fd: FileDescriptor): StructStat = directories[fd]?.stat ?: super.fstat(fd)

    @Implementation
    fun fsync(fd: FileDescriptor) {
        val directory = directories[fd]
        if (directory == null) fd.sync() else directory.channel.force(true)
    }

    @Implementation
    fun close(fd: FileDescriptor) {
        val directory = directories.remove(fd)
        if (directory == null) FileInputStream(fd).close() else directory.channel.close()
    }
}
