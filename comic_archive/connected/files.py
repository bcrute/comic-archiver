"""Selected Linux library custody. Private paths never enter the wire projection.

No site adapter, browser worker, source retrieval or management registry is used.
The caller still owes actual peer/share/lease authorization before each read.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import struct
import uuid
import zipfile

ARCHIVE_MAX = 64 * 1024**2
ENTRY_MAX = 32 * 1024**2
EXPANDED_MAX = 256 * 1024**2
METADATA_MAX = 1024**2
BOOKS_MAX = 10000
CATALOG_MAX = 8 * 1024**2
FORMAT = 'comic-archiver.connected-index/1'


class SourceError(Exception):
    """Safe protocol code only; never include an OSError, filename or path."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(value, code='integrity_failed'):
    if not value:
        raise SourceError(code)


def _parts(value):
    _require(isinstance(value, str), 'not_found')
    try:
        _require(0 < len(value.encode('utf-8')) <= 4096, 'not_found')
    except UnicodeError:
        raise SourceError('not_found') from None
    parts = PurePosixPath(value).parts
    _require(parts and not value.startswith('/') and '\\' not in value
             and all(part not in {'.', '..', ''} and not part.startswith('.') for part in parts)
             and '\x00' not in value and '/'.join(parts) == value and len(parts) <= 16, 'not_found')
    return parts


def _directory(path):
    # openat/O_NOFOLLOW on every component, not a resolve-then-open race.
    _require(os.name == 'posix' and hasattr(os, 'pread') and hasattr(os, 'O_NOFOLLOW'), 'unsupported_format')
    value = Path(path).absolute()
    _require('..' not in value.parts, 'not_found')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in value.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass(frozen=True)
class Stamp:
    device: int
    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def of(cls, fd, maximum):
        value = os.fstat(fd)
        _require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1 and 0 < value.st_size <= maximum)
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def encode(self):
        return json.dumps([self.device, self.inode, self.size, self.modified, self.changed], separators=(',', ':'))


class SharedRoot:
    def __init__(self, path):
        self.path = Path(path).absolute()
        try:
            self.fd = _directory(self.path)
            value = os.fstat(self.fd)
            self.identity = (value.st_dev, value.st_ino)
        except OSError:
            raise SourceError('host_offline') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def current(self):
        _require(self.fd is not None, 'host_offline')
        try:
            fd = _directory(self.path)
            try:
                value = os.fstat(fd)
                _require((value.st_dev, value.st_ino) == self.identity, 'host_offline')
            finally:
                os.close(fd)
        except OSError:
            raise SourceError('host_offline') from None

    def open(self, relative, maximum=ARCHIVE_MAX):
        parts = _parts(relative)
        self.current()
        directory = os.dup(self.fd)
        fd = None
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            stamp = Stamp.of(fd, maximum)
            return Pinned(self, relative, fd, stamp, maximum)
        except (OSError, SourceError):
            if fd is not None:
                os.close(fd)
            raise SourceError('not_found') from None
        finally:
            os.close(directory)


class Pinned:
    def __init__(self, root, relative, fd, stamp, maximum):
        self.root, self.relative, self.fd = root, relative, fd
        self.stamp, self.maximum = stamp, maximum

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def current(self):
        _require(self.fd is not None, 'cancelled')
        self.root.current()
        try:
            _require(Stamp.of(self.fd, self.maximum) == self.stamp, 'version_changed')
            # A replaced pathname cannot keep serving a stale pinned edition.
            other = self.root.open(self.relative, self.maximum)
        except (OSError, SourceError):
            raise SourceError('version_changed') from None
        try:
            _require(other.stamp == self.stamp, 'version_changed')
        finally:
            other.close()

    def chunk(self, offset, length):
        _require(type(offset) is int and type(length) is int and 0 <= offset < self.stamp.size
                 and 0 < length <= 16344 and offset + length <= self.stamp.size, 'resource_limit')
        self.current()
        data = os.pread(self.fd, length, offset)
        _require(len(data) == length, 'version_changed')
        self.current()
        return data

    def sha256(self):
        self.current()
        digest = hashlib.sha256()
        offset = 0
        while offset < self.stamp.size:
            data = os.pread(self.fd, min(1024**2, self.stamp.size - offset), offset)
            _require(data, 'version_changed')
            digest.update(data)
            offset += len(data)
        self.current()
        return digest.hexdigest()


def _object(root, relative):
    source = root.open(relative, METADATA_MAX)
    try:
        source.current()
        data = os.pread(source.fd, source.stamp.size, 0)
        source.current()
        value = json.loads(data)
        _require(isinstance(value, dict))
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError('integrity_failed') from None
    finally:
        source.close()


def _label(value):
    _require(isinstance(value, str) and value.strip())
    try:
        raw = value.strip().encode('utf-8')
    except UnicodeError:
        raise SourceError('integrity_failed') from None
    return raw[:512].decode('utf-8', errors='ignore')


def _central_bound(source):
    # Bound zipfile's central-directory allocation BEFORE constructing ZipFile.
    # Ordinary and redundant-local-ZIP64 exports have normal 32-bit EOCD fields.
    size = source.stamp.size
    tail = os.pread(source.fd, min(size, 65557), max(0, size - 65557))
    offset = tail.rfind(b'PK\x05\x06')
    _require(offset >= 0 and len(tail) - offset >= 22)
    _, disk, start_disk, local_count, total_count, central_size, central_offset, comment = struct.unpack('<4s4H2IH', tail[offset:offset + 22])
    _require(disk == 0 and start_disk == 0 and 1 <= total_count <= 1000 and local_count == total_count
             and central_size <= METADATA_MAX and central_offset + central_size <= size - len(tail) + offset
             and offset + 22 + comment == len(tail))


def _completed(source, metadata):
    _require(metadata.get('schema_version') == 2 and metadata.get('format') == 'cbz'
             and metadata.get('content_type') == 'comic', 'unsupported_format')
    try:
        _require(str(uuid.UUID(metadata['export_id'])) == metadata['export_id'])
        pages = metadata['content_hashes']
        _require(isinstance(pages, list) and type(metadata['page_count']) is int
                 and 1 <= len(pages) <= 1000 and metadata['page_count'] == len(pages))
        source.current()
        # Bounded compressed input and member count/expanded declarations are
        # checked before any member is decompressed. Pixel decode stays in reader.
        _central_bound(source)
        with os.fdopen(os.dup(source.fd), 'rb') as stream, zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            _require(len(members) == len(pages) and len({item.filename for item in members}) == len(members))
            total = 0
            for member, page in zip(members, pages):
                _require(isinstance(page, dict) and member.filename == page['file']
                         and not member.is_dir() and not member.flag_bits & 1
                         and member.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                         and type(page['size']) is int and member.file_size == page['size']
                         and 1 <= member.file_size <= ENTRY_MAX)
                _require(len(PurePosixPath(member.filename).parts) == 1
                         and member.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')), 'unsupported_format')
                total += member.file_size
                _require(total <= EXPANDED_MAX, 'resource_limit')
            for member, page in zip(members, pages):
                digest, size = hashlib.sha256(), 0
                with archive.open(member) as content:
                    while data := content.read(1024**2):
                        size += len(data)
                        _require(size <= member.file_size)
                        digest.update(data)
                _require(size == member.file_size and digest.hexdigest() == page['sha256'])
        source.current()
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, zipfile.BadZipFile):
        raise SourceError('integrity_failed') from None


class Index:
    """Local identities, not an authenticated peer/session or RPC API.

    Keep the SQLite connection and its file operations on one owned worker;
    this object is not a cross-thread cancellation or process-lifecycle owner.
    """

    def __init__(self, path, root):
        self.root = root
        self.path = Path(path).absolute()
        _require(not self.path.is_relative_to(root.path), 'resource_limit')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = _directory(self.path.parent)
        try:
            parent = os.fstat(directory)
            _require(parent.st_uid == os.geteuid() and not parent.st_mode & 0o077)
            fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            try:
                before = os.fstat(fd)
                _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                         and before.st_uid == os.geteuid() and not before.st_mode & 0o077)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=1)
            after = os.stat(self.path, follow_symlinks=False)
            _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
            self.db.row_factory = sqlite3.Row
            if before.st_size == 0:
                self.db.executescript('''CREATE TABLE IF NOT EXISTS metadata(format TEXT NOT NULL, root TEXT NOT NULL,
                    share_id TEXT NOT NULL, revision TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS series(relative TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE);
                    CREATE TABLE IF NOT EXISTS books(relative TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    version TEXT NOT NULL, sha256 TEXT NOT NULL, stamp TEXT NOT NULL, title TEXT NOT NULL,
                    series_id TEXT NOT NULL, series_title TEXT NOT NULL, chapter_order INTEGER);''')
                self.db.execute('PRAGMA user_version=1')
            _require(self.db.execute('PRAGMA user_version').fetchone()[0] == 1)
            rows = self.db.execute('SELECT * FROM metadata LIMIT 2').fetchall()
            identity = json.dumps(root.identity)
            if not rows:
                _require(before.st_size == 0)
                self.db.execute('INSERT INTO metadata VALUES(?,?,?,?)', (FORMAT, identity, str(uuid.uuid4()), str(uuid.uuid4())))
            else:
                _require(len(rows) == 1 and rows[0]['format'] == FORMAT and rows[0]['root'] == identity)
        except (OSError, sqlite3.Error, SourceError):
            if hasattr(self, 'db'):
                self.db.close()
            raise SourceError('integrity_failed') from None
        finally:
            os.close(directory)

    def close(self):
        self.db.close()

    @property
    def share_id(self):
        return self.db.execute('SELECT share_id FROM metadata').fetchone()[0]

    def register_completed(self, relative, series_title):
        _require(isinstance(relative, str) and relative.lower().endswith('.cbz'), 'unsupported_format')
        source = self.root.open(relative)
        try:
            metadata = _object(self.root, str(PurePosixPath(relative).with_suffix('.json')))
            _completed(source, metadata)
            digest = source.sha256()
            title, series_title = _label(metadata.get('title')), _label(series_title)
            chapter = metadata.get('episode_number')
            if type(chapter) is not int or not 0 <= chapter < 2**31:
                chapter = None
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = self.db.execute('SELECT * FROM books WHERE relative=?', (relative,)).fetchone()
                moved = None
                if prior is None:
                    candidates = self.db.execute('SELECT * FROM books WHERE sha256=? LIMIT ?', (digest, BOOKS_MAX + 1)).fetchall()
                    _require(len(candidates) <= BOOKS_MAX, 'resource_limit')
                    for candidate in candidates:
                        if json.loads(candidate['stamp'])[:2] != [source.stamp.device, source.stamp.inode]:
                            continue
                        try:
                            old = self.root.open(candidate['relative'])
                        except SourceError as error:
                            if error.code != 'not_found':
                                raise
                            _require(moved is None, 'integrity_failed')
                            moved = candidate
                        else:
                            old.close()
                    if moved:
                        prior = moved
                count = self.db.execute('SELECT count(*) FROM books').fetchone()[0]
                _require(prior is not None or count < BOOKS_MAX, 'resource_limit')
                parent = str(PurePosixPath(relative).parent)
                self.db.execute('INSERT OR IGNORE INTO series VALUES(?,?)', (parent, str(uuid.uuid4())))
                series_id = self.db.execute('SELECT id FROM series WHERE relative=?', (parent,)).fetchone()[0]
                book_id = prior['id'] if prior else str(uuid.uuid4())
                version = prior['version'] if prior and prior['sha256'] == digest else str(uuid.uuid4())
                if moved:
                    self.db.execute('DELETE FROM books WHERE relative=?', (moved['relative'],))
                record = dict(relative=relative, id=book_id, version=version, sha256=digest, stamp=source.stamp.encode(),
                              title=title, series_id=series_id, series_title=series_title, chapter_order=chapter)
                self.db.execute('INSERT OR REPLACE INTO books VALUES(?,?,?,?,?,?,?,?,?)', tuple(record.values()))
                if prior is None or self._public(prior) != self._public(record):
                    self.db.execute('UPDATE metadata SET revision=?', (str(uuid.uuid4()),))
                source.current()
                self.db.execute('COMMIT')
                return self.describe(book_id, version)
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        finally:
            source.close()

    def _row(self, book_id, version):
        self.root.current()
        row = self.db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        _require(row is not None, 'not_found')
        _require(row['version'] == version, 'version_changed')
        return row

    @staticmethod
    def _public(row):
        return dict(book_id=row['id'], content_version=row['version'], series_id=row['series_id'],
                    title=row['title'], series_title=row['series_title'], chapter_order=row['chapter_order'],
                    byte_length=json.loads(row['stamp'])[2], format='cbz', sha256=row['sha256'])

    def describe(self, book_id, version):
        source = self.open_original(book_id, version)
        try:
            source.current()
            return self._public(self._row(book_id, version))
        finally:
            source.close()

    def snapshot(self):
        self.root.current()
        rows = self.db.execute('SELECT * FROM books ORDER BY series_id, chapter_order, id LIMIT ?', (BOOKS_MAX + 1,)).fetchall()
        _require(len(rows) <= BOOKS_MAX, 'resource_limit')
        entries = [self._public(row) for row in rows]
        _require(len(json.dumps(entries, ensure_ascii=False).encode()) <= CATALOG_MAX, 'resource_limit')
        revision = self.db.execute('SELECT revision FROM metadata').fetchone()[0]
        return revision, entries

    def open_original(self, book_id, version):
        row = self._row(book_id, version)
        source = self.root.open(row['relative'])
        if source.stamp.encode() != row['stamp']:
            source.close()
            raise SourceError('version_changed')
        return source
