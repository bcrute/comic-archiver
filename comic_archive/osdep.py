"""What Linux and Windows do differently with files, locks and processes, kept in one place.

The rest of the app asks here and stays the same on both. Nothing in this
module knows about libraries, sites or accounts.
"""
import errno
import os
from pathlib import Path, PurePath
import signal
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'
# Windows has none of these flags. Every caller also refuses a symlink by name before it opens anything.
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
# On Windows a file another process has open cannot be replaced until that process lets go of it,
# which a reader of a status file does within milliseconds. Replacing waits this long for that.
REPLACE_WAIT_SECONDS = 5
# Windows ends a whole path at 259 characters unless a setting that most computers leave off is changed.
PATH_LIMIT = 259 if WINDOWS else None
# A library may be moved later. Names are cut as if the path of its folder were at least this long, so that
# they still fit in any folder up to that depth.
LIBRARY_PATH_ROOM = 100
# What is left of a name when its folder leaves no room: enough to tell one from another.
SHORTEST_NAME = 8


if WINDOWS:
    import msvcrt

    def lock(descriptor):
        """Take the lock on this open file, or raise BlockingIOError when another holder has it."""
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EDEADLK):
                raise
            raise BlockingIOError(error.errno, 'The file is locked by another holder.') from None

    def unlock(descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock(descriptor):
        """Take the lock on this open file, or raise BlockingIOError when another holder has it."""
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(descriptor):
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def replace_file(source, target):
    """Put one file in the place of another in a single step, as os.replace does."""
    deadline = time.monotonic() + REPLACE_WAIT_SECONDS
    while True:
        try:
            return os.replace(source, target)
        except PermissionError:
            # On Linux this is a folder that cannot be written to, and waiting changes nothing.
            if not WINDOWS or time.monotonic() >= deadline:
                raise
        time.sleep(.02)


def sync_directory(path):
    """Have a rename or removal inside this folder reach the disk.

    Windows cannot open a folder to flush it; its file system records renames in its own journal.
    """
    if WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def shortened(name, library, before, after):
    """A name cut so that its whole path stays one Windows accepts: the library folder, 'before' characters
    of folders and separators inside it, the name, and 'after' more characters behind the name.

    Unchanged where paths have no such limit. Windows counts in UTF-16 units, in which one emoji is two;
    use units() for 'before'. Nothing is cut that the name's own identifier would be needed to tell apart:
    callers put that identifier behind the name, inside 'after'.
    """
    if PATH_LIMIT is None:
        return name
    room = max(SHORTEST_NAME, PATH_LIMIT - max(units(library), LIBRARY_PATH_ROOM) - before - after)
    return name.encode('utf-16-le')[:room * 2].decode('utf-16-le', 'ignore')


def units(path):
    """The length of a path or name as Windows counts it."""
    return len(str(path).encode('utf-16-le')) // 2


def anchored(path):
    """Whether a path names its own starting point (a root or a drive) rather than lying under the folder it is joined to.

    On Windows '/x' and 'C:x' are not absolute paths, yet joined to a folder either one leaves that folder.
    """
    return bool((path if isinstance(path, PurePath) else Path(path)).anchor)


def sync_file(path):
    """Have a finished file's contents reach the disk.

    Windows flushes only through a handle that may write, so the file is opened for that there.
    """
    with open(path, 'r+b' if WINDOWS else 'rb') as stream:
        os.fsync(stream.fileno())


def disk_bytes(path):
    """The room a file takes on its disk, which for a sparse or tiny file is not its length."""
    info = os.stat(path)
    blocks = getattr(info, 'st_blocks', None)
    return info.st_size if blocks is None else blocks * 512


def open_log(path):
    """A descriptor for a private log that only ever adds to its end, to give a child process as its output.

    Several processes write to the same log. Appending is the system's promise on Linux; on Windows it
    is only that when the file is opened with the right to append and without the right to write elsewhere.
    """
    if not WINDOWS:
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    append_data, read_attributes, write_attributes, synchronize = 0x4, 0x80, 0x100, 0x100000
    share_all, open_always, normal, no_reparse = 0x7, 4, 0x80, 0x200000
    handle = kernel.CreateFileW(str(path), append_data | read_attributes | write_attributes | synchronize,
                                share_all, None, open_always, normal | no_reparse, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise


def python_environment(environ):
    """Set for the Pythons this app starts what its launcher script sets on Linux; Windows starts it from a shortcut.

    They read and write text the same way on every computer, and take no code from the person's
    own Python settings, packages or the folder they are started in.
    """
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        environ.pop(name, None)
    environ.update(PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTHONSAFEPATH='1')


def detached():
    """How to start a process that outlives the one starting it and gets none of its keyboard signals."""
    if WINDOWS:
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {'start_new_session': True}


def hidden():
    """How to start a console program from the window without a console window of its own showing."""
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def runs_command(pid, words):
    """Whether that process is one of the app's own, started with all of these words on its command line.

    Windows does not show another process's command line without far more machinery, so there the
    check is that the process is this app's own Python, which nothing else on the computer runs.
    """
    try:
        if WINDOWS:
            return os.path.normcase(_image(pid)) == os.path.normcase(sys.executable)
        given = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return all(os.fsencode(word) in given for word in words)
    except OSError:
        return False


def _image(pid):
    """The program file a Windows process was started from."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    query_limited_information = 0x1000
    handle = kernel.OpenProcess(query_limited_information, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return name.value
    finally:
        kernel.CloseHandle(handle)


def end_process(pid):
    """End a process that did not stop when asked, with the processes it started.

    Raises OSError when it could not be ended. On Linux the processes it started go with it only
    when it leads its own group: ending the group of a process that shares the caller's would end the caller.
    Windows has no such groups: the process itself is ended, and the browser engine's helper processes
    end themselves once the process that started them is gone.
    """
    if WINDOWS:
        os.kill(pid, signal.SIGTERM)  # On Windows this ends the process at once, whatever the signal.
    elif os.getpgid(pid) == pid:
        os.killpg(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)
