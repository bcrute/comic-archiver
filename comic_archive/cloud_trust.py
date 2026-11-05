"""The cloud connections this computer uses, noted outside every library folder.

A library folder can come from another computer, or from someone else, with a connection to a bucket that is not
the person's own in it. New episodes go to a connection only once it was set up here, or the person said to use it.
Each is noted by a digest of its name and connection file, never the keys themselves, so a library folder moved or
copied on this computer keeps its connection.
"""
import hashlib
import json
import re

from . import osdep
from .models import ArchiveError

RECORD = 'cloud-connections.json'
DIGEST = re.compile(r'[0-9a-f]{64}')
# Larger than any connection file the app writes.
LIMIT = 64 * 1024


def fingerprint(target):
    """The digest that names one connection; None when its file cannot be read."""
    from .cloud import _config
    try:
        with _config(target['config']).open('rb') as stream:
            data = stream.read(LIMIT + 1)
        name = target['remote'].encode()
    except (ArchiveError, OSError, KeyError, TypeError, ValueError, AttributeError):
        return None
    return hashlib.sha256(name + b'\0' + data).hexdigest() if len(data) <= LIMIT else None


def _read(folder):
    """The digests noted, or None before any were noted on this computer."""
    try:
        value = json.loads((folder / RECORD).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return set()  # Nothing known: the person is asked again.
    noted = value.get('connections') if isinstance(value, dict) else None
    return {digest for digest in noted if isinstance(digest, str) and DIGEST.fullmatch(digest)} if isinstance(noted, list) else set()


def _write(folder, digests):
    # Not locked: two noted at the same moment may keep one, and the person is asked about the other again.
    try:
        (folder / (RECORD + '.new')).write_text(json.dumps({'schema_version': 1, 'connections': sorted(digests)}),
                                                encoding='utf-8')
        osdep.replace_file(folder / (RECORD + '.new'), folder / RECORD)
    except OSError:
        raise ArchiveError('Comic Archiver could not note this cloud connection in its settings folder.') from None


def _opened():
    """The connections in every library folder this computer opened before connections were noted: each was set
    up by the person, or used by them, before this version."""
    from . import app_runtime, sites
    from .cloud import settings
    roots = [app_runtime.saved_root(), *app_runtime.opened_libraries()]
    try:
        roots.insert(0, app_runtime.default_root())
    except ArchiveError:
        pass
    found = set()
    for root in dict.fromkeys(root for root in roots if root is not None):
        for site in sites.SITES:
            try:
                value = settings(sites.folder(root, site) / 'Library')
            except (ArchiveError, OSError):
                continue
            found.update(fingerprint(target) for target in (value or {'targets': {}})['targets'].values())
    return found - {None}


def known():
    """Every connection noted on this computer. The first time, those already in use are noted."""
    from .app_runtime import state_dir
    folder = state_dir()
    found = _read(folder)
    if found is None:
        found = _opened()
        _write(folder, found)
    return found


def trusted(target):
    digest = fingerprint(target)
    try:
        return digest is not None and digest in known()
    except ArchiveError:
        return False


def add(target):
    """Note a connection set up here, or that the person said to use."""
    from .app_runtime import state_dir
    digest = fingerprint(target)
    if digest is None:
        return
    found = known()
    if digest not in found:
        _write(state_dir(), found | {digest})
