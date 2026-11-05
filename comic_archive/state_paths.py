"""Where the app keeps its private state beside a library."""
from pathlib import Path

from . import osdep, sites
from .models import ArchiveError
from .storage import safe_root


SESSION_NAME = '.comic-session'
# The folders the app makes in each site's own folder. None of them is ever a junction: one that is was put there
# by someone else, and would have the app read, write or delete somewhere else.
INSIDE = (SESSION_NAME, SESSION_NAME + '/worker', SESSION_NAME + '/worker/requests', SESSION_NAME + '/worker/responses',
          '.comic-web', 'Library', 'Library/.archive-state', 'Library/.archive-state/cloud')


def private_path(root, kind):
    if kind not in {'session', 'web', 'shared'}:
        raise ArchiveError('Unknown private app storage.')
    return safe_root(Path(root) / ('.comic-' + kind))


def private_folder(root, kind):
    """The private folder, made if it is not there yet, and hidden from the file manager as on Linux."""
    path = private_path(root, kind)
    path.mkdir(mode=0o700, exist_ok=True)
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
    for site in sites.SITES:
        place = sites.folder(root, site)
        found += ([] if place == root else [place]) + [place / name for name in INSIDE]
    return next((folder for folder in found if osdep.junction(folder)), None)
