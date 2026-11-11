"""What desktop platforms do differently with files, locks and processes, kept in one place.

The rest of the app asks here and stays the same on both. Nothing in this
module knows about libraries, sites or accounts.
"""
import errno
import os
from pathlib import Path, PurePath, PureWindowsPath
import signal
import stat
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
# And a file being replaced can refuse a reader for a moment, while the old one is still on its way out: reading
# waits this long for the new one.
READ_WAIT_SECONDS = 1
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

    def held(descriptor):
        """Whether another holder keeps the lock on this open file. Windows has no lock that two who only ask
        can share, so one found taken is asked about again: another asker has it for an instant, a holder
        for as long as it runs."""
        for _ in range(5):
            try:
                lock(descriptor)
            except BlockingIOError:
                time.sleep(.02)
                continue
            unlock(descriptor)
            return False
        return True
else:
    import fcntl

    def lock(descriptor):
        """Take the lock on this open file, or raise BlockingIOError when another holder has it."""
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(descriptor):
        fcntl.flock(descriptor, fcntl.LOCK_UN)

    def held(descriptor):
        """Whether another holder keeps the lock on this open file. Asked with a shared lock, so two who ask
        at once never see each other as a holder."""
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False


def private_file(descriptor):
    """Restrict a private file on Unix; Windows secret writers first protect the containing state tree's ACL.

    Windows Python before 3.13 has no fchmod. Permission bits on newer Windows
    Python only change read-only status, not who may read the file.
    """
    if not WINDOWS:
        os.fchmod(descriptor, 0o600)


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


def read_text(path, encoding=None):
    """A whole file's text, read as Path.read_text does, from a file another process may be replacing just then."""
    deadline = time.monotonic() + READ_WAIT_SECONDS
    while True:
        try:
            return Path(path).read_text(encoding=encoding)
        except PermissionError:
            # On Linux a replace never gets in a reader's way, so this is a file that cannot be read at all.
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


def hide(path):
    """Leave a private folder out of what the file manager shows, as its leading dot already does on Linux.

    Windows hides by a mark on the folder, which is added to the marks it has. A folder that cannot be
    marked is only shown; nothing depends on it being hidden.
    """
    if not WINDOWS:
        return
    import ctypes
    hidden_mark, invalid = 0x2, 0xFFFFFFFF
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFileAttributesW.restype = ctypes.c_uint32
        kernel.GetFileAttributesW.argtypes = (ctypes.c_wchar_p,)
        kernel.SetFileAttributesW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        marks = kernel.GetFileAttributesW(str(path))
        if marks != invalid and not marks & hidden_mark:
            kernel.SetFileAttributesW(str(path), marks | hidden_mark)
    except (AttributeError, OSError):
        pass


def keep_awake(running):
    """Keep Windows or macOS awake while a download runs; the screen may still turn off.

    The request belongs to the thread that makes it, so the same thread lifts it, and Windows drops it
    when that thread or the app ends. macOS uses an idle-sleep assertion tied to
    this process. Elsewhere this does nothing.
    """
    if sys.platform == 'darwin' and not WINDOWS:
        from .macos import keep_awake as mac_keep_awake
        mac_keep_awake(running)
        return
    if not WINDOWS:
        return
    import ctypes
    continuous, system_required = 0x80000000, 0x1
    try:
        kernel = ctypes.WinDLL('kernel32')
        kernel.SetThreadExecutionState.restype = ctypes.c_uint32
        kernel.SetThreadExecutionState.argtypes = (ctypes.c_uint32,)
        kernel.SetThreadExecutionState(continuous | (system_required if running else 0))
    except (AttributeError, OSError):
        pass


def extended_path(path):
    """An internal Windows metadata path can exceed Win32's legacy length limit.

    Call after normal directory/relocation guards. This is the same filesystem
    location, including on UNC shares; it never changes a saved archive's name.
    """
    if not WINDOWS:
        return Path(path)
    value = PureWindowsPath(path)
    if not value.is_absolute():
        raise ValueError('Extended metadata paths must be absolute.')
    text = str(value)
    prefix = '\\\\?\\'
    if text.startswith(prefix):
        return Path(text)
    return Path(prefix + 'UNC\\' + text[2:] if text.startswith('\\\\') else prefix + text)



def disk_bytes(path):
    """The room a file takes on its disk, which for a sparse or tiny file is not its length."""
    info = os.stat(path)
    blocks = getattr(info, 'st_blocks', None)
    return info.st_size if blocks is None else blocks * 512


def open_exclusive(path):
    """A descriptor for a fresh private file, created only where its name was unused and never through a symlink.

    Whatever already has the name — an interrupted earlier write's leftover, or a symlink planted
    at the name — gives the name up by that name alone, and the creation is tried once more; the
    target of a symlink is never opened or written, and a name taken again straight away is
    reported as taken rather than retried.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    except FileExistsError:
        os.unlink(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    private_file(descriptor)
    return descriptor


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


def linked(path):
    """Whether path is a link, or on Windows a junction: either leads somewhere else, and a folder can hold them."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or _junction(info)


def junction(path):
    """Whether path is a Windows junction: a folder that leads somewhere else, which Path.is_symlink does not see.
    A drive mounted in a folder is one too. Never true elsewhere."""
    try:
        return _junction(os.lstat(path))
    except OSError:
        return False


def _junction(info):
    # Only Windows has reparse tags, and the stat module names them only there.
    return WINDOWS and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def python_environment(environ):
    """Set for the Pythons this app starts what its launcher script sets on Linux; Windows starts it from a shortcut.

    They read and write text the same way on every computer, and take no code from the person's
    own Python settings, packages or the folder they are started in.
    """
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        environ.pop(name, None)
    environ.update(PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTHONSAFEPATH='1')


def python_child():
    """Launch a real interpreter PID while retaining a Windows virtual environment.

    Windows venv python.exe redirects to a second process. Use CPython's own
    multiprocessing convention so the PID returned by Popen is the worker's,
    and its imports still come from this environment. Packaged/Unix Pythons
    already launch directly.
    """
    base = getattr(sys, '_base_executable', None)
    if WINDOWS and sys.prefix != sys.base_prefix and base and base != sys.executable:
        child = os.environ.copy()
        child['__PYVENV_LAUNCHER__'] = sys.executable
        return base, {'env': child}
    return sys.executable, {}


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
        if sys.platform == 'darwin':
            from .macos import runs_command as mac_runs_command
            return mac_runs_command(pid, words)
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


# File systems reached over a network, as Linux names them in its list of mounts.
NETWORK_FILE_SYSTEMS = frozenset({'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', '9p', 'afs', 'ceph', 'glusterfs', 'davfs',
                                  'fuse.sshfs', 'fuse.rclone', 'fuse.gvfsd-fuse'})
WINDOWS_DRIVES = {2: 'removable', 3: 'local', 4: 'network', 5: 'disc', 6: 'memory'}


def drive(path, mounts=Path('/proc/self/mounts'), blocks=Path('/sys/class/block')):
    """(kind, file system) of the drive a folder is on; kind is local, removable, network, disc, memory or unknown."""
    try:
        if sys.platform == 'darwin' and not WINDOWS:
            from .macos import drive as mac_drive
            return mac_drive(path)
        return _windows_drive(path) if WINDOWS else _mounted_drive(path, mounts, blocks)
    except (AttributeError, OSError, ValueError):
        return 'unknown', 'unknown'


def _windows_drive(path):
    import ctypes
    anchor = Path(os.path.abspath(path)).anchor
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kind = WINDOWS_DRIVES.get(kernel.GetDriveTypeW(anchor), 'unknown')
    name = ctypes.create_unicode_buffer(64)
    known = kernel.GetVolumeInformationW(anchor, None, 0, None, None, None, name, len(name))
    return kind, name.value if known and name.value else 'unknown'


def _unescaped(field):
    # The list of mounts writes a space, tab, newline or backslash in a name as its octal code.
    import re
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), field)


def _mounted_drive(path, mounts, blocks):
    path = Path(os.path.realpath(path))
    found = None
    for line in mounts.read_text(encoding='utf-8', errors='replace').splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, point, system = (_unescaped(field) for field in fields[:3])
        point = Path(point)
        # The deepest mount holding the folder is the one it is on; a later one at the same place covers it.
        if (point == path or point in path.parents) and (found is None or len(point.parts) >= len(found[1].parts)):
            found = (device, point, system)
    if found is None:
        return 'unknown', 'unknown'
    device, _, system = found
    if system in NETWORK_FILE_SYSTEMS:
        return 'network', system
    if not device.startswith('/dev/'):
        return 'local', system
    # A partition has no removable flag of its own: its disk, one folder up, does. A USB disk often says it is
    # fixed, so its place on the USB bus says it instead.
    block = (blocks / Path(os.path.realpath(device)).name).resolve()
    for folder in (block, block.parent):
        flag = folder / 'removable'
        if flag.is_file():
            removable = flag.read_text().strip() == '1' or '/usb' in block.as_posix()
            return ('removable' if removable else 'local'), system
    return 'local', system
