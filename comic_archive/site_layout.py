"""Uniform provider folders, with resumable relocation of the original Tapas layout."""
import json
import logging
import os
from pathlib import Path

from .models import ArchiveError
from .osdep import sync_directory

MARKER = 'site-layout.json'
PENDING = 'site-layout-migration.json'
ENTRIES = ('Library', '.comic-session', '.comic-web')
SCHEMA = {'schema_version': 1, 'provider_directories': True}
LOG = logging.getLogger('comic_archive.desktop')


class LayoutError(ArchiveError):
    pass


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > 16384:
        raise ValueError('Invalid layout record')
    return json.loads(path.read_text(encoding='utf-8'))


def known(root):
    try:
        return _read(Path(root) / '.comic-shared' / MARKER) == SCHEMA
    except (OSError, ValueError, RecursionError):
        return False


def legacy(root):
    root = Path(root)
    return (root / 'Library/.archive-state').is_dir() or (root / '.comic-session/state.json').is_file()


def lock_state(lock_root):
    """Block new archive writers and worker launches once an offline move is journaled."""
    root = Path(lock_root)
    if root.name == 'Library':
        place = root.parent
    elif root.name == '.runtime' and root.parent.name == 'worker' and root.parent.parent.name == '.comic-session':
        place = root.parent.parent.parent
    else:
        return None
    if ((place / '.comic-shared' / PENDING).exists()
            or place.name == 'Tapas' and (place.parent / '.comic-shared' / PENDING).exists()):
        return 'moving'
    return 'moved' if known(place) else None


def require_location(root):
    from .errors import LibraryBusy, LibraryMoved
    state = lock_state(root)
    if state == 'moved':
        raise LibraryMoved('This library moved into its provider folder. Reopen it from the selected library location.')
    if state == 'moving':
        raise LibraryBusy('The provider folders are being arranged; retry after the move finishes.')


def _identity(path):
    from .osdep import junction
    if path.is_symlink() or junction(path) or not path.is_dir():
        raise LayoutError('A provider folder must be a normal directory before it can be arranged.')
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _same(path, identity):
    """Whether path is the folder a journal recorded. Only the folder's own number is compared: the number of
    the drive it is on changes when the computer restarts or the drive is plugged in again."""
    return _identity(path)[1] == identity[1]


def _point_cloud(root, locked=False):
    """A library that keeps its own cloud connection names its private file by its full path; after the move
    that is the file's place in the Tapas folder. Without this, uploads stop until cloud storage is set up again."""
    from .cloud_paths import moved
    from .errors import LibraryBusy
    from .locking import LibraryLock
    library = root / 'Tapas/Library'
    try:
        if locked:
            moved(library, root / 'Library')
        elif (library / '.archive-state/cloud/targets.json').is_file():
            # A move made by an earlier version left the old path. A running download has the library; next time.
            with LibraryLock(library):
                moved(library, root / 'Library')
    except LibraryBusy:
        pass
    except (ArchiveError, OSError) as error:
        if locked and isinstance(error, OSError):
            raise  # The move stays pending and is taken up again.
        LOG.warning('library layout: a cloud connection still names its old place (%s)', type(error).__name__)


def prepare(root):
    """Rename only known app directories; keep shared settings and other providers at the selected root.

    Writer/worker locks are held until the journal blocks new admissions. They
    are then released before renaming, so the move also works on Windows.
    """
    from .archive import atomic_json
    from .background import status
    from .errors import LibraryBusy
    from .locking import LibraryLock
    from .state_paths import leads_elsewhere, private_folder
    from .storage import safe_root
    root = safe_root(root)
    if leads_elsewhere(root) is not None:
        raise LayoutError('Provider folders must not be junctions before they can be arranged.')
    shared = private_folder(root, 'shared')
    pending, marker = shared / PENDING, shared / MARKER
    with LibraryLock(shared / 'site-layout'):
        try:
            if marker.exists() and not known(root):
                raise LayoutError('The library folder layout record cannot be read. Restore it before continuing.')
            if not legacy(root) and not pending.exists():
                if not marker.exists():
                    atomic_json(marker, SCHEMA); marker.chmod(0o600)
                    sync_directory(shared)
                    sync_directory(root)
                else:
                    _point_cloud(root)
                return
            target = root / 'Tapas'
            if pending.exists():
                record = _read(pending)
                if (not isinstance(record, dict) or set(record) != {'schema_version', 'target', 'entries'}
                        or record['schema_version'] != 1 or not isinstance(record['entries'], dict)
                        or not {'Library', '.comic-session'} <= set(record['entries']) <= set(ENTRIES)
                        or any(not isinstance(v, list) or len(v) != 2 or any(type(n) is not int for n in v)
                               for v in [record['target'], *record['entries'].values()])
                        or not _same(target, record['target'])):
                    raise LayoutError('The pending library folder move cannot be verified. Saved files are retained.')
                sync_directory(shared)  # Reestablish durability after an earlier flush failed.
            else:
                if status(root / '.comic-session').get('running') is not False:
                    raise LibraryBusy('Pause or close the downloader before arranging the provider folders.')
                # Both locks must be obtained before publishing the admission barrier.
                with LibraryLock(root / '.comic-session/worker/.runtime'), LibraryLock(root / 'Library'):
                    if target.exists() and (target.is_symlink() or not target.is_dir() or any(target.iterdir())):
                        raise LayoutError('Both old and new Tapas folders exist. They cannot be combined automatically.')
                    target.mkdir(mode=0o700, exist_ok=True)
                    record = {'schema_version': 1, 'target': _identity(target),
                              'entries': {name: _identity(root / name) for name in ENTRIES if (root / name).exists()}}
                    if any(identity[0] != record['target'][0] for identity in record['entries'].values()):
                        raise LayoutError('The provider folders must be on the same filesystem to be moved.')
                    sync_directory(root)
                    atomic_json(pending, record); pending.chmod(0o600)
                    sync_directory(shared)  # The journal must be durable before the first rename.
            for name, identity in record['entries'].items():
                source, destination = root / name, target / name
                if source.exists():
                    if destination.exists() or destination.is_symlink() or not _same(source, identity):
                        raise LayoutError('A provider folder changed during its move. Close the app and try again.')
                    os.rename(source, destination)
                if not _same(destination, identity):
                    raise LayoutError('A moved provider folder cannot be verified. Saved files are retained.')
                sync_directory(target)
                sync_directory(root)
            _point_cloud(root, locked=True)  # The journal still keeps every writer out.
            atomic_json(marker, SCHEMA); marker.chmod(0o600)
            sync_directory(shared)  # Commit the completed layout before retiring the journal.
            pending.unlink()
            sync_directory(shared)
            LOG.info('library layout: Tapas directories relocated; provider folders are siblings')
        except (OSError, ValueError, RecursionError) as error:
            LOG.warning('library layout: relocation remains pending (%s)', type(error).__name__)
            raise LayoutError('The provider folders could not be arranged. Close other apps using the library and try again.') from None
