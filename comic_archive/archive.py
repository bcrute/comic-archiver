"""Content-addressed resume cache and open CBZ/EPUB/JSON publication."""
import hashlib
import json
import os
import re
import sqlite3
import shutil
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .models import ArchiveError, canonical_url, identifier
from .transport import ExpiredAsset
from .archive_paths import directory
from .osdep import anchored, replace_file, sync_file
from .locking import LibraryLock
from .errors import RunLimit
ARCHIVE_VERSION = 15
LIBRARY_REPORT_MAX_BYTES = 64 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def episode_archives(folder):
    return sorted([*Path(folder).glob('*.cbz'), *Path(folder).glob('*.epub'), *Path(folder).glob('*.tar.zst')])


def archive_extension(metadata):
    return {'epub': '.epub', 'tar.zst': '.tar.zst'}.get(metadata.get('format'), '.cbz')


def sidecar_path(path):
    path = Path(path)
    return path.with_name(path.name[:-8] + '.json') if path.name.endswith('.tar.zst') else path.with_suffix('.json')


def archive_from_sidecar(path, metadata):
    return Path(path).with_suffix(archive_extension(metadata))


def safe_name(title):
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")
    title = title[:80].rstrip(" .") or "Untitled"
    # Filesystem limits count encoded bytes on Linux. Leave room for source
    # identifiers, episode numbers, extensions and atomic-publication suffixes.
    title = title.encode('utf-8')[:112].decode('utf-8','ignore').rstrip(' .') or 'Untitled'
    if title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"{s}{i}" for s in ("COM", "LPT") for i in range(1, 10)]}:
        title = "_" + title
    return title


def image_extension(data):
    # Magic-byte detection preserves opaque-named Android JPEG files unchanged.
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in data[-16:]:
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data):
        return ".webp"
    raise ArchiveError("Page is not a recognized complete JPEG/PNG/GIF/WebP; encrypted content is unsupported.")


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ArchiveError("Refusing a symlink in archive output.")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    replace_file(temporary, path)


class Store:
    def __init__(self, root, min_free_bytes=256 * 1024 * 1024, cache_journal=None):
        if cache_journal not in (None, 'wal'):
            raise ArchiveError('Use a supported cache journal.')
        self.root = directory(Path(root).absolute())
        self.min_free_bytes = min_free_bytes
        self.lock = LibraryLock(self.root)
        self.lock.__enter__()
        self.db = None
        self._batching_pages = False
        self._restore_cache_journal = None
        self._indexed_episode_folders = set()
        try:
            self.cache = directory(self.root / ".archive-cache")
            self.cache.chmod(0o700)
            db = self.cache / "index.sqlite3"
            if any(path.is_symlink() for path in (db, *(Path(str(db) + suffix)
                                                       for suffix in ('-wal', '-shm', '-journal')))):
                raise ArchiveError("Refusing a symlink in archive cache.")
            self.db = sqlite3.connect(db)
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, hash TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS series_paths (source TEXT, series_id TEXT, path TEXT, PRIMARY KEY(source,series_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS episode_paths (source TEXT, series_id TEXT, episode_id TEXT, path TEXT, PRIMARY KEY(source,series_id,episode_id))")
            self.db.execute('CREATE TABLE IF NOT EXISTS archived_pages (hash TEXT,path TEXT,member TEXT,size INTEGER,PRIMARY KEY(hash,path,member))')
            columns = self.db.execute('PRAGMA table_info(archived_pages)').fetchall()
            if [r[1] for r in columns if r[5]] == ['hash']:
                # Preserve early single-location cache indexes while adding
                # alternate recovery archives for shared images.
                with self.db:
                    self.db.execute('ALTER TABLE archived_pages RENAME TO archived_pages_single')
                    self.db.execute('CREATE TABLE archived_pages (hash TEXT,path TEXT,member TEXT,size INTEGER,PRIMARY KEY(hash,path,member))')
                    self.db.execute('INSERT INTO archived_pages SELECT hash,path,member,size FROM archived_pages_single')
                    self.db.execute('DROP TABLE archived_pages_single')
            self.db.execute('CREATE TABLE IF NOT EXISTS cache_owners (hash TEXT,source TEXT,series_id TEXT,episode_id TEXT,PRIMARY KEY(hash,source,series_id,episode_id))')
            self.db.commit()
            if cache_journal == 'wal':
                mode = self.db.execute('PRAGMA journal_mode').fetchone()[0]
                if mode not in ('delete', 'wal'):
                    raise ArchiveError('Fast imports require a local DELETE or WAL cache journal.')
                self._restore_cache_journal = mode
                self.db.execute('PRAGMA synchronous=FULL')
                if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                    raise ArchiveError('The local cache does not support fast imports.')
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.db is not None:
                try:
                    if self._restore_cache_journal == 'delete':
                        self.db.commit()
                        if self.db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                            raise ArchiveError('The cache journal could not return to its original mode.')
                finally:
                    self.db.close()
                    self.db = None
        finally:
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
                self.lock = None

    def stored_path(self, relative):
        path = Path(relative)
        if anchored(path) or ".." in path.parts or any(p.is_symlink() for p in (self.root / path, *(self.root / path).parents)):
            raise ArchiveError("Stored archive path is invalid or a symlink.")
        return self.root / path

    def read_metadata(self, path):
        return json.loads(Path(path).read_text())

    def prepare_metadata(self, metadata, series, episode):
        return metadata

    def comparable_metadata(self, metadata):
        return {k: v for k, v in metadata.items() if k != 'imported_at'}

    def publish_metadata(self, path, metadata):
        atomic_json(path, metadata)

    def novel_entries(self, series, episode, modified):
        from .novels import epub_entries
        return epub_entries(series, episode, modified)

    def publish_comic(self, path, entries, metadata, check=None):
        temporary = path.with_suffix(path.suffix + '.tmp')
        if callable(check): check()
        if temporary.is_symlink():
            raise ArchiveError('Refusing a symlink in archive output.')
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as z:
                for name, h, _size in entries:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = 0o644 << 16
                    with (self.cache / h).open('rb') as source, z.open(info, 'w', force_zip64=True) as target:
                        shutil.copyfileobj(source, target)
            replace_file(temporary, path)
        finally:
            if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()

    def series_folder(self, series):
        row = self.db.execute("SELECT path FROM series_paths WHERE source=? AND series_id=?", (series.source, series.id)).fetchone()
        if row:
            return directory(self.stored_path(row[0]))
        folder = self.root / f"{safe_name(series.title)} [{series.id}]"
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or candidate.name.startswith(".") or candidate.is_symlink():
                continue
            try:
                metadata = json.loads((candidate / "series.json").read_text())
                if (metadata.get("source") == series.source and
                        (metadata.get("library_series_id", metadata.get("source_series_id")) == series.id
                         or (series.platform_id is not None and metadata.get('source_series_id')==series.platform_id))):
                    folder = candidate
                    break
            except (OSError, ValueError, AttributeError):
                continue
        directory(folder)
        self.db.execute("INSERT INTO series_paths VALUES (?,?,?)", (series.source, series.id, folder.relative_to(self.root).as_posix()))
        self.db.commit()
        return folder

    def episode_path(self, series, episode, folder=None):
        extension = '.epub' if episode.content_type == 'novel' else '.cbz'
        row = self.db.execute("SELECT path FROM episode_paths WHERE source=? AND series_id=? AND episode_id=?",
                              (series.source, series.id, episode.id)).fetchone()
        if row:
            if Path(row[0]).suffix != extension:
                raise ArchiveError('Episode content format changed; existing export is retained.')
            return self.stored_path(row[0])
        folder = folder or self.series_folder(series)
        identity = (series.source, series.id, str(folder))
        if identity not in self._indexed_episode_folders:
            # Recover portable exports once per series, rather than rereading
            # every old sidecar for every new episode in a growing collection.
            recovered = set()
            for sidecar in folder.glob('*.json'):
                try:
                    metadata = json.loads(sidecar.read_text())
                    episode_id = metadata.get('source_episode_id')
                    if (not isinstance(episode_id, str) or not episode_id
                            or episode_id in recovered
                            or not (metadata.get('series_id') == series.id or
                                    (series.platform_id is not None and metadata.get('source_series_id') == series.platform_id))):
                        continue
                    identifier(episode_id)
                    stored_extension = '.epub' if metadata.get('format') == 'epub' else '.cbz'
                    stored = sidecar.with_suffix(stored_extension)
                    self.db.execute('INSERT OR IGNORE INTO episode_paths VALUES (?,?,?,?)',
                                    (series.source, series.id, episode_id, stored.relative_to(self.root).as_posix()))
                    recovered.add(episode_id)
                except (OSError, ValueError, AttributeError, ArchiveError):
                    continue
            self.db.commit()
            self._indexed_episode_folders.add(identity)
        row = self.db.execute('SELECT path FROM episode_paths WHERE source=? AND series_id=? AND episode_id=?',
                              (series.source, series.id, episode.id)).fetchone()
        if row:
            if Path(row[0]).suffix != extension:
                raise ArchiveError('Episode content format changed; existing export is retained.')
            return self.stored_path(row[0])
        path = folder / f"{episode.order:03d} - {safe_name(episode.title)} [{episode.id}]{extension}"
        self.db.execute("INSERT INTO episode_paths VALUES (?,?,?,?)", (series.source, series.id, episode.id, path.relative_to(self.root).as_posix()))
        self.db.commit()
        return path

    def key(self, source, episode, page):
        if page.local:
            p = Path(page.location)
            try:
                stat = p.stat()
            except OSError:
                raise ArchiveError("Local source page is missing.") from None
            identity = f"{p}:{stat.st_size}:{stat.st_mtime_ns}"
        else:
            u = urlsplit(page.location)
            identity = f"{u.scheme}://{u.netloc}{u.path}"
        return digest(f"{source}:{episode.series_id}:{episode.id}:{page.order}:{identity}".encode())

    def page(self, source, episode, page, adapter, refresh=False):
        data = self.cached_page(source, episode, page) if not refresh else None
        if data is not None:
            return data
        data = adapter.retrieve(page)
        self.remember_page(source, episode, page, data)
        return data

    def cached_page(self, source, episode, page):
        key = self.key(source, episode, page)
        row = self.db.execute("SELECT hash FROM pages WHERE key=?", (key,)).fetchone()
        if row:
            blob = self.cache / row[0]
            if blob.is_file() and not blob.is_symlink():
                data = blob.read_bytes()
                if digest(data) == row[0]:
                    return data
            records = self.db.execute('SELECT path,member,size FROM archived_pages WHERE hash=? LIMIT 100', (row[0],)).fetchall()
            for record in records:
                from .purge import archive_page
                data = archive_page(self.root, record, row[0])
                if data is not None:
                    self.cache_image(data)
                    return data
        return None

    def remember_page(self, source, episode, page, data):
        h = self.cache_image(data)
        key = self.key(source, episode, page)
        self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?)", (key, h))
        self.db.execute('INSERT OR IGNORE INTO cache_owners VALUES (?,?,?,?)', (h,source,episode.series_id,episode.id))
        if not self._batching_pages:
            self.db.commit()
        return data

    @contextmanager
    def page_transaction(self):
        """Commit a completed group once, including successes before a failure.

        Image blobs are saved before their cache mappings. Only the owner
        thread enters this transaction; no network wait occurs inside it.
        """
        previous = self._batching_pages
        self._batching_pages = True
        try:
            yield
        finally:
            self._batching_pages = previous
            if not previous:
                self.db.commit()

    def parallel_entries(self, source, episode, adapter, refresh, progress, workers):
        """Keep a bounded asset pool occupied; drain it before refresh or failure.

        The owner thread alone reserves budgets, touches SQLite/cache, refreshes
        authorization and constructs ordered entries. Adapters without an
        explicitly safe asset capability continue through sequential retrieval.
        """
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        originals = sorted(episode.pages, key=lambda p: p.order)
        ready = {}
        current, refreshed = episode, False
        width = max(4, len(str(len(originals))))
        check = getattr(adapter, 'check', None)
        prepare = getattr(adapter, 'asset_request', None)

        def record(page, data):
            data = self.transform_page(source, episode, page, data)
            name = f'{page.order:0{width}d}{image_extension(data)}'
            ready[page.order] = (name, digest(data), len(data))
            progress(f'Episode {episode.id}: page {page.order}/{len(originals)} ready')

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='comic-assets') as pool:
            while len(ready) < len(originals):
                futures = {}
                errors = []
                remaining = iter(p for p in current.pages if p.order not in ready)
                next_page = next(remaining, None)
                try:
                    while next_page is not None or futures:
                        try:
                            if not errors and callable(check):
                                check()
                            while not errors and next_page is not None and len(futures) < workers:
                                page = next_page
                                cached = self.cached_page(source, current, page) if not refresh else None
                                if cached is not None:
                                    record(page, cached)
                                else:
                                    request = prepare(page) if callable(prepare) else None
                                    if request is None:
                                        if futures:
                                            break  # Wait for budget reservations to be released.
                                        record(page, self.page(source, current, page, adapter, refresh))
                                    else:
                                        try:
                                            future = pool.submit(request.fetch)
                                        except BaseException:
                                            request.cancel()
                                            raise
                                        futures[future] = (page, request)
                                next_page = next(remaining, None)
                        except BaseException as error:
                            errors.append(error)
                        if not futures:
                            break
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        # Detect every completed failure before filling a slot.
                        # After a failure, cache all in-flight successes and stop.
                        try:
                            with self.page_transaction():
                                for future in sorted(done, key=lambda f: futures[f][0].order):
                                    page, request = futures.pop(future)
                                    try:
                                        data = future.result()
                                        request.finish(data)
                                        self.remember_page(source, current, page, data)
                                        record(page, data)
                                    except BaseException as error:
                                        errors.append(error)
                                    finally:
                                        request.cancel()
                        except BaseException as error:
                            errors.append(error)
                        done.clear()
                        data = cached = future = None
                    if errors:
                        raise next((e for e in errors if not isinstance(e, ExpiredAsset)), errors[0])
                except ExpiredAsset:
                    if refreshed:
                        raise ArchiveError('Refreshed reader assets still failed; resume after checking access.') from None
                    updated = adapter.refresh(current)
                    if (updated.id != current.id or updated.series_id != current.series_id
                            or len(updated.pages) != len(current.pages)
                            or any(self.key(source, current, a) != self.key(source, updated, b)
                                   for a, b in zip(current.pages, updated.pages, strict=True))):
                        raise ArchiveError('Reader page manifest changed; restart capture to preserve ordering.') from None
                    current, refreshed = updated, True
                finally:
                    for _, request in futures.values():
                        request.cancel()
                    futures.clear()
                    data = cached = future = None
        return [ready[page.order] for page in originals]

    def transform_page(self, source, episode, page, data):
        return data

    def completed_episode(self, path, metadata):
        pass

    def publishes_here(self):
        """Whether the next archive is saved on this computer, needing its room on disk."""
        return True

    def episode_is_offloaded(self, series, episode, path):
        return False

    def cache_image(self,data):
        image_extension(data)
        h = digest(data)
        blob = self.cache / h
        if blob.is_symlink():
            raise ArchiveError("Refusing a symlink in archive cache.")
        if not blob.exists() or digest(blob.read_bytes()) != h:
            if shutil.disk_usage(self.root).free < len(data) + self.min_free_bytes:
                raise RunLimit("Insufficient free disk space for the next page; cached pages are retained.")
            temporary = self.cache / f"{h}.tmp"
            if temporary.is_symlink():
                raise ArchiveError("Refusing a symlink in archive cache.")
            temporary.write_bytes(data)
            replace_file(temporary, blob)
        return h

    def cover(self,series,adapter,refresh=False):
        if not series.cover_url:
            return None
        key=digest(f'series-cover:{series.source}:{series.platform_id or series.id}:{canonical_url(series.cover_url)}'.encode())
        row=self.db.execute('SELECT hash FROM pages WHERE key=?',(key,)).fetchone()
        if row and not refresh:
            blob=self.cache/row[0]
            if blob.is_file() and not blob.is_symlink():
                data=blob.read_bytes()
                if digest(data)==row[0]:
                    image_extension(data)
                    return data
        data=adapter.retrieve_cover(series)
        if not isinstance(data,bytes) or len(data)>10*1024*1024:
            raise ArchiveError('Cover is absent or exceeds the 10 MiB limit.')
        h=self.cache_image(data)
        self.db.execute('INSERT OR REPLACE INTO pages VALUES (?,?)',(key,h))
        self.db.commit()
        return data

    def metadata_ready(self,series):
        path=self.series_folder(series)/'series.json'
        try:
            if path.is_symlink() or path.stat().st_size>256*1024:
                return False
            value=self.read_metadata(path)
            acquired=datetime.fromisoformat(value.get('metadata_imported_at',''))
            age=(datetime.now(timezone.utc)-acquired).total_seconds()
            return (value.get('metadata_source')=='authenticated_series_page' and 0<=age<86400
                    and not verify_series(path))
        except (OSError,ValueError,TypeError,AttributeError):
            return False

    def series_episode_count(self,series,folder):
        count=0
        for file in folder.glob('*.json'):
            if file.name=='series.json' or file.is_symlink():
                continue
            try:
                m=self.read_metadata(file)
                if (m.get('source')==series.source and archive_from_sidecar(file,m).is_file() and
                        (m.get('series_id')==series.id or
                         (series.platform_id is not None and m.get('source_series_id')==series.platform_id))):
                    count+=1
            except (OSError,ValueError,AttributeError):
                pass
        return count

    def write_series_metadata(self,series,cover=None,enriched=False):
        folder=self.series_folder(series)
        count=self.series_episode_count(series,folder)
        metadata={'schema_version':1,'source':series.source,'source_series_id':series.platform_id or series.id,
                  'title':series.title,'creators':list(series.creators),'description':series.description,
                  'source_url':canonical_url(series.source_url),'episode_count':count,
                  'imported_at':datetime.now(timezone.utc).isoformat()}
        if series.platform_id is not None:
            metadata.update(library_series_id=series.id,source_series_id_kind='numeric')
        if series.slug is not None:
            metadata['source_series_slug']=series.slug
        if series.credits is not None:
            metadata['credits']=series.credits
        if series.cover_url is not None:
            metadata['cover_url']=canonical_url(series.cover_url)
        path=folder/'series.json'
        if path.is_file() and not path.is_symlink():
            try:
                previous=self.read_metadata(path)
                if (previous.get('source')==series.source and
                        (previous.get('library_series_id',previous.get('source_series_id'))==series.id or
                         (series.platform_id is not None and previous.get('source_series_id')==series.platform_id))):
                    for key in ('creators','description','source_url','credits','cover_url','cover_file','cover_sha256','cover_size',
                                'metadata_source','metadata_imported_at','source_series_slug','source_series_id_kind'):
                        if not metadata.get(key) and previous.get(key) is not None:
                            metadata[key]=previous[key]
                    metadata['source_url']=canonical_url(metadata.get('source_url'))
                    if metadata.get('cover_url'):
                        metadata['cover_url']=canonical_url(metadata['cover_url'])
            except (OSError,ValueError,AttributeError):
                pass
        if cover is not None:
            name='cover'+image_extension(cover)
            target=folder/name
            temporary=target.with_suffix(target.suffix+'.tmp')
            if target.is_symlink() or temporary.is_symlink():
                raise ArchiveError('Refusing a symlink in cover output.')
            if not target.is_file() or digest(target.read_bytes())!=digest(cover):
                if shutil.disk_usage(self.root).free < len(cover)+self.min_free_bytes:
                    raise RunLimit('Insufficient disk space for the cover; episode archives are retained.')
                with temporary.open('wb') as output:
                    output.write(cover); output.flush(); os.fsync(output.fileno())
                replace_file(temporary, target)
            metadata.update(cover_file=name,cover_sha256=digest(cover),cover_size=len(cover))
        if enriched:
            metadata.update(metadata_source='authenticated_series_page',metadata_imported_at=datetime.now(timezone.utc).isoformat())
        self.publish_metadata(path,metadata)
        return folder

    def archive(self, manifest, adapter, refresh=False, progress=print, download_workers=1):
        from .downloads import adapter_profile, validate_download_workers
        validate_download_workers(download_workers, adapter_profile(adapter))
        series = manifest.series
        folder = self.series_folder(series)
        for episode in sorted(manifest.episodes, key=lambda e: e.order):
            if not refresh and self.episode_is_offloaded(series, episode, self.episode_path(series, episode, folder)):
                progress(f'Episode {episode.id}: already uploaded and verified; local archive was removed')
                continue
            if episode.content_type == 'novel':
                self.archive_novel(series, episode, adapter, folder, progress)
                continue
            cbz = self.episode_path(series, episode, folder)
            entries = []
            refreshed = False
            current = episode
            check = getattr(adapter, 'check', None)
            originals = sorted(episode.pages, key=lambda p: p.order)
            if download_workers > 1:
                entries = self.parallel_entries(series.source, episode, adapter, refresh, progress, download_workers)
                originals = ()
            for original in originals:
                check=getattr(adapter,'check',None)
                if callable(check):check()
                page = next(p for p in current.pages if p.order == original.order)
                try:
                    data = self.page(series.source, current, page, adapter, refresh)
                except ExpiredAsset:
                    if refreshed:
                        raise ArchiveError("Refreshed reader assets still failed; resume after checking access.") from None
                    current = adapter.refresh(current)
                    refreshed = True
                    page = next(p for p in current.pages if p.order == page.order)
                    data = self.page(series.source, current, page, adapter, refresh)
                width = max(4, len(str(len(episode.pages))))
                data = self.transform_page(series.source, episode, page, data)
                name = f"{page.order:0{width}d}{image_extension(data)}"
                entries.append((name, digest(data), len(data)))
                progress(f"Episode {episode.id}: page {page.order}/{len(episode.pages)} ready")
            sidecar = sidecar_path(cbz)
            hashes = [{"file": n, "sha256": h, "size": size} for n, h, size in entries]
            metadata = {
                "schema_version": 1, "source": series.source,
                "source_episode_id": episode.id, "series_id": series.id,
                "title": episode.title, "episode_number": episode.order,
                "source_url": canonical_url(episode.source_url),
                "access_state": episode.access, "published_at": episode.published_at,
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "page_count": len(entries), "content_hashes": hashes,
            }
            if series.platform_id is not None:
                metadata["source_series_id"] = series.platform_id
            metadata = self.prepare_metadata(metadata, series, episode)
            if cbz.is_symlink() or sidecar.is_symlink():
                raise ArchiveError("Refusing a symlink in archive output.")
            # Preserve both bytes and import time when rerunning a verified episode.
            if cbz.is_file() and sidecar.is_file():
                try:
                    previous = self.read_metadata(sidecar)
                    if (previous.get("source_episode_id") == episode.id and previous.get("series_id") == series.id
                            and metadata["published_at"] is None):
                        metadata["published_at"] = previous.get("published_at")
                    equal = self.comparable_metadata(previous) == self.comparable_metadata(metadata)
                    if equal and not verify_episode(cbz):
                        progress(f"Episode {episode.id}: already archived and verified")
                        self.completed_episode(cbz, previous)
                        continue
                except (ValueError, OSError):
                    pass
            if self.publishes_here() and shutil.disk_usage(self.root).free < sum(size for _, _, size in entries) + self.min_free_bytes + 1024 * 1024:
                raise RunLimit("Insufficient free disk space to publish the archive; original pages remain cached.")
            self.publish_comic(cbz, entries, metadata, check)
            self.publish_metadata(sidecar, metadata)
            progress(f"Episode {episode.id}: archive and JSON saved")
        self.write_series_metadata(series)
        return folder

    def archive_novel(self, series, episode, adapter, folder, progress):
        from .novels import validate_fragment
        text = episode.text_html.encode('utf-8')
        character_count = len(''.join(validate_fragment(episode.text_html, episode.illustrations).itertext()))
        path = self.episode_path(series, episode, folder)
        sidecar = sidecar_path(path)
        if path.is_symlink() or sidecar.is_symlink():
            raise ArchiveError('Refusing a symlink in novel output.')
        metadata = {'schema_version': 1, 'source': series.source, 'source_episode_id': episode.id,
                    'series_id': series.id, 'title': episode.title, 'episode_number': episode.order,
                    'source_url': canonical_url(episode.source_url), 'access_state': episode.access,
                    'published_at': episode.published_at, 'format': 'epub', 'content_type': 'novel',
                    'page_count': 0, 'chapter_count': 1, 'character_count': character_count,
                    'text_sha256': digest(text), 'text_size': len(text)}
        if episode.illustrations:
            metadata['illustration_count'] = len(episode.illustrations)
        if series.platform_id is not None:
            metadata['source_series_id'] = series.platform_id
        metadata = self.prepare_metadata(metadata, series, episode)
        if path.is_file() and sidecar.is_file():
            try:
                previous = self.read_metadata(sidecar)
                previous_comparable = self.comparable_metadata(previous)
                if all(previous_comparable.get(k) == v for k, v in self.comparable_metadata(metadata).items()) and not verify_episode(path):
                    progress(f'Episode {episode.id}: EPUB already archived and verified')
                    self.completed_episode(path, previous)
                    return
            except (OSError, ValueError):
                pass
        check = getattr(adapter, 'check', None)
        if callable(check):
            check()
        consume = getattr(adapter, 'consume_novel', None)
        if callable(consume):
            consume(episode)
        imported = datetime.now(timezone.utc).isoformat()
        modified = datetime.fromisoformat(imported).strftime('%Y-%m-%dT%H:%M:%SZ')
        entries = self.novel_entries(series, episode, modified)
        metadata.update(imported_at=imported, content_hashes=[
            {'file': name, 'sha256': digest(data), 'size': len(data)} for name, data in entries.items()])
        if shutil.disk_usage(self.root).free < sum(map(len, entries.values())) + self.min_free_bytes + 1024 * 1024:
            raise RunLimit('Insufficient free disk space to publish the EPUB; its queue is retained.')
        temporary = path.with_suffix('.epub.tmp')
        if temporary.is_symlink():
            raise ArchiveError('Refusing a symlink in novel output.')
        if callable(check):
            check()
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as z:
                for name, data in entries.items():
                    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.external_attr = 0o644 << 16
                    z.writestr(entry, data)
            with temporary.open('rb') as stream:
                if verify_episode_contents(stream, metadata):
                    raise ArchiveError('New novel export failed verification; its queue is retained.')
            sync_file(temporary)
            replace_file(temporary, path)
            self.publish_metadata(sidecar, metadata)
        finally:
            if temporary.is_file() and not temporary.is_symlink():
                temporary.unlink()
        progress(f'Episode {episode.id}: EPUB and JSON saved')


def verify_series_contents(metadata, cover=None, *, image_decoder=None):
    """Verify one pinned series/cover snapshot, independently of filenames."""
    try:
        if metadata.get('schema_version') == 2:
            from .publication import validate_public_metadata
            validate_public_metadata(metadata,series=True)
        else:
            identifier(metadata['source']);identifier(metadata['source_series_id'])
        if not isinstance(metadata.get('title'),str) or not metadata['title'].strip():
            return ['Invalid series identity or title']
        filename=metadata.get('cover_file')
        if filename is None:
            return []
        if not isinstance(filename,str) or not re.fullmatch(r'cover\.(jpg|png|gif|webp)',filename):
            return ['Invalid cover filename']
        if not isinstance(cover,bytes) or len(cover)>10*1024*1024:
            return ['Cover is missing, a symlink or oversized']
        if (type(metadata.get('cover_size')) is not int or len(cover)!=metadata['cover_size']
                or digest(cover)!=metadata.get('cover_sha256')):
            return ['Cover size or hash does not match metadata']
        image_extension(cover)
        if image_decoder is not None:
            try:
                image_decoder(cover)
            except ArchiveError:
                return ['Cover image pixels could not be decoded']
        return []
    except (ValueError,TypeError,KeyError,AttributeError,ArchiveError):
        return ['Invalid or unreadable series metadata/cover']


def verify_series(path, *, image_decoder=None):
    try:
        if path.is_symlink():
            return ['Series metadata is a symlink']
        metadata=json.loads(path.read_text())
        filename=metadata.get('cover_file')
        if filename is None:
            return verify_series_contents(metadata)
        if not isinstance(filename,str) or not re.fullmatch(r'cover\.(jpg|png|gif|webp)',filename):
            return ['Invalid cover filename']
        cover=path.parent/filename
        if cover.is_symlink() or not cover.is_file() or cover.stat().st_size>10*1024*1024:
            return ['Cover is missing, a symlink or oversized']
        data=cover.read_bytes()
        return verify_series_contents(metadata,data,image_decoder=image_decoder)
    except (OSError,ValueError,TypeError,AttributeError,ArchiveError):
        return ['Invalid or unreadable series metadata/cover']


def verify_episode_contents(archive, meta, *, image_decoder=None):
    """Verify already-open comic or novel bytes against one captured JSON object."""
    try:
        if meta.get('schema_version') == 2:
            from .publication import validate_public_metadata
            validate_public_metadata(meta)
        if meta.get('format') == 'tar.zst':
            from .publication import verify_compressed_comic
            return verify_compressed_comic(archive, meta, image_decoder=image_decoder)
        if meta.get('format') == 'epub':
            from .novels import EPUB_FILES, MAX_TEXT_BYTES, MAX_ILLUSTRATION_BYTES, epub_files, verify_epub
            hashes = meta['content_hashes']
            expected = epub_files(meta)
            if [item['file'] for item in hashes] != expected:
                return ['Invalid EPUB file manifest']
            with zipfile.ZipFile(archive) as z:
                if z.namelist() != expected:
                    return ['EPUB membership differs from metadata']
                for item in hashes:
                    member = z.getinfo(item['file'])
                    cap = MAX_TEXT_BYTES + 65536 if item['file'] in EPUB_FILES else MAX_ILLUSTRATION_BYTES
                    if (type(item['size']) is not int or not 0 < member.file_size <= cap
                            or member.file_size != item['size'] or digest(z.read(member)) != item['sha256']):
                        return ['EPUB content size or hash mismatch']
                    if item['file'] not in EPUB_FILES and image_decoder is not None:
                        image_decoder(z.read(member))
                verify_epub(z, meta)
            return []
        pages = meta["content_hashes"]
        expected = [p["file"] for p in pages]
        if not expected or len(set(expected)) != len(expected) or meta["page_count"] != len(pages):
            return ["Invalid page count or duplicate pages"]
        with zipfile.ZipFile(archive) as z:
            if z.namelist() != expected:
                return ["Archive page order or membership differs from metadata"]
            for i, p in enumerate(pages, 1):
                info = z.getinfo(p["file"])
                if info.file_size != p["size"] or info.file_size > 100 * 1024 * 1024:
                    return ["Page size mismatch"]
                data = z.read(p["file"])
                width = max(4, len(str(len(pages))))
                if p["file"] != f"{i:0{width}d}{image_extension(data)}" or digest(data) != p["sha256"]:
                    return ["Page content or numbering mismatch"]
                if image_decoder is not None:
                    try:
                        image_decoder(data)
                    except ArchiveError:
                        return [f'Page {i}: image pixels could not be decoded']
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, ET.ParseError, zipfile.BadZipFile, RuntimeError, ArchiveError):
        return ["Unreadable, incomplete or corrupt archive/metadata"]


def verify_episode(cbz, *, image_decoder=None):
    try:
        if cbz.is_symlink() or sidecar_path(cbz).is_symlink():
            return ['Archive or sidecar is a symlink']
        meta=json.loads(sidecar_path(cbz).read_text())
        if not cbz.name.endswith(archive_extension(meta)):
            return ['Archive extension differs from metadata format']
        with cbz.open('rb') as archive:
            return verify_episode_contents(archive,meta,image_decoder=image_decoder)
    except (OSError,ValueError,TypeError,RecursionError):
        return ['Unreadable, incomplete or corrupt archive/metadata']


def verify(root, *, decode_images=False):
    image_decoder = None
    if decode_images:
        from .image_validation import decoded_image_checker
        image_decoder = decoded_image_checker()
    root = Path(root)
    errors = []
    archives = sorted(p for pattern in ('*.cbz', '*.epub', '*.tar.zst') for p in root.rglob(pattern)
                      if not {'.archive-state', '.archive-cache'}.intersection(p.relative_to(root).parts))
    if not archives:
        return ["No portable episode archives found"]
    for cbz in archives:
        errors.extend(f"{cbz.name}: {e}" for e in verify_episode(cbz,image_decoder=image_decoder))
    for sidecar in root.rglob("*.json"):
        if sidecar.name=='series.json' and not {'.archive-cache','.archive-state'}.intersection(sidecar.parts):
            errors.extend(f'{sidecar.parent.name}: {e}' for e in verify_series(sidecar,image_decoder=image_decoder))
            continue
        if sidecar.name != "series.json" and not {".archive-cache", ".archive-state"}.intersection(sidecar.parts) and not any(sidecar.with_suffix(ext).is_file() for ext in ('.cbz', '.epub', '.tar.zst')):
            # A discovery report is metadata about potential imports, not an
            # episode sidecar. Keep detecting actual orphan episode metadata.
            try:
                if not sidecar.is_symlink() and sidecar.stat().st_size <= LIBRARY_REPORT_MAX_BYTES:
                    report = json.loads(sidecar.read_text())
                    if (isinstance(report, dict) and report.get("document_type") in {"comic_access_discovery", "comic_archive_reconciliation"}
                            and report.get("schema_version") == 1 and report.get("access_snapshot_only") is True
                            and "source_episode_id" not in report and "content_hashes" not in report):
                        continue
            except (OSError, ValueError):
                pass
            errors.append(f"{sidecar.name}: missing CBZ or EPUB")
    return errors
