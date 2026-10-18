"""Settings every site in one library folder shares: cloud storage and the connection rule.

Each site keeps its own library inside the library folder (sites.folder). The app links each one to the
folder's shared settings when it opens it. A library that is not linked, as one used from the command line,
keeps all its settings itself. A linked library may still keep its own cloud storage, if the person asks.
"""
import json
from pathlib import Path

from .archive import atomic_json
from .archive_paths import directory
from .locking import LibraryLock
from .models import ArchiveError
from .storage import safe_root

LINK = '.archive-state/shared.json'
MODES = ('shared', 'separate')


def _read(library):
    path = safe_root(library) / LINK
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Shared settings must not be symlinked.')
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'folder', 'cloud'}
                or value['schema_version'] != 1 or type(value['schema_version']) is not int
                or not isinstance(value['folder'], str) or not Path(value['folder']).is_absolute()
                or value['cloud'] not in MODES):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ArchiveError('Shared settings are unreadable; saved episodes are kept.') from None


def _write(library, value):
    path = safe_root(library) / LINK
    directory(path.parent).chmod(0o700)
    atomic_json(path, value)
    path.chmod(0o600)


def folder(library):
    """The shared settings folder this library is linked to, or None."""
    value = _read(library)
    return safe_root(value['folder']) if value else None


def cloud_folder(library):
    """Where this library's cloud connections are kept, when they are the shared ones; None when it keeps its own."""
    value = _read(library)
    return safe_root(value['folder']) / 'cloud' if value and value['cloud'] == 'shared' else None


def cloud_mode(library):
    """'shared', 'separate', or None for a library that is not linked."""
    value = _read(library)
    return value['cloud'] if value else None


def choose_cloud(library, mode):
    """Use the shared cloud storage, or this library's own. Nothing set up either way is deleted."""
    if mode not in MODES:
        raise ArchiveError('Choose shared or separate cloud storage.')
    with LibraryLock(library):
        value = _read(library)
        if value is None:
            raise ArchiveError('This library has no shared settings.')
        _write(library, value | {'cloud': mode})


def link(library, shared):
    """Link a site's library to the library folder's shared settings, once.

    A library linked for the first time brings what it already set up: its cloud connections become the
    shared ones if there are none yet, and its connection rule too. If the shared settings already have
    a cloud connection and this library has its own, it keeps its own, so neither is lost.
    """
    library, shared = safe_root(library), safe_root(shared)
    value = _read(library)
    if value is not None:
        if value['folder'] != str(shared):
            # The library folder was moved or renamed by hand: the shared settings are those beside it now.
            from .cloud_paths import relocate
            with LibraryLock(library), LibraryLock(shared):
                relocate(library, value['folder'], shared)
                _write(library, value | {'folder': str(shared)})
        return
    directory(shared).chmod(0o700)
    from . import cloud, network
    from .osdep import hide
    hide(shared)
    with LibraryLock(library), LibraryLock(shared):
        mode = cloud.join(library, shared / 'cloud')
        network.join(library, shared)
        _write(library, {'schema_version': 1, 'folder': str(shared), 'cloud': mode})
