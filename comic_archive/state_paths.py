"""Choose neutral private-state paths without moving an existing installation."""
from pathlib import Path

from .models import ArchiveError
from .storage import safe_root


SESSION_NAMES = {'.comic-session', '.tapas-session'}


def compatible_path(current, legacy):
    current, legacy = safe_root(current), safe_root(legacy)
    if current.exists() and legacy.exists():
        raise ArchiveError('Both current and legacy app storage exist; choose one before continuing.')
    return legacy if legacy.exists() else current


def private_path(root, kind):
    if kind not in {'session', 'web', 'hosted', 'automation'}:
        raise ArchiveError('Unknown private app storage.')
    root = Path(root)
    return compatible_path(root / ('.comic-' + kind), root / ('.tapas-' + kind))


def session_path(root):
    return private_path(root, 'session')
