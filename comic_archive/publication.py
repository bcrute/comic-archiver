"""Source-neutral public exports; importer provenance stays in private state."""
import json
import os
from pathlib import Path
import re
import tarfile
import zipfile
import shutil
import sqlite3
import uuid
from dataclasses import replace

import zstandard

from .archive import Store, atomic_json, digest, image_extension, safe_name, archive_from_sidecar
from .archive_paths import directory
from .models import ArchiveError
from .osdep import replace_file, shortened, units

MAX_PAGE_BYTES = 100 * 1024 * 1024
# What follows a title in a name: ' [' with twelve characters of the export id and ']', then the longest
# ending a file gets while it is written ('.tar.zst.tmp').
AFTER_TITLE, AFTER_NAME = 15, 12
# Room a series folder leaves for the files inside it: a separator, the episode number, 48 characters of title.
EPISODE_ROOM = 1 + 12 + 48 + AFTER_TITLE + AFTER_NAME
EPISODE_FIELDS = ('title', 'episode_number', 'published_at', 'imported_at', 'page_count',
                  'content_hashes', 'format', 'content_type', 'chapter_count', 'character_count',
                  'text_sha256', 'text_size', 'illustration_count', 'image_profile', 'images_recompressed')
SERIES_FIELDS = ('title', 'creators', 'episode_count', 'cover_file', 'cover_sha256', 'cover_size')


def valid_export_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value))


def public_metadata(metadata, export_id, *, series=False):
    return {'schema_version': 2, 'export_id': export_id,
            **{key: metadata[key] for key in (SERIES_FIELDS if series else EPISODE_FIELDS) if key in metadata}}


def validate_public_metadata(value, *, series=False):
    fields=SERIES_FIELDS if series else EPISODE_FIELDS
    if (set(value)-{'schema_version','export_id',*fields} or value.get('schema_version')!=2
            or not valid_export_id(value.get('export_id')) or not isinstance(value.get('title'),str)
            or not value['title'].strip()):
        raise ValueError('Invalid neutral export metadata')
    if not series and (value.get('format') not in {'cbz','tar.zst','epub'}
                       or value.get('content_type') != ('novel' if value['format']=='epub' else 'comic')):
        raise ValueError('Invalid neutral export format')
    if not series and ('image_profile' in value or 'images_recompressed' in value):
        from .image_profiles import validate_profile
        validate_profile(value.get('image_profile'))
        if type(value.get('images_recompressed')) is not bool:
            raise ValueError('Invalid image compression declaration')


def metadata_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def provenance_record(root, sidecar, value, metadata):
    """The private record for a public file about to be written, keeping the one it replaces readable."""
    record = {'schema_version': 1, 'sidecar': sidecar.relative_to(root).as_posix(),
              'public_hash': metadata_hash(value), 'metadata': metadata}
    try:
        current = json.loads(sidecar.read_text()) if sidecar.is_file() and not sidecar.is_symlink() else None
        if (isinstance(current, dict) and current.get('schema_version') == 2
                and current.get('export_id') == value['export_id'] and current != value):
            record['previous'] = {'public_hash': metadata_hash(current), 'metadata': import_metadata(sidecar)}
    except (OSError, ValueError, TypeError):
        pass  # Nothing readable is there to keep.
    return record


def import_metadata(sidecar):
    """Recover source identity only from the matching private provenance record."""
    sidecar = Path(sidecar)
    if any(p.is_symlink() for p in (sidecar, *sidecar.parents)) or sidecar.stat().st_size > 16*1024*1024:
        raise ValueError('Unsafe metadata')
    value = json.loads(sidecar.read_text())
    if not isinstance(value, dict): raise ValueError('Invalid metadata')
    if value.get('schema_version') != 2: return value
    validate_public_metadata(value,series=sidecar.name=='series.json')
    export_id = value.get('export_id')
    if not valid_export_id(export_id): raise ValueError('Invalid export identity')
    root = sidecar.parent.parent
    private = root / '.archive-state/provenance' / (export_id + '.json')
    if any(p.is_symlink() for p in (private, *private.parents)) or private.stat().st_size > 16*1024*1024:
        raise ValueError('Unsafe private provenance')
    record = json.loads(private.read_text())
    if not isinstance(record, dict) or record.get('sidecar') != sidecar.relative_to(root).as_posix():
        raise ValueError('Export provenance mismatch')
    if record.get('public_hash') != metadata_hash(value) and isinstance(record.get('previous'), dict):
        # Stopped between the private and public writes: the public file is still the one before.
        record = record['previous']
    if record.get('public_hash') != metadata_hash(value) or not isinstance(record.get('metadata'), dict):
        raise ValueError('Export provenance mismatch')
    metadata = record['metadata']
    if public_metadata(metadata, export_id, series=sidecar.name == 'series.json') != value:
        raise ValueError('Export metadata differs from private provenance')
    return metadata


class StrictZstdReader:
    """A bounded single frame reader that rejects truncation and trailing frames."""
    def __init__(self, source, maximum):
        self.source, self.maximum, self.total = source, maximum, 0
        self.decoder = zstandard.ZstdDecompressor(max_window_size=8*1024*1024).decompressobj()
        self.first_chunk = True
        self.buffer = bytearray()

    def read(self, size=-1):
        if size < 0: raise ValueError('Unbounded archive reads are unsupported')
        while len(self.buffer) < size and not self.decoder.eof:
            compressed = self.source.read(4096)
            if not compressed: raise ValueError('Incomplete compressed frame')
            if self.first_chunk:
                if zstandard.get_frame_parameters(compressed).window_size > 8*1024*1024:
                    raise ValueError('Compressed window exceeds the supported size')
                self.first_chunk = False
            data = self.decoder.decompress(compressed)
            self.total += len(data)
            if self.total > self.maximum: raise ValueError('Archive exceeds its manifest')
            self.buffer.extend(data)
            if self.decoder.eof and (self.decoder.unused_data or self.source.read(1)):
                raise ValueError('Unexpected data after compressed frame')
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def finish(self):
        # TAR readers stop at their terminator. Finish the frame and its checksum.
        while data := self.read(65536):
            if any(data): raise ValueError('Unexpected data after TAR terminator')


def comic_pages(meta):
    pages = meta['content_hashes']
    if (not isinstance(pages, list) or not pages or len(pages) > 10000
            or type(meta.get('page_count')) is not int or meta['page_count'] != len(pages)):
        raise ValueError('Invalid page count')
    width = max(4, len(str(len(pages))))
    for i, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or type(page.get('size')) is not int
                or not 0 < page['size'] <= MAX_PAGE_BYTES
                or not isinstance(page.get('file'), str)
                or not re.fullmatch(f'{i:0{width}d}\\.(?:jpg|png|gif|webp)', page['file'])
                or not isinstance(page.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', page['sha256'])):
            raise ValueError('Invalid page manifest')
    return pages


def verify_compressed_comic(source, meta, *, image_decoder=None):
    try:
        pages = comic_pages(meta)
        body_size=sum(512 + ((p['size']+511)//512)*512 for p in pages)
        maximum=((body_size+1024+10239)//10240)*10240
        reader = StrictZstdReader(source, maximum)
        with tarfile.open(fileobj=reader, mode='r|', bufsize=512) as archive:
            for position, page in enumerate(pages, 1):
                member = archive.next()
                if (member is None or not member.isfile() or member.name != page['file']
                        or member.size != page['size'] or member.pax_headers
                        or member.linkname or member.uid != 0 or member.gid != 0
                        or member.uname or member.gname or member.mtime != 0):
                    return ['Archive page order, size or membership differs from metadata']
                stream = archive.extractfile(member)
                if stream is None: return ['Unreadable archive page']
                data = stream.read(page['size']+1)
                if len(data) != page['size'] or digest(data) != page['sha256'] or image_extension(data) != Path(page['file']).suffix:
                    return ['Page content or numbering mismatch']
                if image_decoder is not None:
                    try: image_decoder(data)
                    except ArchiveError: return [f'Page {position}: image pixels could not be decoded']
            if archive.next() is not None: return ['Unexpected archive member']
        reader.finish()
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, zstandard.ZstdError, ArchiveError):
        return ['Unreadable, incomplete or corrupt archive/metadata']


def write_compressed_comic(path, metadata, open_page, check=None):
    """Stream originals into a deterministic TAR and a checksummed Zstd frame."""
    pages = comic_pages(metadata)
    temporary = path.with_name(path.name + '.tmp')
    if any(p.is_symlink() for p in (path, temporary, *path.parents)):
        raise ArchiveError('Refusing unsafe archive output.')
    try:
        with temporary.open('wb') as output:
            with zstandard.ZstdCompressor(level=1, write_checksum=True).stream_writer(output, closefd=False) as compressed:
                with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
                    for page in pages:
                        if callable(check): check()
                        member = tarfile.TarInfo(page['file'])
                        member.size, member.mode = page['size'], 0o644
                        with open_page(page) as source: archive.addfile(member, source)
            output.flush(); os.fsync(output.fileno())
        with temporary.open('rb') as stream:
            if verify_compressed_comic(stream, metadata):
                raise ArchiveError('New compressed export failed verification; originals are retained.')
        replace_file(temporary, path)
    finally:
        if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()


def _zip_pages(output, pages, open_page, check):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for page in pages:
            if callable(check): check()
            info = zipfile.ZipInfo(page['file'], date_time=(1980,1,1,0,0,0))
            info.external_attr = 0o644 << 16
            with open_page(page) as source, archive.open(info,'w',force_zip64=True) as target:
                shutil.copyfileobj(source, target, 65536)


def cbz_bytes(metadata, open_page, check=None):
    """The CBZ write_cbz saves, held in memory instead."""
    import io
    from .archive import verify_episode_contents
    output = io.BytesIO()
    _zip_pages(output, comic_pages(metadata), open_page, check)
    if verify_episode_contents(output, metadata):
        raise ArchiveError('New CBZ failed verification; the episode is downloaded again next time.')
    return output.getvalue()


def write_cbz(path, metadata, open_page, check=None):
    """Standard ZIP comic with original page bytes and deterministic ordering."""
    from .archive import verify_episode_contents
    pages = comic_pages(metadata)
    temporary = path.with_name(path.name + '.tmp')
    if any(p.is_symlink() for p in (path, temporary, *path.parents)):
        raise ArchiveError('Refusing unsafe CBZ output.')
    try:
        with temporary.open('wb') as output:
            _zip_pages(output, pages, open_page, check)
            output.flush(); os.fsync(output.fileno())
        with temporary.open('rb') as source:
            if verify_episode_contents(source, metadata):
                raise ArchiveError('New CBZ failed verification; original pages are retained.')
        replace_file(temporary, path)
    finally:
        if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()


# An episode bigger than this, held in memory for a direct upload, is saved here instead.
DIRECT_MEMORY = 256 * 1024**2


class DirectEpisode:
    """One episode's pages, held in memory while they download, for an upload straight to B2.

    Nothing of it is saved here, so after a crash the episode is downloaded again.
    """
    def __init__(self, archive):
        self.archive = archive
        self.pages = {}
        self.rows = []  # Cache index rows, written only if the pages are saved here after all.
        self.size = 0
        self.data = None

    def keep(self, data):
        image_extension(data)
        h = digest(data)
        if h not in self.pages:
            self.pages[h] = data
            self.size += len(data)
        return h


class PublicationStore(Store):
    """The publisher used by all importer entry points, with no public source IDs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from .storage import policy
            from .image_profiles import prerequisites
            self.storage_policy = policy(self.root)
            prerequisites(self.storage_policy['image_profile'])
            self._originals = {}
            self._recompressed = set()
            self._episode_profiles = {}
            self._episode_formats = {}
            self._cloud_pending = set()
            self._streaming_episode = None
            self._direct = None
            self._direct_ready = []
            self._uploading = None
            self.db.execute('''CREATE TABLE IF NOT EXISTS export_ids
                (source TEXT,series_id TEXT,episode_id TEXT,id TEXT UNIQUE NOT NULL,
                 PRIMARY KEY(source,series_id,episode_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS export_series_keys
                (source TEXT,source_series_id TEXT,series_id TEXT NOT NULL,
                 PRIMARY KEY(source,source_series_id))''')
            self.db.commit()
            self.provenance = directory(self.root / '.archive-state/provenance')
            self.provenance.chmod(0o700)
            self._published_sidecars={}
            self._recover_series_keys()
        except BaseException:
            self.close(); raise

    def _recover_series_keys(self):
        # Upgrade existing neutral exports without allocating new UUIDs or paths.
        rows=self.db.execute('''SELECT e.source,e.series_id,p.path FROM export_ids e
            JOIN series_paths p ON p.source=e.source AND p.series_id=e.series_id
            WHERE e.episode_id='' AND NOT EXISTS
                (SELECT 1 FROM export_series_keys k WHERE k.source=e.source AND k.series_id=e.series_id)''').fetchall()
        with self.db:
            for source,key,folder in rows:
                path=self.stored_path(folder)/'series.json'
                if not path.is_file(): continue  # A reserved export is not published yet.
                metadata=import_metadata(path)
                if (metadata.get('source')!=source or
                        metadata.get('library_series_id',metadata.get('source_series_id'))!=key):
                    raise ArchiveError('Stored series export identity differs from its private metadata.')
                canonical=metadata['source_series_id']
                previous=self.db.execute('SELECT series_id FROM export_series_keys WHERE source=? AND source_series_id=?',(source,canonical)).fetchone()
                if previous and previous[0]!=key:
                    raise ArchiveError('Multiple export identities exist for one series; existing files are retained.')
                self.db.execute('INSERT OR IGNORE INTO export_series_keys VALUES (?,?,?)',(source,canonical,key))

    def series_export_key(self, source, series_id, platform_id=None):
        canonical=platform_id or series_id
        row=self.db.execute('SELECT series_id FROM export_series_keys WHERE source=? AND source_series_id=?',(source,canonical)).fetchone()
        if row: return row[0]
        self.db.execute('INSERT INTO export_series_keys VALUES (?,?,?)',(source,canonical,series_id))
        self.db.commit()
        return series_id

    def export_id(self, source, series_id, episode_id=''):
        row = self.db.execute('SELECT id FROM export_ids WHERE source=? AND series_id=? AND episode_id=?',
                              (source, series_id, episode_id)).fetchone()
        if row: return row[0]
        value = str(uuid.uuid4())
        self.db.execute('INSERT INTO export_ids VALUES (?,?,?,?)', (source, series_id, episode_id, value))
        self.db.commit()
        return value

    def series_folder(self, series):
        key=self.series_export_key(series.source,series.id,series.platform_id)
        export_id = self.export_id(series.source, key)
        row = self.db.execute('SELECT path FROM series_paths WHERE source=? AND series_id=?', (series.source, key)).fetchone()
        if row and row[0].endswith('[' + export_id[:12] + ']'):
            return directory(self.stored_path(row[0]))
        title = shortened(safe_name(series.title), self.root, 1, AFTER_TITLE + EPISODE_ROOM)
        folder = directory(self.root / f'{title} [{export_id[:12]}]')
        self.db.execute('INSERT OR REPLACE INTO series_paths VALUES (?,?,?)',
                        (series.source, key, folder.relative_to(self.root).as_posix()))
        self.db.commit()
        return folder

    def episode_path(self, series, episode, folder=None):
        extension = '.epub' if episode.content_type == 'novel' else '.cbz'
        key=self.series_export_key(series.source,series.id,series.platform_id)
        export_id = self.export_id(series.source, key, episode.id)
        folder = folder or self.series_folder(series)
        row = self.db.execute('SELECT path FROM episode_paths WHERE source=? AND series_id=? AND episode_id=?',
                              (series.source, key, episode.id)).fetchone()
        legacy = bool(row and episode.content_type == 'comic' and row[0].endswith('['+export_id[:12]+'].tar.zst'))
        if legacy:
            from .cloud import offloaded
            from .archive import verify_episode
            candidate = self.stored_path(row[0])
            legacy = (candidate.is_file() and not verify_episode(candidate)) or offloaded(self.root,candidate)
            if not legacy and candidate.is_file(): self._retire_legacy(candidate)
        if row and (row[0].endswith('[' + export_id[:12] + ']' + extension) or legacy):
            path = self.stored_path(row[0])
            from .archive import sidecar_path, verify_episode
            if sidecar_path(path).is_file() and ((path.is_file() and not verify_episode(path)) or legacy):
                previous = self.read_metadata(sidecar_path(path))
                self._episode_profiles[(series.source, episode.series_id, episode.id)] = previous.get('image_profile', 'original')
                self._episode_formats[(series.source, episode.series_id, episode.id)] = previous.get('format', 'cbz')
            return path
        number = f'{episode.order:03d} - '
        title = shortened(safe_name(episode.title), self.root, 1 + units(folder.name) + 1 + len(number), AFTER_TITLE + AFTER_NAME)
        path = folder / f'{number}{title} [{export_id[:12]}]{extension}'
        self.db.execute('INSERT OR REPLACE INTO episode_paths VALUES (?,?,?,?)',
                        (series.source, key, episode.id, path.relative_to(self.root).as_posix()))
        self.db.commit()
        return path

    def _retire_legacy(self, old):
        """Move a legacy tar that fails verification aside, so the CBZ saved again in its place is all that is left."""
        from .archive import sidecar_path
        folder = directory(self.root/'.archive-state/legacy-exports'/old.parent.name); folder.chmod(0o700)
        retained, sidecar = folder/old.name, sidecar_path(old)
        if any(p.exists() or p.is_symlink() for p in (retained, sidecar_path(retained))):
            raise ArchiveError('Retained tar already exists; both copies held for review.')
        try:
            # A full private manifest, as conversion keeps, when the public one can still be read.
            atomic_json(sidecar_path(retained), import_metadata(sidecar)); sidecar_path(retained).chmod(0o600)
            sidecar.unlink()
        except (OSError, ValueError, TypeError):
            if sidecar.is_file() and not sidecar.is_symlink(): replace_file(sidecar, sidecar_path(retained))
        replace_file(old, retained); retained.chmod(0o600)
        self.db.execute('DELETE FROM archived_pages WHERE path=?', (old.relative_to(self.root).as_posix(),))
        self.db.commit()

    def read_metadata(self, path):
        return import_metadata(path)

    def _series_sidecars(self,folder):
        if folder not in self._published_sidecars:
            names=set()
            for path in folder.glob('*.json'):
                if path.name=='series.json': continue
                try:
                    metadata=self.read_metadata(path)
                    archive = archive_from_sidecar(path,metadata)
                    from .cloud import offloaded
                    if archive.is_file() or offloaded(self.root, archive): names.add(path.name)
                except (OSError,ValueError,TypeError): pass
            self._published_sidecars[folder]=names
        return self._published_sidecars[folder]

    def series_episode_count(self,series,folder):
        return len(self._series_sidecars(folder))

    def write_series_metadata(self,series,cover=None,enriched=False):
        key=self.series_export_key(series.source,series.id,series.platform_id)
        result = super().write_series_metadata(replace(series,id=key),cover=cover,enriched=enriched)
        try:
            # One episode uploads at a time, and it is done before anything else touches the cloud.
            self._finish_upload()
        except BaseException:
            from .cloud import keep_here
            for archive, data in self._direct_ready:
                try:
                    keep_here(self.root, archive, data)
                except (ArchiveError, OSError):
                    pass  # Not saved anywhere: the episode is downloaded again next time.
            self._direct_ready.clear()
            raise
        if self._cloud_pending:
            from .cloud import queue_episode, upload_pending
            for path in sorted(self._cloud_pending):
                queue_episode(self.root, path)
            self._cloud_pending.clear()
            upload_pending(self.root)
        if self._direct_ready:
            # The series files exist now, so the episode held in memory goes up with them while the next one downloads.
            from .cloud import DirectUpload
            archive, data = self._direct_ready.pop()
            self._uploading = DirectUpload(self.root, archive, data)
        return result

    def _finish_upload(self):
        uploading, self._uploading = self._uploading, None
        if uploading is not None:
            uploading.finish()

    def comparable_metadata(self, metadata):
        value = super().comparable_metadata(metadata)
        # Cache ownership is importer bookkeeping; slug changes must not
        # rewrite verified exports or their original import timestamp.
        value.pop('cache_series_id', None)
        value.setdefault('image_profile', 'original')
        value.setdefault('images_recompressed', False)
        value.setdefault('original_content_hashes', [])
        return value

    def prepare_metadata(self, metadata, series, episode):
        key=self.series_export_key(series.source,series.id,series.platform_id)
        return metadata | {'export_id': self.export_id(series.source, key, episode.id),
                           'series_id':key,
                           'access_expires_at':episode.access_expires_at,
                           'format': 'epub' if episode.content_type == 'novel' else self._episode_formats.get((series.source,episode.series_id,episode.id),'cbz'),
                           'content_type': episode.content_type,
                           'image_profile': self._episode_profiles.get((series.source, episode.series_id, episode.id), self.storage_policy['image_profile']) if episode.content_type == 'comic' else 'original',
                           'images_recompressed': (series.source, episode.series_id, episode.id) in self._recompressed,
                           'original_content_hashes': [item for _, item in sorted(self._originals.get((series.source, episode.series_id, episode.id), {}).items())],
                           'cache_series_id': series.id}

    def cache_image(self, data):
        if self._direct is None or self._direct.data is not None:
            return super().cache_image(data)
        h = self._direct.keep(data)
        if self._direct.size > DIRECT_MEMORY:
            self._save_direct_pages()
        return h

    def remember_page(self, source, episode, page, data):
        if self._direct is None or self._direct.data is not None:
            return super().remember_page(source, episode, page, data)
        direct = self._direct
        h = direct.keep(data)
        direct.rows.append((self.key(source, episode, page), h, source, episode.series_id, episode.id))
        if direct.size > DIRECT_MEMORY:
            self._save_direct_pages()
        return data

    def _save_direct_pages(self):
        """Too big to hold: save the pages kept so far, and the rest of the episode, here."""
        direct, self._direct = self._direct, None
        for data in direct.pages.values():
            super().cache_image(data)
        for key, h, source, series_id, episode_id in direct.rows:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?)", (key, h))
            self.db.execute('INSERT OR IGNORE INTO cache_owners VALUES (?,?,?,?)', (h, source, series_id, episode_id))
        if not self._batching_pages:
            self.db.commit()

    def _keep_direct_pages(self):
        """An episode stopped partway (paused, held or failed) keeps the pages it has: saved here, they are read
        from here when it continues instead of being downloaded again. Only a crash loses them."""
        if self._direct is None or self._direct.data is not None:
            return
        try:
            self._save_direct_pages()
            self.db.commit()
        except (ArchiveError, OSError, sqlite3.Error):
            pass  # The pages are downloaded again; the error that stopped the episode is the one to report.

    def publishes_here(self):
        return self._direct is None

    def transform_page(self, source, episode, page, data):
        from .image_profiles import compress_image
        key = (source, episode.series_id, episode.id)
        result = compress_image(data, self._episode_profiles.get(key, self.storage_policy['image_profile']))
        if result != data:
            self._originals.setdefault(key, {})[page.order] = {'sha256':digest(data),'size':len(data)}
            self._recompressed.add(key)
            self.cache_image(result)
        if self._streaming_episode is not None:
            self._streaming_episode.add_page(page, result)
        return result

    def archive(self, manifest, adapter, refresh=False, progress=print, download_workers=1):
        from .network import bind_adapter
        bind_adapter(adapter,self.root)
        for episode in manifest.episodes:
            # Originals are counted again from this attempt's pages, never added to an earlier attempt's.
            key = (manifest.series.source, episode.series_id, episode.id)
            self._originals.pop(key, None); self._recompressed.discard(key)
        from .cloud import settings
        from .models import Manifest
        from .cloud_stream import StreamingEpisode
        from .cloud import direct_target
        cloud = settings(self.root)
        if not manifest.episodes or not cloud or not any(t['enabled'] for t in cloud['targets'].values()):
            return super().archive(manifest, adapter, refresh, progress, download_workers)
        folder = self.series_folder(manifest.series)
        direct = direct_target(self.root) is not None
        # Publish series metadata and finish/offload each episode before the
        # next one, bounding cloud staging by an episode rather than a series.
        for episode in sorted(manifest.episodes, key=lambda e: e.order):
            path = self.episode_path(manifest.series, episode, folder)
            key = (manifest.series.source, episode.series_id, episode.id)
            stream = None
            if direct and episode.access_expires_at is not None:
                # Access that runs out may not let a lost episode be downloaded again: it is saved here first, as
                # a too-big one is, uploaded, and removed once the upload is checked.
                pass
            elif (direct and episode.content_type == 'comic' and self._episode_formats.get(key, 'cbz') == 'cbz'
                    and not path.exists() and not self.episode_is_offloaded(manifest.series, episode, path)):
                # B2 alone takes new episodes: hold this one in memory and send it straight there.
                self._direct = DirectEpisode(path)
            else:
                stream = StreamingEpisode.create(self, manifest.series, episode, path)
            self._streaming_episode = stream
            try:
                super().archive(Manifest(manifest.series, (episode,)), adapter, refresh, progress, download_workers)
            except BaseException:
                self._keep_direct_pages()
                try:
                    self._finish_upload()
                except (ArchiveError, OSError):
                    pass  # Recorded as failed, for the retry button; the first error is the one to report.
                raise
            finally:
                self._streaming_episode = None
                self._direct = None
                self._direct_ready.clear()
                if stream is not None: stream.close()
        self._finish_upload()
        return folder

    def completed_episode(self, path, metadata):
        if self.storage_policy['purge_completed_cache']:
            from .purge import evict_episode
            evict_episode(self, path, metadata)
        self._cloud_pending.add(path)
        from .cloud import queue_episode
        queue_episode(self.root, path)

    def episode_is_offloaded(self, series, episode, path):
        from .cloud import offloaded
        if path.is_file() or not offloaded(self.root, path):return False
        from .archive import sidecar_path
        metadata = self.read_metadata(sidecar_path(path))
        return (metadata.get('source') == series.source and metadata.get('source_episode_id') == episode.id
                and metadata.get('series_id') == self.series_export_key(series.source,series.id,series.platform_id)
                and metadata.get('title') == episode.title and metadata.get('episode_number') == episode.order)

    def publish_metadata(self, path, metadata):
        series = path.name == 'series.json'
        if not series: names=self._series_sidecars(path.parent)
        key=(self.series_export_key(metadata['source'],metadata.get('library_series_id',metadata['source_series_id']),
                                   metadata['source_series_id'] if 'library_series_id' in metadata else None)
             if series else None)
        export_id = (self.export_id(metadata['source'], key)
                     if series else metadata['export_id'])
        value = public_metadata(metadata, export_id, series=series)
        private = self.provenance / (export_id + '.json')
        atomic_json(private, provenance_record(self.root, path, value, metadata))
        private.chmod(0o600)
        atomic_json(path, value)
        if not series: names.add(path.name)
        if not series and self._direct is not None and self._direct.data is not None:
            # Held in memory, not saved here: it uploads once the series files are written.
            self._direct_ready.append((self._direct.archive, self._direct.data))
            return
        if not series and self.storage_policy['purge_completed_cache']:
            from .purge import evict_episode
            evict_episode(self, archive_from_sidecar(path, metadata), metadata)
        if not series:
            self._cloud_pending.add(archive_from_sidecar(path, metadata))
            from .cloud import queue_episode
            queue_episode(self.root, archive_from_sidecar(path, metadata))

    def publish_comic(self, path, entries, metadata, check=None):
        if callable(check): check()
        if self._direct is not None:
            import io
            from collections import Counter
            direct = self._direct
            # Each page is let go once its last use is in the CBZ, so the episode is held once while it is built.
            uses = Counter(page['sha256'] for page in comic_pages(metadata))
            def open_page(page):
                # A page already cached here from an earlier attempt is read from the cache.
                uses[page['sha256']] -= 1
                data = direct.pages.get(page['sha256']) if uses[page['sha256']] else direct.pages.pop(page['sha256'], None)
                return io.BytesIO(data) if data is not None else (self.cache / page['sha256']).open('rb')
            # Built from memory in a moment, after the check above: a pause is taken before it, while the pages
            # can still be kept (_keep_direct_pages), not partway through, once some are let go.
            direct.data = cbz_bytes(metadata, open_page)
            direct.pages.clear()
        elif self._streaming_episode is not None:
            self._streaming_episode.finish(metadata, check)
        else:
            writer = write_compressed_comic if metadata['format'] == 'tar.zst' else write_cbz
            writer(path, metadata, lambda page: (self.cache / page['sha256']).open('rb'), check)

    def novel_entries(self, series, episode, modified):
        from .novels import epub_entries
        key=self.series_export_key(series.source,series.id,series.platform_id)
        return epub_entries(series, episode, modified, export_id=self.export_id(series.source, key, episode.id))
