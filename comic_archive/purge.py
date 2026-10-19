"""Remove only media with verified replacements; retain indexes and partials."""
from pathlib import Path
import re
import tarfile
import zipfile
import zstandard

from .archive import digest, sidecar_path, verify_episode
from .models import ArchiveError
from .osdep import anchored, disk_bytes
from .publication import import_metadata, StrictZstdReader, comic_pages

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


