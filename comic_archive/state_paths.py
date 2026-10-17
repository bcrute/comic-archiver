"""Where the app keeps its private state beside a library."""
from pathlib import Path

from .models import ArchiveError
from .storage import safe_root


SESSION_NAME = '.comic-session'


def private_path(root, kind):
    if kind not in {'session', 'web', 'hosted', 'automation'}:
        raise ArchiveError('Unknown private app storage.')
    return safe_root(Path(root) / ('.comic-' + kind))


def session_path(root):
    return private_path(root, 'session')
