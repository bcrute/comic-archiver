"""Where the app keeps its private state beside a library."""
from pathlib import Path

from . import osdep
from .models import ArchiveError
from .storage import safe_root


SESSION_NAME = '.comic-session'


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
