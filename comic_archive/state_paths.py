"""Where the app keeps its private state beside a library."""
import os
import stat
from pathlib import Path

from . import osdep, sites, windows_privacy
from .models import ArchiveError
from .storage import safe_root


SESSION_NAME = '.comic-session'
# The folders the app makes in each site's own folder. None of them is ever a junction: one that is was put there
# by someone else, and would have the app read, write or delete somewhere else.
INSIDE = (SESSION_NAME, SESSION_NAME + '/worker', SESSION_NAME + '/worker/requests', SESSION_NAME + '/worker/responses',
          '.comic-web', 'Library', 'Library/.archive-state', 'Library/.archive-state/cloud')


def state_location(*, windows=osdep.WINDOWS):
    """Locate per-user settings outside the library without creating a directory."""
    name, default = ('LOCALAPPDATA', 'AppData/Local') if windows else ('XDG_STATE_HOME', '.local/state')
    base = Path(os.environ.get(name) or '')
    return (base if base.is_absolute() else Path.home() / default) / 'comic-archiver'


def queue_directory_identity(path):
    return queue_entry_identity(path.lstat())


def queue_entry_identity(entry):
    if (not stat.S_ISDIR(entry.st_mode)
            or getattr(entry, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ArchiveError('The worker needs regular directories for its queue.')
    if os.name == 'posix' and (entry.st_uid != os.geteuid() or entry.st_mode & 0o022):
        raise ArchiveError('The worker needs queue folders writable only by this user.')
    return entry.st_dev, entry.st_ino


def private_path(root, kind):
    if kind not in {'session', 'web', 'shared'}:
        raise ArchiveError('Unknown private app storage.')
    return safe_root(Path(root) / ('.comic-' + kind))


def private_folder(root, kind):
    """The private folder, made if it is not there yet, and hidden from the file manager as on Linux."""
    path = private_path(root, kind)
    path.mkdir(mode=0o700, exist_ok=True)
    windows_privacy.folder(path)
    osdep.hide(path)
    return path


def session_path(root):
    return private_path(root, 'session')


def leads_elsewhere(root):
    """The first folder the app keeps in this library folder that is a junction, or None. Only Windows has them.

    A junction is a folder that leads somewhere else, as a symlink does; the app's checks for symlinks do not
    see one. Only the folders the app made are looked at: the library folder itself, and the folders around it,
    are the person's own to arrange, and may well be junctions or drives mounted in a folder.
    """
    root = Path(root)
    found = [root / '.comic-shared', root / '.comic-shared/cloud']
    # Retain checks on the original Tapas directories until their move completes.
    found += [root / name for name in INSIDE] + [root / sites.DEFAULT.name]
    for site in sites.SITES:
        place = sites.folder(root, site)
        found += ([] if place == root else [place]) + [place / name for name in INSIDE]
    return next((folder for folder in found if osdep.junction(folder)), None)
