"""Adopt a captured portable workspace into a new, held, private working copy.

Android owns the document grant, complete listing and stable capture. This module
only consumes that private snapshot, normalized to Library / <Site>/Library.
It never opens a provider session, imports a queue or acts on a cloud connection.
The destination must be an empty, unselected staging directory. Failed output
stays unselectable; the coordinator can discard it and retry the retained input.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
from urllib.parse import quote
import uuid

from . import cloud, cloud_history, sites
from .archive import (archive_from_sidecar, atomic_json, directory,
                      sidecar_path, verify_episode_contents, verify_series)
from .discovery import observed_id
from .inventory import Inventory
from .models import ArchiveError, Series, canonical_url, identifier
from .osdep import sync_directory, sync_file
from .publication import (EPISODE_FIELDS, SERIES_FIELDS, PublicationStore, import_metadata,
                          metadata_hash, public_metadata, valid_export_id)

MAX_FILES = 100_000
MAX_JSON = 16 * 1024**2
MAX_DATABASE = 128 * 1024**2
RECEIPT = '.working-import.json'
LOCATION_MARKER = '.working-library.json'
HASH = re.compile(r'[a-f0-9]{64}')
MESSAGES = {
    'invalid': 'This library snapshot could not be verified. The previous library is retained.',
    'incomplete': 'Some saved records are missing their files or provenance. The previous library is retained.',
    'changed': 'The source library changed during import. Pause it and choose the folder again.',
    'destination': 'The private library copy is not empty. Resume or cancel that copy first.',
    'cancelled': 'Library copying was canceled. The previous library is retained.',
}
SERIES_PRIVATE = {'schema_version', 'source', 'source_series_id', 'library_series_id',
                  'source_series_id_kind', 'source_series_slug', 'source_url', 'description',
                  'credits', 'cover_url', 'metadata_source', 'metadata_imported_at', 'imported_at', *SERIES_FIELDS}
EPISODE_PRIVATE = {'schema_version', 'source', 'source_episode_id', 'series_id', 'source_series_id',
                   'source_url', 'access_state', 'export_id', 'access_expires_at', 'cache_series_id',
                   'original_content_hashes', *EPISODE_FIELDS}


class LibraryImportError(ArchiveError):
    def __init__(self, code='invalid'):
        self.code = code
        super().__init__(MESSAGES[code])


@dataclass(frozen=True)
class ImportedSite:
    key: str
    series: int
    episodes: int
    cloud_only: int
    account_bound: bool


@dataclass(frozen=True)
class ImportedLibrary:
    digest: str
    sites: tuple[ImportedSite, ...]


def _safe(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise LibraryImportError()
    return path


def _root(path):
    _safe(Path(path).absolute())
    return _safe(Path(os.path.abspath(path)))


def _children(path):
    _safe(path)
    values = list(islice(path.iterdir(), MAX_FILES + 1))
    if len(values) > MAX_FILES:
        raise LibraryImportError()
    return sorted(values)


def _json(path, maximum=MAX_JSON):
    _safe(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise LibraryImportError('incomplete')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise LibraryImportError()
    return value


def _poll(check):
    if check is not None:
        check()


def _hash(path, check):
    _safe(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise LibraryImportError()
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _poll(check)
            value.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise LibraryImportError('changed')
    return {'size': after.st_size, 'sha256': value.hexdigest()}


def _inputs(snapshot, check):
    """Whitelist inputs; sessions, caches, settings links and jobs are not data to adopt."""
    result = {}

    def add(path, maximum=None):
        _poll(check)
        _safe(path)
        if maximum is not None and path.stat().st_size > maximum:
            raise LibraryImportError()
        if len(result) >= MAX_FILES:
            raise LibraryImportError()
        result[path.relative_to(snapshot).as_posix()] = _hash(path, check)

    found = False
    for site in sites.SITES:
        root = sites.folder(snapshot, site) / 'Library'
        _safe(root)
        if not root.exists():
            continue
        found = True
        result[root.relative_to(snapshot).as_posix()] = {'directory': True}
        for folder in _children(root):
            if folder.name.startswith('.'):
                continue
            _safe(folder)
            if not folder.is_dir():
                raise LibraryImportError()
            if len(result) >= MAX_FILES:
                raise LibraryImportError()
            result[folder.relative_to(snapshot).as_posix()] = {'directory': True}
            for path in _children(folder):
                if (path.name.endswith(('.cbz', '.epub', '.tar.zst', '.json'))
                        or path.name in {'cover.jpg', 'cover.png', 'cover.gif', 'cover.webp'}):
                    add(path, MAX_JSON if path.suffix == '.json' else None)
                elif path.name.endswith(('.tmp', '.part')):
                    raise LibraryImportError('changed')
        state = root / '.archive-state'
        for name in ('inventory.sqlite3', 'inventory.sqlite3-wal', 'inventory.sqlite3-shm', 'inventory.sqlite3-journal'):
            path = _safe(state / name)
            if path.exists():
                add(path, MAX_DATABASE)
        provenance = _safe(state / 'provenance')
        if provenance.exists():
            for path in _children(provenance):
                if path.suffix != '.json' or not valid_export_id(path.stem):
                    raise LibraryImportError()
                add(path, MAX_JSON)
        reading = _safe(state / 'reading')
        if reading.exists():
            for path in _children(reading):
                if path.name == 'writer.lock' or path.name.endswith('.json.tmp'):
                    continue
                if not re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
                    raise LibraryImportError()
                add(path, 64 * 1024)
        targets = _safe(state / 'cloud/targets.json')
        if targets.exists():
            add(targets, 1024**2)
        for provider in cloud.PROVIDERS:
            outbox = _safe(state / 'cloud/outbox' / provider)
            if outbox.exists():
                for path in _children(outbox):
                    if path.suffix != '.json' or not valid_export_id(path.stem):
                        raise LibraryImportError()
                    add(path, 1024**2)
            history = _safe(state / 'cloud/history' / provider)
            if history.exists():
                for folder in _children(history):
                    if not valid_export_id(folder.name):
                        raise LibraryImportError()
                    for path in _children(folder):
                        if path.suffix != '.json':
                            raise LibraryImportError()
                        add(path, 1024**2)
    if not found:
        raise LibraryImportError('incomplete')
    return result


def _account(root, scratch, check):
    """Read a bounded SQLite snapshot in disposable scratch, never install its schema."""
    source = root / '.archive-state/inventory.sqlite3'
    if not source.exists():
        if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise LibraryImportError('incomplete')
        return {}, []
    local = scratch / 'inventory.sqlite3'
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = Path(str(source) + suffix)
        if path.exists():
            _copy(path, Path(str(local) + suffix), check)
    with closing(sqlite3.connect(f"file:{quote(str(local), safe='/')}?mode=ro", uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            return int(calls > 5000)

        db.set_progress_handler(budget, 1000)
        if db.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
            raise LibraryImportError()
        tables = dict(db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('settings','episodes')"))
        if tables != {'settings': 'table', 'episodes': 'table'}:
            raise LibraryImportError()

        def authorize(action, table, column, database, origin):
            allowed = action == sqlite3.SQLITE_SELECT or (
                action == sqlite3.SQLITE_READ and table in {'settings', 'episodes'} and database == 'main' and origin is None)
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

        db.set_authorizer(authorize)
        rows = db.execute("SELECT key,value FROM settings WHERE key IN ('account_salt','account_binding')").fetchmany(3)
        binding = dict(rows)
        if len(rows) != len(binding) or binding and (set(binding) != {'account_salt', 'account_binding'}
                or not isinstance(binding.get('account_salt'), str)
                or not re.fullmatch(r'[a-fA-F0-9]{64}', binding['account_salt'])
                or not isinstance(binding.get('account_binding'), str) or not HASH.fullmatch(binding['account_binding'])):
            raise LibraryImportError()
        archived = db.execute("SELECT id,series_id,archive_path FROM episodes WHERE status='archived'").fetchmany(MAX_FILES + 1)
        if len(archived) > MAX_FILES:
            raise LibraryImportError()
        _poll(check)
        return binding, archived


def _copy(source, target, check):
    _safe(source); _safe(target)
    directory(target.parent).chmod(0o700)
    with source.open('rb') as reader, target.open('xb') as writer:
        os.chmod(target, 0o600)
        while data := reader.read(1024 * 1024):
            _poll(check)
            writer.write(data)
        writer.flush()
        os.fsync(writer.fileno())


def _metadata(sidecar, site, *, series=False):
    public = _json(sidecar)
    metadata = import_metadata(sidecar)
    if metadata.get('source') != site.key:
        raise LibraryImportError()
    selected = SERIES_PRIVATE if series else EPISODE_PRIVATE
    value = {k: v for k, v in metadata.items() if k in selected}
    for key in ('source_url', 'cover_url'):
        if key in value:
            value[key] = canonical_url(value[key])
    if type(public.get('schema_version')) is not int or public['schema_version'] not in (1, 2):
        raise LibraryImportError()
    if public.get('schema_version') == 2:
        export_id = public['export_id']
        if not series and metadata.get('export_id') not in (None, export_id):
            raise LibraryImportError()
    else:
        export_id = str(uuid.uuid4())
    if not series:
        value['export_id'] = export_id
        value.setdefault('format', 'cbz')
        value.setdefault('content_type', 'novel' if value['format'] == 'epub' else 'comic')
        if value.get('cache_series_id') is not None:
            identifier(value['cache_series_id'])
        if value.get('access_expires_at') is not None:
            from .expiry import expiration
            expiration(value['access_expires_at'])
        originals = value.get('original_content_hashes', [])
        if (not isinstance(originals, list) or len(originals) > 10000
                or any(not isinstance(item, dict) or set(item) != {'size', 'sha256'}
                       or type(item['size']) is not int or not 0 < item['size'] <= 100 * 1024**2
                       or not isinstance(item['sha256'], str) or not HASH.fullmatch(item['sha256']) for item in originals)):
            raise LibraryImportError()
    return public, value, export_id


def _series(value, site):
    canonical = value.get('source_series_id')
    key = value.get('library_series_id', canonical)
    creators = value.get('creators', [])
    if not observed_id(canonical) or not isinstance(creators, list):
        raise LibraryImportError()
    identifier(key)
    return Series(key, value.get('title'), tuple(creators),
                  value.get('description'), value.get('source_url'), site.key,
                  canonical if canonical != key else None, value.get('source_series_slug'), value.get('credits'))


def _episode(value, series):
    key = value.get('source_episode_id')
    canonical = value.get('source_series_id', value.get('series_id'))
    order, title = value.get('episode_number'), value.get('title')
    if (not observed_id(key) or canonical != (series.platform_id or series.id)
            or value.get('series_id') != series.id or type(order) is not int or order < 1
            or not isinstance(title, str) or not title.strip() or len(title) > 500
            or value.get('format') not in {'cbz', 'tar.zst', 'epub'}
            or value.get('content_type') != ('novel' if value['format'] == 'epub' else 'comic')):
        raise LibraryImportError()
    return SimpleNamespace(id=key, series_id=series.id, title=title, order=order, content_type=value['content_type'])


class _CheckedReader:
    def __init__(self, stream, check):
        self.stream, self.check = stream, check

    def read(self, *args):
        _poll(self.check)
        return self.stream.read(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _verify(path, metadata, check):
    with path.open('rb') as stream:
        problems = verify_episode_contents(_CheckedReader(stream, check), metadata)
        _poll(check)
        if problems:
            raise LibraryImportError()


def _receipt_records(root, inputs, public, archive, library_id, check):
    if public.get('schema_version') != 2 or library_id is None:
        return []
    records = []
    episode_id = public['export_id']
    for provider in cloud.PROVIDERS:
        paths = [root / '.archive-state/cloud/outbox' / provider / (episode_id + '.json')]
        paths += sorted((root / '.archive-state/cloud/history' / provider / episode_id).glob('*.json'))
        for path in paths:
            _poll(check)
            if not path.exists():
                continue
            value = _json(path, 1024**2)
            if not cloud_history.checked(root, archive, public, library_id, value):
                continue  # Retry entries and older versions are not completed copies of this sidecar.
            series = _json(archive.parent / 'series.json')
            files = value['files']
            names = ['episode.' + public['format'], 'episode.json', 'series.json']
            expected_paths = [archive, sidecar_path(archive), archive.parent / 'series.json']
            # Series descriptions and covers may have changed since the upload.
            # Preserve its historical bundle, not a claim about today's cover.
            if len(files) == 4 and files[3].get('name') in {'cover.jpg', 'cover.png', 'cover.gif', 'cover.webp'}:
                names.append(files[3]['name']); expected_paths.append(archive.parent / files[3]['name'])
            if (len(files) != len(names) or value['series_id'] != series.get('export_id')
                    or value.get('remote_root') != cloud.CLOUD_ROOT
                    or type(value.get('eviction_requested', False)) is not bool
                    or any(item.get('name') != name or item.get('path') != p.relative_to(root).as_posix()
                           for item, name, p in zip(files, names, expected_paths, strict=True))):
                raise LibraryImportError()
            original = inputs.get(archive.relative_to(root).as_posix())
            if original and files[0]['sha256'] != original['sha256']:
                raise LibraryImportError()
            records.append((provider, {k: value[k] for k in (
                'state', 'library_id', 'target_id', 'episode_id', 'series_id', 'version', 'archive',
                'verified_at', 'remote_root', 'eviction_requested') if k in value} | {
                    'files': [{k: item[k] for k in ('name', 'path', 'sha256')} for item in files]}))
    return records


def _publish_metadata(root, target, source, original, value, export_id, check, *, series=False):
    if target.exists():
        raise LibraryImportError()
    public = public_metadata(value, export_id, series=series)
    if original.get('schema_version') == 2:
        # Retain exact public bytes: cloud receipts hash the sidecar, including whitespace.
        _copy(source, target, check)
    else:
        atomic_json(target, public)
    private = directory(root / '.archive-state/provenance') / (export_id + '.json')
    private.parent.chmod(0o700)
    atomic_json(private, {'schema_version': 1, 'sidecar': target.relative_to(root).as_posix(),
                          'public_hash': metadata_hash(public), 'metadata': value})
    private.chmod(0o600)


def _site(source, target, site, inputs, scratch, check):
    binding, archived = _account(source, scratch, check)
    relative_inputs = {Path(k).relative_to(source).as_posix(): v for k, v in inputs.items()
                       if Path(k).is_relative_to(source)}
    own = source / '.archive-state/cloud/targets.json'
    library_id = None
    if own.exists():
        value = _json(own, 1024**2)
        if type(value.get('schema_version')) is not int or value['schema_version'] != 1 or not valid_export_id(value.get('library_id')):
            raise LibraryImportError()
        library_id = value['library_id']
        cloud._write(target / '.archive-state/cloud/targets.json',
                     {'schema_version': 1, 'library_id': library_id, 'targets': {}})
    elif any(relative.startswith(('.archive-state/cloud/history/', '.archive-state/cloud/outbox/'))
             and _json(source / relative).get('state') == 'verified' for relative in relative_inputs):
        raise LibraryImportError('incomplete')
    completed, seen_series, seen_episodes, seen_exports = {}, set(), set(), set()
    series_count, cloud_only = 0, 0
    with closing(PublicationStore(target)) as store, Inventory(target) as inventory:
        with inventory.db:
            inventory.db.executemany('INSERT INTO settings VALUES (?,?)', sorted(binding.items()))
        for folder in _children(source):
            if folder.name.startswith('.'):
                continue
            _poll(check)
            if verify_series(folder / 'series.json'):
                raise LibraryImportError('incomplete')
            public, metadata, export_id = _metadata(folder / 'series.json', site, series=True)
            series = _series(metadata, site)
            canonical = series.platform_id or series.id
            if canonical in seen_series or export_id in seen_exports:
                raise LibraryImportError()
            seen_series.add(canonical); seen_exports.add(export_id); series_count += 1
            store.db.execute('INSERT INTO export_series_keys VALUES (?,?,?)', (site.key, canonical, series.id))
            store.db.execute('INSERT INTO export_ids VALUES (?,?,?,?)', (site.key, series.id, '', export_id))
            store.db.commit()
            output = store.series_folder(series)
            _publish_metadata(target, output / 'series.json', folder / 'series.json', public,
                              metadata, export_id, check, series=True)
            if metadata.get('cover_file'):
                _copy(folder / metadata['cover_file'], output / metadata['cover_file'], check)
            expected = {'series.json', metadata.get('cover_file')}
            for sidecar in _children(folder):
                if sidecar.name == 'series.json' or sidecar.suffix != '.json':
                    continue
                public, metadata, export_id = _metadata(sidecar, site)
                episode = _episode(metadata, series)
                if episode.id in seen_episodes or export_id in seen_exports:
                    raise LibraryImportError()
                seen_episodes.add(episode.id); seen_exports.add(export_id)
                archive = archive_from_sidecar(sidecar, metadata)
                expected.update((sidecar.name, archive.name))
                receipts = _receipt_records(source, relative_inputs, public, archive, library_id, check)
                local = archive.is_file()
                if local:
                    _verify(archive, metadata, check)
                elif not any(record.get('eviction_requested') is True for _, record in receipts):
                    raise LibraryImportError('incomplete')
                else:
                    cloud_only += 1
                store.db.execute('INSERT INTO export_ids VALUES (?,?,?,?)', (site.key, series.id, episode.id, export_id))
                store.db.commit()
                destination = store.episode_path(series, episode, output)
                if metadata['format'] == 'tar.zst':
                    destination = destination.with_suffix('.tar.zst')
                    store.db.execute('UPDATE episode_paths SET path=? WHERE source=? AND series_id=? AND episode_id=?',
                                     (destination.relative_to(target).as_posix(), site.key, series.id, episode.id))
                    store.db.commit()
                if local:
                    _copy(archive, destination, check)
                _publish_metadata(target, sidecar_path(destination), sidecar, public, metadata, export_id, check)
                for provider, record in receipts:
                    rebased = record | {'archive': destination.relative_to(target).as_posix()}
                    rebased['files'] = [item | {'path': (output / (destination.name if i == 0 else
                        sidecar_path(destination).name if i == 1 else item['name'])).relative_to(target).as_posix()}
                        for i, item in enumerate(record['files'])]
                    cloud_history.retain(target, provider, rebased)
                inventory.observe(canonical, series.title, [{'id': episode.id, 'title': episode.title,
                    'order': episode.order, 'access_state': 'unavailable_or_unverified'}], 'snapshot_import')
                inventory.mark(episode.id, 'archived', path=destination)
                completed[archive.relative_to(source).as_posix()] = (episode.id, canonical)
            for path in _children(folder):
                if path.name not in expected:
                    raise LibraryImportError('incomplete')
            if verify_series(output / 'series.json'):
                raise LibraryImportError()
        for key, series_id, path in archived:
            if not isinstance(path, str) or completed.get(path) != (key, series_id):
                raise LibraryImportError('incomplete')
        inventory.reconcile(full=True)
        if inventory.status()['episodes_by_status'] != ({'archived': len(seen_episodes)} if seen_episodes else {}):
            raise LibraryImportError('incomplete')
    # Reading records are independent portable data, including future/temporarily
    # unreadable records. Preserve their exact bytes; never copy locks or partial writes.
    for relative in relative_inputs:
        if relative.startswith('.archive-state/reading/'):
            _copy(source / relative, target / relative, check)
    return ImportedSite(site.key, series_count, len(seen_episodes), cloud_only, bool(binding))


def _output_files(destination, check, *, location_marker=False):
    result = {}
    for folder, directories, files in os.walk(destination, followlinks=False):
        for name in directories:
            _safe(Path(folder) / name)
        for name in files:
            path = Path(folder) / name
            if path == destination / RECEIPT or location_marker and path == destination / LOCATION_MARKER:
                continue
            if len(result) >= MAX_FILES:
                raise LibraryImportError()
            result[path.relative_to(destination).as_posix()] = _hash(path, check)
    return dict(sorted(result.items()))


def prepare_snapshot(snapshot, destination, *, check=None):
    """Build and verify one complete working copy. Success does not activate it or start Go."""
    try:
        snapshot, destination = _root(snapshot), _root(destination)
        if snapshot == destination or snapshot in destination.parents or destination in snapshot.parents:
            raise LibraryImportError('destination')
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise LibraryImportError('destination')
        before = _inputs(snapshot, check)
        directory(destination).chmod(0o700)
        reports = []
        with tempfile.TemporaryDirectory(prefix='.library-import-', dir=destination.parent) as name:
            scratch = Path(name)
            for site in sites.SITES:
                source = sites.folder(snapshot, site) / 'Library'
                if source.exists():
                    temporary = scratch / site.key
                    temporary.mkdir(mode=0o700)
                    absolute_inputs = {str(snapshot / key): value for key, value in before.items()}
                    reports.append(_site(source, sites.folder(destination, site) / 'Library', site,
                                         absolute_inputs, temporary, check))
        if before != _inputs(snapshot, check):
            raise LibraryImportError('changed')
        files = _output_files(destination, check)
        source_digest = metadata_hash(before)
        site_reports = [asdict(report) for report in reports]
        digest = metadata_hash({'source_digest': source_digest, 'files': files, 'sites': site_reports})
        atomic_json(destination / RECEIPT, {'v': 1, 'digest': digest, 'files': files,
                                           'source_digest': source_digest, 'sites': site_reports})
        (destination / RECEIPT).chmod(0o600)
        for folder, _, _ in os.walk(destination, topdown=False):
            sync_directory(Path(folder))
        sync_file(destination / RECEIPT)
        return ImportedLibrary(digest, tuple(reports))
    except LibraryImportError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, ArchiveError):
        raise LibraryImportError() from None


def prepared_snapshot(destination, *, check=None, ticket=None, source_key=None):
    """Recover only an unchanged, completely prepared staging copy after a coordinator crash."""
    try:
        destination = _root(destination)
        value = _json(destination / RECEIPT, 64 * 1024**2)
        marker = _safe(destination / LOCATION_MARKER)
        if marker.exists():
            recorded = _json(marker, 4096)
            if (not isinstance(ticket, str) or not re.fullmatch(
                    r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', ticket)
                    or not isinstance(source_key, str) or not HASH.fullmatch(source_key)
                    or recorded != {'v': 1, 'id': ticket, 'source': source_key, 'digest': value.get('digest')}):
                raise LibraryImportError()
        if (set(value) != {'v', 'digest', 'files', 'sites', 'source_digest'} or type(value['v']) is not int or value['v'] != 1
                or not isinstance(value['digest'], str) or not HASH.fullmatch(value['digest'])
                or not isinstance(value['source_digest'], str) or not HASH.fullmatch(value['source_digest'])
                or not isinstance(value['files'], dict)
                or value['files'] != _output_files(destination, check, location_marker=marker.exists())
                or metadata_hash({k: value[k] for k in ('source_digest', 'files', 'sites')}) != value['digest']
                or not isinstance(value['sites'], list) or not value['sites'] or len(value['sites']) > len(sites.SITES)):
            raise LibraryImportError('incomplete')
        reports = []
        for report in value['sites']:
            if (not isinstance(report, dict) or set(report) != {'key', 'series', 'episodes', 'cloud_only', 'account_bound'}
                    or report['key'] not in sites.KEYS or any(r.key == report['key'] for r in reports)
                    or any(type(report[k]) is not int or report[k] < 0 for k in ('series', 'episodes', 'cloud_only'))
                    or report['cloud_only'] > report['episodes'] or type(report['account_bound']) is not bool):
                raise LibraryImportError()
            reports.append(ImportedSite(**report))
        return ImportedLibrary(value['digest'], tuple(reports))
    except LibraryImportError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise LibraryImportError() from None
