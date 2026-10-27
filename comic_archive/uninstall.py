"""What uninstalling takes away: every sign-in and cloud key Comic Archiver saved on this computer.

Saved episodes stay where they are. Each site's sign-in goes from every library folder the app has opened,
with the Backblaze B2 keys kept beside them, and so do the app's own folders: its settings and log, the
backup helper it downloaded, and Qt's cache. The Windows uninstaller runs this before it removes the app;
on Linux the menu entry's Uninstall does. The window must be closed first (desktop_app checks).
"""
import json
import os
import re
from pathlib import Path
import shutil
import sys
import time

from . import app_runtime, background, cloud, shared, sites
from .app_runtime import LOG
from .models import ArchiveError
from .osdep import WINDOWS, replace_file
from .session import forget_session
from .state_paths import private_path
from .storage import safe_root

# The file that starts a copy of the app, on Linux and on Windows; each copy also has a VERSION file.
LAUNCHERS = ('comic-archiver', 'Comic Archiver.cmd')
# Set by the Linux launcher: the folder of the copy the menu entry started before this one.
PREVIOUS = 'COMIC_ARCHIVER_PREVIOUS'
# How long a downloader left running is given to stop when asked, before it is ended.
STOP_SECONDS = 10


def libraries():
    """Every library folder the app may have kept a sign-in in: the standard one, the chosen one, and each one
    it noted opening."""
    found = [app_runtime.default_root(), app_runtime.saved_root(), *app_runtime.opened_libraries()]
    return list(dict.fromkeys(root for root in found if root is not None))


def idle(session, seconds=0):
    """Wait up to the given time until no downloader holds the library. The runtime lock decides, not a status file."""
    deadline = time.monotonic() + seconds
    while background.status(session).get('running') is True:
        if time.monotonic() >= deadline:
            return False
        time.sleep(.1)
    return True


def stop_downloader(session):
    """Ask a downloader still running to stop, and end it if it does not. True once none runs."""
    try:
        if idle(session):
            return True
        try:
            background.stop(session)
            if idle(session, STOP_SECONDS):
                return True
        except ArchiveError as error:
            LOG.warning('uninstall could not ask a downloader to stop: %s', app_runtime.describe(error))
        app_runtime.end_downloader(session)
        return idle(session, app_runtime.END_WAIT_SECONDS)
    except (ArchiveError, OSError) as error:
        LOG.warning('uninstall could not stop a downloader: %s', app_runtime.describe(error))
        return False


def forget_connections(folder):
    """Remove the cloud connection files in one folder, with the keys they hold, and the record of them."""
    try:
        folder = safe_root(folder)
    except ArchiveError:
        return  # Never followed: the app keeps no connection behind a symlink.
    if not folder.is_dir():
        return
    for path in folder.glob('*.conf'):
        path.unlink(missing_ok=True)  # A link goes, never what it points to.
    record = folder / 'targets.json'
    if record.is_file():
        try:
            value = json.loads(record.read_text(encoding='utf-8'))
            cloud._write(record, value | {'targets': {}})
        except (OSError, ValueError, TypeError):
            record.unlink(missing_ok=True)


def forget(root):
    """Forget the sign-ins and cloud keys kept in one library folder. False when a downloader would not stop."""
    stopped = True
    for site in sites.SITES:
        try:
            place = safe_root(sites.folder(root, site))
            session = private_path(place, 'session')
        except ArchiveError:
            continue  # A symlinked folder is never followed; the app never saved through one.
        if session.is_dir():
            stopped = stop_downloader(session) and stopped
            forget_session(session)
        library = place / 'Library'
        forget_connections(library / '.archive-state/cloud')
        try:
            linked = shared.cloud_folder(library)
        except ArchiveError:
            linked = None
        if linked is not None:
            forget_connections(linked)
    try:
        forget_connections(private_path(root, 'shared') / 'cloud')
    except ArchiveError:
        pass
    return stopped


def app_folders():
    """The folders only Comic Archiver writes in, outside the library: settings and log, backup helper, Qt cache."""
    home = Path.home()
    if WINDOWS:
        base = Path(os.environ.get('LOCALAPPDATA') or '')
        local = base if base.is_absolute() else home / 'AppData/Local'
        return [local / 'comic-archiver', local / app_runtime.FOLDER_NAME]
    def xdg(name, default):
        base = Path(os.environ.get(name) or '')
        return base if base.is_absolute() else home / default
    return [xdg('XDG_STATE_HOME', '.local/state') / 'comic-archiver', home / '.local/share/comic-archive',
            xdg('XDG_CACHE_HOME', '.cache') / app_runtime.FOLDER_NAME,
            xdg('XDG_DATA_HOME', '.local/share') / app_runtime.FOLDER_NAME]


def forget_all():
    """Forget everything kept for signing in, in every library folder. False if something stayed."""
    complete = True
    for root in libraries():
        try:
            complete = forget(root) and complete
        except (ArchiveError, OSError) as error:
            LOG.warning('uninstall could not clear a library folder: %s', app_runtime.describe(error))
            complete = False
    return complete


def remove_app_folders():
    """Remove the app's own folders; run last, as one of them holds the lock that keeps the window from opening.
    False if one stayed."""
    complete = True
    for folder in app_folders():
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            complete = complete and not folder.exists()
    return complete


def copy_version(folder):
    """The version a copy of the app says it is, as numbers to compare, or None."""
    try:
        text = (folder / 'VERSION').read_text(encoding='ascii').strip()
    except (OSError, ValueError):
        return None
    return tuple(int(part) for part in text.split('.')) if re.fullmatch(r'[0-9]{1,6}(\.[0-9]{1,6}){0,3}', text) else None


def this_copy():
    """The folder of the copy of the app running now, or None when it runs from its source."""
    folder = Path(sys.prefix).parent
    if copy_version(folder) is None or not any((folder / name).is_file() for name in LAUNCHERS):
        return None
    return folder


def app_folder():
    """The unpacked Linux app this runs from, or None."""
    return None if WINDOWS else this_copy()


def unpacked(folder):
    """True for a copy of the app that was unpacked, not installed, so deleting its folder removes it."""
    try:
        return (not folder.is_symlink() and copy_version(folder) is not None and (folder / 'python').is_dir()
                and any((folder / name).is_file() for name in LAUNCHERS) and not (folder / 'unins000.exe').exists())
    except (OSError, ValueError):
        return False


def older_copies():
    """Copies of the app started on this computer before this one, of an older version, that may be deleted.

    Each start notes the copy that ran. On Linux the launcher also passes the copy the menu entry started until
    now (COMIC_ARCHIVER_PREVIOUS), which finds copies from before copies were noted. A newer copy is never
    offered, nor one of the same version, nor an installed one (its uninstaller would forget the sign-ins), nor
    one with a library inside.
    """
    here = this_copy()
    if here is None:
        return []
    mine, record = copy_version(here), app_runtime.state_dir() / 'app'
    candidates = [os.environ.get(PREVIOUS) or '']
    try:
        candidates.append(record.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        pass
    found, newest = [], True
    for text in dict.fromkeys(candidates):
        folder = Path(text)
        if not text or '\0' in text or not folder.is_absolute():
            continue
        theirs = copy_version(folder)
        if theirs is not None and theirs > mine:
            newest = False
            continue
        if theirs == mine:
            continue  # Another copy of this version may be the one kept on purpose.
        inside = [here, *libraries()]
        if unpacked(folder) and not any(path == folder or folder in path.parents or path in folder.parents
                                        for path in inside):
            found.append(folder)
    if newest:
        (record.parent / 'app.new').write_text(f'{here}\n', encoding='utf-8')
        replace_file(record.parent / 'app.new', record)
    return found


def running_copy(pid, proc=Path('/proc')):
    """The unpacked copy of the app a running process started from, or None. Linux only: read from /proc."""
    try:
        program = (proc / str(int(pid)) / 'exe').resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None
    folder = program.parents[2]  # <copy>/python/bin/python3.x
    return folder if unpacked(folder) else None


def remove_copy(folder):
    """Delete an older copy found by older_copies. False if some of it stayed."""
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def remove_menu_entry(folder):
    """Remove the applications-menu entry the Linux launcher made, when it starts this copy of the app."""
    base = Path(os.environ.get('XDG_DATA_HOME') or '')
    entry = (base if base.is_absolute() else Path.home() / '.local/share') / 'applications/comic-archiver.desktop'
    try:
        if f'Exec="{folder}/comic-archiver"' in entry.read_text(encoding='utf-8').splitlines():
            entry.unlink()
    except (OSError, ValueError):
        pass
