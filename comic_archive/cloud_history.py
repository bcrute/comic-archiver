"""Verified upload history, independent of the connection chosen for future uploads.

The outbox is mutable retry state. A copy retained here continues to describe
an earlier save when that outbox is reused for another connection.
"""
from datetime import datetime
import hashlib
import re

from .archive import sidecar_path
from .models import ArchiveError
from .publication import valid_export_id, validate_public_metadata
from .storage import safe_root


def retain(root, provider, record):
    """Retain verified evidence before replacing an outbox entry or removing local bytes."""
    from .cloud import PROVIDERS, _json, _write
    if (provider not in PROVIDERS or record.get('state') != 'verified'
            or not all(valid_export_id(record.get(k)) for k in ('library_id', 'target_id', 'episode_id'))
            or not isinstance(record.get('version'), str) or not re.fullmatch(r'[a-f0-9]{64}', record['version'])):
        raise ArchiveError('Cloud history needs a verified upload identity.')
    folder = safe_root(root) / '.archive-state/cloud/history' / provider / record['episode_id']
    path = folder / (record['target_id'] + '-' + record['version'] + '.json')
    if path.exists():
        previous = _json(path)
        # An eviction mark is monotonic; a later upload cannot undo an earlier offload.
        if not record.get('eviction_requested') or previous.get('eviction_requested') is True:
            return
        record = previous | {'eviction_requested': True}
    _write(path, record)


def checked(root, archive, public, library_id, record):
    """Match a historical receipt to the retained public metadata, never to an active target."""
    if (record.get('state') != 'verified' or record.get('library_id') != library_id
            or record.get('archive') != archive.relative_to(root).as_posix()
            or record.get('episode_id') != public['export_id'] or not isinstance(record.get('files'), list)
            or len(record['files']) < 3 or not valid_export_id(record.get('series_id'))
            or not valid_export_id(record.get('target_id'))):
        return None
    try:
        verified_at = datetime.fromisoformat(record['verified_at'])
        if verified_at.tzinfo is None:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    items = record['files']
    expected_names = ['episode.' + public['format'], 'episode.json', 'series.json']
    if (any(not isinstance(item, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))) for item in items)
            or [p.get('name') for p in items[:3]] != expected_names):
        return None
    if (items[0].get('path') != record['archive']
            or items[1].get('path') != sidecar_path(archive).relative_to(root).as_posix()):
        return None
    with sidecar_path(archive).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != items[1]['sha256']:
            return None
    if hashlib.sha256((items[0]['sha256'] + items[1]['sha256']).encode()).hexdigest() != record.get('version'):
        return None
    return record


def receipts(root, archive, provider, library_id, *, outbox_only=False):
    """Verified copies matching this episode, including disconnected targets, without network access."""
    from .cloud import PROVIDERS, _json
    root, archive = safe_root(root), safe_root(archive)
    if provider not in PROVIDERS or len(archive.relative_to(root).parts) != 2 or archive.parent.name.startswith('.'):
        return []
    public = _json(sidecar_path(archive), 16 * 1024**2)
    validate_public_metadata(public)
    paths = [root / '.archive-state/cloud/outbox' / provider / (public['export_id'] + '.json')]
    if not outbox_only:
        folder = safe_root(root / '.archive-state/cloud/history' / provider / public['export_id'])
        paths.extend(sorted(folder.glob('*.json')))
    found = {}
    for path in paths:
        if not path.is_file():
            continue
        record = checked(root, archive, public, library_id, _json(path))
        if record:
            key = record['target_id'], record['version']
            previous = found.get(key)
            if previous is None or record.get('eviction_requested') is True:
                found[key] = record
    return list(found.values())
