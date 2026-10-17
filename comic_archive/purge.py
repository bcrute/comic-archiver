"""Remove only media with verified replacements; retain indexes and partials."""
from pathlib import Path
import re
import tarfile
import zipfile
import zstandard

from .archive import atomic_json, digest, episode_archives, sidecar_path, verify_episode, verify_series
from .locking import wait_for_library
from .models import ArchiveError
from .osdep import anchored, disk_bytes, sync_directory
from .publication import import_metadata, StrictZstdReader, comic_pages
from .storage import safe_root

HASH = re.compile(r'[a-f0-9]{64}')


def archive_page(root, record, expected_hash):
    relative, name, size = record
    path = Path(relative)
    if anchored(path) or '..' in path.parts or len(path.parts) != 2 or path.parts[0].startswith('.'):
        return None
    path = Path(root) / path
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        return None
    try:
        meta = import_metadata(sidecar_path(path))
        item = next((p for p in meta['content_hashes'] if p['file'] == name and p['sha256'] == expected_hash and p['size'] == size), None)
        if item is None or type(size) is not int or not 0 < size <= 100*1024**2:
            return None
        if meta.get('format') == 'tar.zst':
            pages = comic_pages(meta)
            maximum = ((sum(512 + ((p['size']+511)//512)*512 for p in pages)+1024+10239)//10240)*10240
            with path.open('rb') as source:
                reader = StrictZstdReader(source, maximum)
                with tarfile.open(fileobj=reader, mode='r|', bufsize=512) as archive:
                    data = None
                    for member in archive:
                        if member.name == name and member.isfile() and member.size == size:
                            data = archive.extractfile(member).read(size+1)
                            break
        else:
            with zipfile.ZipFile(path) as archive:
                member = archive.getinfo(name)
                if member.file_size != size:
                    return None
                data = archive.read(member)
        return data if data is not None and len(data) == size and digest(data) == expected_hash else None
    except (OSError, ValueError, KeyError, TypeError, ArchiveError, tarfile.TarError, zipfile.BadZipFile, zstandard.ZstdError):
        return None


def register_archive(store, path, metadata):
    """Remember where identical original bytes can be recovered after eviction."""
    if metadata.get('format') != 'epub':
        store.db.executemany('INSERT OR REPLACE INTO archived_pages VALUES (?,?,?,?)',
            [(p['sha256'], path.relative_to(store.root).as_posix(), p['file'], p['size']) for p in metadata['content_hashes']])
    store.db.commit()


def evict_episode(store, path, metadata):
    if verify_episode(path):
        raise ArchiveError('Archive verification failed; cached originals are retained.')
    register_archive(store, path, metadata)
    hashes = {p['sha256'] for p in metadata['content_hashes']}
    raw = metadata.get('original_content_hashes', [])
    hashes.update(p['sha256'] for p in raw)
    removed = 0
    for hash_value in hashes:
        # A raw image used by another unfinished import remains available.
        if hash_value in {p['sha256'] for p in raw}:
            owners = store.db.execute('SELECT source,series_id,episode_id FROM cache_owners WHERE hash=?', (hash_value,)).fetchall()
            identity = (metadata['source'], metadata.get('cache_series_id', metadata['series_id']), metadata['source_episode_id'])
            if any(tuple(owner) != identity for owner in owners):
                continue
        blob = store.cache / hash_value
        if blob.is_file() and not blob.is_symlink() and HASH.fullmatch(blob.name):
            removed += disk_bytes(blob)
            blob.unlink()
    return removed


def _identity(meta):
    return meta.get('source'), str(meta.get('source_series_id', meta.get('series_id'))), meta.get('source_episode_id')


def _equivalent(old, new):
    if (_identity(old) != _identity(new) or old.get('content_type', 'comic') != new.get('content_type', 'comic')
            or old.get('episode_number') != new.get('episode_number') or old.get('title') != new.get('title')):
        return False
    if old.get('format') == 'epub':
        return (old.get('text_sha256') == new.get('text_sha256') and old.get('text_size') == new.get('text_size')
                and [(p['file'], p['sha256'], p['size']) for p in old['content_hashes'] if p['file'].startswith('EPUB/images/')]
                == [(p['file'], p['sha256'], p['size']) for p in new['content_hashes'] if p['file'].startswith('EPUB/images/')])
    return [(p['sha256'], p['size']) for p in old['content_hashes']] == [(p['sha256'], p['size']) for p in new['content_hashes']]


def purge_library(root, *, apply=False, include_legacy=False, wait_seconds=0, progress=None):
    root = safe_root(root)
    if not root.is_dir() or type(apply) is not bool or type(include_legacy) is not bool:
        raise ArchiveError('Purge requires an existing library and explicit cleanup choices.')
    report = {'applied': apply, 'verified_archives': 0, 'cache_files': 0, 'cache_bytes': 0,
              'legacy_files': 0, 'legacy_bytes': 0, 'retained_legacy_episodes': 0,
              'protected_partial_cache_files': 0, 'removed_bytes': 0}
    with wait_for_library(root, wait_seconds):
        replacements, hashes, records = {}, set(), []
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith('.'):
                continue
            if folder.is_symlink() or verify_series(folder / 'series.json'):
                raise ArchiveError('Series verification failed; purge stopped without deleting data.')
            for path in episode_archives(folder):
                if verify_episode(path):
                    raise ArchiveError('Archive verification failed; purge stopped without deleting data.')
                meta = import_metadata(sidecar_path(path))
                identity = _identity(meta)
                if identity in replacements:
                    raise ArchiveError('Duplicate export identity; purge stopped without deleting data.')
                replacements[identity] = meta
                report['verified_archives'] += 1
                hashes.update(p['sha256'] for p in meta['content_hashes'])
                records.append((path, meta))
                if progress and report['verified_archives'] % 100 == 0:
                    progress(dict(report))
        cache = root / '.archive-cache'
        if cache.is_symlink():
            raise ArchiveError('Cache directory must not be symlinked.')
        candidates = []
        if cache.is_dir():
            for blob in cache.iterdir():
                if HASH.fullmatch(blob.name):
                    if blob.is_symlink() or not blob.is_file():
                        raise ArchiveError('Unsafe cache entry; no data was deleted.')
                    if blob.name in hashes:
                        candidates.append(blob)
                    else:
                        report['protected_partial_cache_files'] += 1
        legacy = root / '.archive-state/legacy-exports'
        if any(p.is_symlink() for p in (legacy, *legacy.parents)):
            raise ArchiveError('Retained export directories must not be symlinked.')
        retired = []
        if include_legacy and legacy.is_dir():
            for folder in sorted(legacy.iterdir()):
                if folder.is_symlink() or not folder.is_dir():
                    raise ArchiveError('Unsafe retained export entry; no data was deleted.')
                for path in episode_archives(folder):
                    if verify_episode(path):
                        report['retained_legacy_episodes'] += 1
                        continue
                    old = import_metadata(sidecar_path(path))
                    new = replacements.get(_identity(old))
                    if new is None or not _equivalent(old, new):
                        report['retained_legacy_episodes'] += 1
                        continue
                    retired.extend((path, sidecar_path(path)))
        report['cache_files'], report['legacy_files'] = len(candidates), len(retired)
        report['cache_bytes'] = sum(disk_bytes(p) for p in candidates)
        report['legacy_bytes'] = sum(disk_bytes(p) for p in retired)
        if not apply:
            return report
        # Register verified recovery locations BEFORE deleting any blob; keep
        # the SQLite export identities, page keys and provenance intact.
        from .publication import PublicationStore
        with_store = PublicationStore(root)
        try:
            for path, meta in records:
                register_archive(with_store, path, meta)
            for path in candidates + retired:
                if path.is_symlink() or not path.is_file():
                    raise ArchiveError('Cleanup entry changed; remaining data is retained.')
                size = disk_bytes(path)
                path.unlink()
                report['removed_bytes'] += size
        finally:
            with_store.close()
        out = root / '.archive-state/purge.json'
        atomic_json(out, report)
        out.chmod(0o600)
        for folder in (cache, legacy):
            if folder.is_dir():
                sync_directory(folder)
        return report
