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


def beside(library):
    """Where a library's shared settings can be: in the library folder itself.

    That is the folder that holds the library, for the first site's library from before sites had folders
    of their own, or the one above it when the library is in a site's folder. Nowhere else is the app's.
    """
    from .sites import SITES
    from .state_paths import private_path
    library = safe_root(library)
    places = [library.parent]
    if library.parent.name in {site.name for site in SITES}:
        places.append(library.parent.parent)
    return tuple(private_path(place, 'shared') for place in places)


def _linked(library):
    """The link, while the folder it names is beside this library.

    The link is a file inside the library, and a library can come from anywhere. A folder named elsewhere
    is never read, written or cleared on its word: until the app links the library again where it is now,
    the library counts as not linked and keeps its own settings.
    """
    value = _read(library)
    return value if value is not None and Path(value['folder']) in beside(library) else None


def _write(library, value):
    path = safe_root(library) / LINK
    directory(path.parent).chmod(0o700)
    atomic_json(path, value)
    path.chmod(0o600)


def folder(library):
    """The shared settings folder this library is linked to, or None."""
    value = _linked(library)
    return safe_root(value['folder']) if value else None


def cloud_folder(library):
    """Where this library's cloud connections are kept, when they are the shared ones; None when it keeps its own."""
    value = _linked(library)
    return safe_root(value['folder']) / 'cloud' if value and cloud_mode(library) == 'shared' else None


def cloud_mode(library):
    """'shared', 'separate', or None for a library that is not linked."""
    value = _linked(library)
    return (_global_mode(safe_root(value['folder'])) or value['cloud']) if value else None


def _global_mode(store):
    path = safe_root(store / 'cloud-sharing.json')
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text('utf-8'))
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'mode'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['mode'] not in MODES):
            raise ValueError
        return value['mode']
    except (OSError, ValueError):
        raise ArchiveError('Cloud sharing settings are unreadable; saved episodes are kept.') from None


def _siblings(library, store):
    from .sites import SITES
    base = store.parent
    candidates = {safe_root(library), base / 'Library'} | {base / site.name / 'Library' for site in SITES}
    return sorted((p for p in candidates if p.is_dir() and (value := _linked(p))
                   and Path(value['folder']) == store), key=str)


def sharing_mode(library):
    """One checkbox for the working library, including older mixed provider choices."""
    store = folder(library)
    if store is None:
        return None
    explicit = _global_mode(store)
    if explicit:
        return explicit
    modes = {_linked(p)['cloud'] for p in _siblings(library, store)}
    return next(iter(modes)) if len(modes) == 1 else 'mixed'


def choose_cloud_all(library, mode):
    """Switch every linked provider and future providers with one atomic preference.

    Shared and individual connections remain untouched, so turning sharing off
    restores individual setups. Refuse a change while any provider is writing.
    """
    from contextlib import ExitStack
    if mode not in MODES:
        raise ArchiveError('Choose shared or separate cloud storage.')
    store = folder(library)
    if store is None:
        raise ArchiveError('This library has no shared settings.')
    with ExitStack() as stack:
        for sibling in _siblings(library, store):
            stack.enter_context(LibraryLock(sibling))
        stack.enter_context(LibraryLock(store))
        _global_mode(store)
        path = store / 'cloud-sharing.json'
        atomic_json(path, {'schema_version': 1, 'mode': mode})
        path.chmod(0o600)


def choose_cloud(library, mode):
    """Use the shared cloud storage, or this library's own. Nothing set up either way is deleted."""
    if mode not in MODES:
        raise ArchiveError('Choose shared or separate cloud storage.')
    with LibraryLock(library):
        value = _linked(library)
        if value is None:
            raise ArchiveError('This library has no shared settings.')
        _write(library, value | {'cloud': mode})


def link(library, shared):
    """Link a site's library to the library folder's shared settings, once.

    A library linked for the first time brings what it already set up: its cloud connections become the
    shared ones if there are none yet, and any connection rule set on this device. If the shared settings already have
    a cloud connection and this library has its own, it keeps its own, so neither is lost.
    """
    library, shared = safe_root(library), safe_root(shared)
    if shared not in beside(library):
        raise ArchiveError('Shared settings are kept in the library folder, beside the library that uses them.')
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
        mode = 'separate' if _global_mode(shared) == 'separate' else cloud.join(library, shared / 'cloud')
        network.join(library, shared)
        _write(library, {'schema_version': 1, 'folder': str(shared), 'cloud': mode})
