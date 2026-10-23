"""Read-only catalog of Android document facts, using the shared saved-file view.

The native collector owns URI grants, listing, metadata reads and opening. Only
bounded public file facts enter here: no document URIs, sessions, source URLs,
cloud configuration or writable filesystem adapter. No original is imported.
"""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re

from .models import ArchiveError
from .saved_library import date, episode_order, page_snapshot, text

MAX_FILES = 50000
MAX_FOLDERS = 2000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
METADATA_FIELDS = {'title', 'format', 'episode_number', 'imported_at', 'cover_file'}


def _name(value):
    return (isinstance(value, str) and 0 < len(value) <= 500 and value not in {'.', '..'}
            and not value.startswith('.') and not any(ord(c) < 32 or c in '/\\' for c in value))


def _file(value, identities):
    if (not isinstance(value, dict) or not {'key', 'name', 'bytes', 'modified'} <= set(value)
            or set(value) - {'key', 'name', 'bytes', 'modified', 'metadata'}):
        raise ValueError
    key = value.get('key')
    if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key) or key in identities:
        raise ValueError
    size = value.get('bytes')
    if not _name(value.get('name')) or (size is not None and (type(size) is not int or not 0 <= size < 2**63)):
        raise ValueError
    modified = value.get('modified')
    if modified is not None and (type(modified) is not int or not 0 < modified <= 32503680000):
        raise ValueError
    metadata = value.get('metadata')
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - METADATA_FIELDS:
            raise ValueError
        if any(isinstance(v, (list, dict)) for v in metadata.values()):
            raise ValueError
    identities.add(key)
    return value


def snapshot(manifest):
    """Project portable sidecars/covers and plain exported CBZ/EPUB folders.

    Plain files have filename titles and provider modification times; no saved
    time or successful archive-integrity check is invented. Legacy compressed
    files require their portable metadata and never receive a reader action.
    """
    try:
        if not isinstance(manifest, dict) or set(manifest) != {'v', 'folders'} or type(manifest['v']) is not int or manifest['v'] != 1:
            raise ValueError
        folders = manifest['folders']
        if not isinstance(folders, list) or len(folders) > MAX_FOLDERS:
            raise ValueError
        rows, identities = [], set()
        episodes = count = 0

        def add(fact, kind, series, title='', saved=None, order=-1):
            rows.append({'id': fact['key'], 'filename': fact['name'], 'kind': kind,
                         'series': series, 'title': title, 'saved_at': date(saved, fact.get('modified')),
                         'date_source': 'saved' if date(saved) else 'file_modified', 'size': fact['bytes'],
                         'location': 'device', 'order': order})

        for folder in folders:
            if not isinstance(folder, dict) or set(folder) != {'name', 'files'} or not _name(folder['name']):
                raise ValueError
            if not isinstance(folder['files'], list):
                raise ValueError
            count += len(folder['files'])
            if count > MAX_FILES:
                raise ValueError
            facts = [_file(value, identities) for value in folder['files']]
            files = {fact['name']: fact for fact in facts}
            if len(files) != len(facts):
                raise ValueError
            series_fact = files.get('series.json')
            series_meta = (series_fact.get('metadata') if series_fact else None) or {}
            series = text(series_meta.get('title')) or folder['name']
            offered = []
            for fact in facts:
                name = fact['name']
                extension = next((ext for ext in ('.cbz', '.epub', '.tar.zst') if name.endswith(ext)), None)
                if extension is None or fact['bytes'] == 0:
                    continue
                sidecar = files.get(name[:-len(extension)] + '.json')
                meta = (sidecar.get('metadata') if sidecar else None) or {}
                declared = {'epub': '.epub', 'tar.zst': '.tar.zst'}.get(meta.get('format'), '.cbz')
                if extension == '.tar.zst' and (not meta or declared != extension):
                    continue
                if meta and declared != extension:
                    continue
                offered.append((fact, sidecar, meta, extension))
            if not offered:
                continue
            if series_fact and series_meta:
                add(series_fact, 'series_metadata', series)
            cover = series_meta.get('cover_file')
            if isinstance(cover, str) and cover in {'cover.jpg', 'cover.png', 'cover.gif', 'cover.webp'} and cover in files:
                add(files[cover], 'cover', series)
            for fact, sidecar, meta, extension in offered:
                title = text(meta.get('title')) or fact['name'][:-len(extension)]
                order = episode_order(meta, PurePosixPath(fact['name']))
                add(fact, extension[1:], series, title, meta.get('imported_at'), order)
                episodes += 1
                if sidecar and meta:
                    add(sidecar, 'episode_metadata', series, title, meta.get('imported_at'), order)
        return {'rows': rows, 'locations': [{'id': 'device', 'label': 'Opened folder', 'connected': True,
                    'files': len(rows), 'episodes': episodes,
                    'bytes': None if any(row['size'] is None for row in rows) else sum(row['size'] for row in rows)}],
                'episode_count': episodes, 'file_count': len(rows), 'updated_at': datetime.now(timezone.utc).isoformat()}
    except (ValueError, TypeError, OverflowError, OSError):
        raise ArchiveError('The folder catalog could not be read. Choose the folder again or refresh it.') from None


def page(manifest, **options):
    return page_snapshot(snapshot(manifest), **options)
