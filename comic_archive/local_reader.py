"""Offline reading of verified local output; archives and publication metadata stay read-only."""
from contextlib import contextmanager
import copy
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import threading
import zipfile

from . import osdep
from .archive import atomic_json, sidecar_path, verify_episode_contents
from .archive_paths import directory
from .models import ArchiveError

MAX_RECORD = 64 * 1024
MAX_MEMBERS = 20_000
MAX_ARCHIVE = 8 * 1024**3
MAX_RENDER = 64 * 1024**2
READING_DIRECTORY = '.archive-state/reading'
RECORD_NAME = re.compile(r'[a-f0-9]{64}\.json')
_writers = threading.RLock()


class ReadingError(ArchiveError):
    """Fixed public words; exception details and paths never enter the reader UI."""


class ReadingConflict(ReadingError):
    def __init__(self):
        super().__init__('Reading progress changed in another window. Reload saved progress before saving again.')


UNAVAILABLE = 'This saved file is missing, changed or incomplete. Refresh the library and choose it again.'
PROGRESS_UNAVAILABLE = 'Saved reading progress is unavailable. Reading still works; the existing record is retained.'
SAVE_FAILED = 'Reading progress could not be saved. Reading still works; try Reload saved progress.'
UNSUPPORTED = 'This archive exceeds the built-in reader’s supported limits. The original is unchanged; use Open instead.'


def _safe(path):
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ReadingError(UNAVAILABLE)
    return path


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


def _json(path, maximum):
    _safe(path)
    descriptor = os.open(path, os.O_RDONLY | osdep.NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError('Invalid record')
        raw = stream.read(maximum + 1)
        if len(raw) != info.st_size or _fact(os.fstat(stream.fileno())) != _fact(info):
            raise ValueError('Record changed')
    return json.loads(raw, object_pairs_hook=_pairs)


def _fact(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _position(value, units):
    if (not isinstance(value, dict) or set(value) != {'unit', 'offset'}
            or type(value['unit']) is not int or not 0 <= value['unit'] < units
            or type(value['offset']) is not int or not 0 <= value['offset'] <= 10_000):
        raise ValueError('Invalid reading position')
    return dict(value)


def fresh_record(content, kind, units):
    return {'v': 1, 'content_sha256': content, 'kind': kind, 'units': units, 'revision': 0,
            'position': {'unit': 0, 'offset': 0}, 'bookmarks': [],
            'preferences': {'fit': 'width', 'text_scale': 1.0}}


def checked_record(value, *, content=None, kind=None, units=None):
    """Optional fields default; unknown versions/fields are retained, never rewritten as v1."""
    if (not isinstance(value, dict) or set(value) - set(fresh_record('', '', 1))
            or type(value.get('v')) is not int or value['v'] != 1
            or not isinstance(value.get('content_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', value['content_sha256'])
            or value.get('kind') not in {'cbz', 'epub'}
            or type(value.get('units')) is not int or not 1 <= value['units'] <= MAX_MEMBERS):
        raise ValueError('Invalid reading record')
    for expected, field in ((content, 'content_sha256'), (kind, 'kind'), (units, 'units')):
        if expected is not None and value[field] != expected:
            raise ValueError('Reading content identity changed')
    record = fresh_record(value['content_sha256'], value['kind'], value['units']) | copy.deepcopy(value)
    if type(record['revision']) is not int or not 0 <= record['revision'] < 2**53 - 1:
        raise ValueError('Invalid reading revision')
    record['position'] = _position(record['position'], record['units'])
    bookmarks = record['bookmarks']
    if not isinstance(bookmarks, list) or len(bookmarks) > 100:
        raise ValueError('Too many bookmarks')
    record['bookmarks'] = [_position(position, record['units']) for position in bookmarks]
    if len({(p['unit'], p['offset']) for p in bookmarks}) != len(bookmarks):
        raise ValueError('Duplicate bookmarks')
    prefs = record['preferences']
    if not isinstance(prefs, dict) or set(prefs) - {'fit', 'text_scale'}:
        raise ValueError('Invalid reading preferences')
    prefs = {'fit': 'width', 'text_scale': 1.0} | prefs
    if (prefs['fit'] not in {'width', 'page'} or type(prefs['text_scale']) not in (int, float)
            or not .75 <= prefs['text_scale'] <= 2 or not math.isfinite(prefs['text_scale'])):
        raise ValueError('Invalid reading preferences')
    record['preferences'] = prefs
    return record


class ReadingStore:
    def __init__(self, library):
        self.library = Path(library).absolute()
        self.folder = self.library / READING_DIRECTORY

    def load(self, content, kind, units):
        expected = fresh_record(content, kind, units)
        checked_record(expected)
        try:
            _safe(self.folder)
            if not self.library.is_dir():
                raise ValueError('Library missing')
            path = self.folder / (content + '.json')
            try:
                value = _json(path, MAX_RECORD)
            except FileNotFoundError:
                return expected
            return checked_record(value, content=content, kind=kind, units=units)
        except (OSError, ValueError, TypeError, ReadingError):
            raise ReadingError(PROGRESS_UNAVAILABLE) from None

    def save(self, value, expected_revision):
        try:
            record = checked_record(value)
            if type(expected_revision) is not int or record['revision'] != expected_revision:
                raise ValueError('Invalid expected revision')
            with _writers:
                _safe(self.folder)
                if not self.library.is_dir():
                    raise ValueError('Library missing')
                directory(self.folder).chmod(0o700)
                descriptor = os.open(self.folder / 'writer.lock', os.O_CREAT | os.O_RDWR | osdep.NOFOLLOW, 0o600)
                locked = False
                try:
                    osdep.private_file(descriptor)
                    osdep.lock(descriptor)
                    locked = True
                    current = self.load(record['content_sha256'], record['kind'], record['units'])
                    if current['revision'] != expected_revision:
                        raise ReadingConflict()
                    if record['revision'] >= 2**53 - 2:
                        raise ReadingError(SAVE_FAILED)
                    record['revision'] += 1
                    atomic_json(self.folder / (record['content_sha256'] + '.json'), record)
                    osdep.sync_directory(self.folder)
                finally:
                    try:
                        if locked:
                            osdep.unlock(descriptor)
                    finally:
                        os.close(descriptor)
            return record
        except ReadingError:
            raise
        except (OSError, ValueError, TypeError, ArchiveError):
            raise ReadingError(SAVE_FAILED) from None


def _zip_bounds(stream, size):
    """Bound the central directory before ZipFile allocates member objects."""
    stream.seek(max(0, size - 65_557))
    tail = stream.read(65_557)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError('Missing ZIP directory')
    _, disk, directory_disk, disk_count, count, length, start, comment = struct.unpack_from('<4s4H2IH', tail, offset)
    end = size - len(tail) + offset
    if (disk or directory_disk or disk_count != count or not 0 < count <= MAX_MEMBERS
            or length > 32 * 1024**2 or start + length != end or end + 22 + comment != size):
        raise ReadingError(UNSUPPORTED)
    stream.seek(start)
    central = stream.read(length)
    position = observed = expanded = 0
    while position < len(central):
        if (observed >= count or position + 46 > len(central)
                or central[position:position + 4] != b'PK\x01\x02'):
            raise ValueError('Invalid ZIP directory membership')
        name, extra, note = struct.unpack_from('<3H', central, position + 28)
        member_size = struct.unpack_from('<I', central, position + 24)[0]
        expanded += member_size
        if member_size > 100 * 1024**2 or expanded > MAX_ARCHIVE:
            raise ReadingError(UNSUPPORTED)
        position += 46 + name + extra + note
        observed += 1
    if position != length or observed != count:
        raise ValueError('Invalid ZIP directory length')
    stream.seek(0)


class _MarkupBounds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if tag not in {'img', 'br', 'hr'}:
            self.depth += 1
        if self.depth > 128 or self.nodes > 100_000:
            raise ValueError('Unsupported text structure')

    def handle_endtag(self, tag):
        if tag not in {'img', 'br', 'hr'}:
            self.depth -= 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


class ReaderBook:
    """Pinned read-only file; close retires active reads before releasing their descriptor."""

    def __init__(self, library, path, *, max_member_bytes=MAX_RENDER, max_text_bytes=8 * 1024**2):
        self.path, self.library = Path(path).absolute(), Path(library).absolute()
        self._gate = threading.Lock()
        self._closed, self._readers = False, 0
        self._stream = self._zip = None
        try:
            if (type(max_member_bytes) is not int or not 0 < max_member_bytes <= MAX_RENDER
                    or type(max_text_bytes) is not int or not 0 < max_text_bytes <= MAX_RENDER):
                raise ReadingError(UNSUPPORTED)
            _safe(self.path)
            _safe(self.library)
            if not self.path.is_relative_to(self.library) or self.path.suffix not in {'.cbz', '.epub'}:
                raise ValueError('Unregistered format')
            descriptor = os.open(self.path, os.O_RDONLY | osdep.NOFOLLOW)
            self._stream = os.fdopen(descriptor, 'rb')
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ReadingError(UNAVAILABLE)
            if info.st_size > MAX_ARCHIVE:
                raise ReadingError(UNSUPPORTED)
            self._identity = _fact(info)
            metadata = _json(sidecar_path(self.path), 16 * 1024**2)
            self.kind = metadata.get('format', 'cbz')
            if self.kind not in {'cbz', 'epub'} or self.path.suffix != '.' + self.kind:
                raise ValueError('Format mismatch')
            records = metadata.get('content_hashes')
            if not isinstance(records, list) or not 0 < len(records) <= MAX_MEMBERS:
                raise ValueError('Invalid membership')
            if any(not isinstance(record, dict) or type(record.get('size')) is not int
                   or record['size'] <= 0 for record in records):
                raise ValueError('Invalid member size')
            if any(record['size'] > max_member_bytes for record in records):
                raise ReadingError(UNSUPPORTED)
            if self.kind == 'epub':
                text = [record for record in records if record.get('file') == 'EPUB/chapter.xhtml']
                images = [record for record in records if isinstance(record.get('file'), str)
                          and record['file'].startswith('EPUB/images/')]
                if len(text) != 1:
                    raise ValueError('Missing chapter')
                if (text[0]['size'] > max_text_bytes or len(images) > 128
                        or sum(record['size'] for record in [*text, *images]) > max_member_bytes):
                    raise ReadingError(UNSUPPORTED)
            _zip_bounds(self._stream, info.st_size)
            if verify_episode_contents(self._stream, metadata):
                raise ValueError('Archive verification failed')
            self._stream.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: self._stream.read(1024 * 1024), b''):
                digest.update(chunk)
            self.content = digest.hexdigest()
            self._members = {record['file']: record for record in records}
            self.units = len(records) if self.kind == 'cbz' else 1
            self._pages = [record['file'] for record in records] if self.kind == 'cbz' else ['EPUB/chapter.xhtml']
            title = metadata.get('title')
            self.title = (title if isinstance(title, str) else self.path.stem)[:4096]
            self._zip = zipfile.ZipFile(self._stream)
            self._check()
        except ReadingError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ReadingError(UNAVAILABLE) from None

    def _check(self):
        _safe(self.path)
        if (_fact(self.path.stat()) != self._identity
                or _fact(os.fstat(self._stream.fileno())) != self._identity):
            raise ReadingError(UNAVAILABLE)

    @contextmanager
    def _reading(self):
        with self._gate:
            if self._closed:
                raise ReadingError(UNAVAILABLE)
            self._readers += 1
        try:
            self._check()
            yield
            with self._gate:
                if self._closed:
                    raise ReadingError(UNAVAILABLE)
            self._check()
        except ReadingError:
            raise
        except Exception:
            raise ReadingError(UNAVAILABLE) from None
        finally:
            with self._gate:
                self._readers -= 1
                if self._closed and not self._readers:
                    self._retire()

    def _read(self, name):
        record = self._members[name]
        info = self._zip.getinfo(name)
        if not 0 < info.file_size <= 100 * 1024**2 or info.file_size != record['size']:
            raise ValueError('Invalid member size')
        data = self._zip.read(info)
        if hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Member changed')
        return data

    def render(self, unit, *, max_bytes=MAX_RENDER, max_text_bytes=8 * 1024**2):
        if type(unit) is not int or not 0 <= unit < self.units:
            raise ReadingError(UNAVAILABLE)
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_RENDER:
            raise ReadingError(UNSUPPORTED)
        if type(max_text_bytes) is not int or not 0 < max_text_bytes <= MAX_RENDER:
            raise ReadingError(UNSUPPORTED)
        with self._reading():
            names = [self._pages[unit]]
            if self.kind == 'epub':
                names.extend(name for name in self._members if name.startswith('EPUB/images/'))
            # Refuse the aggregate before any decompression/allocation. A bounded
            # individual member does not bound a chapter with many illustrations.
            if (sum(self._members[name]['size'] for name in names) > max_bytes
                    or self.kind == 'epub' and (len(names) > 129
                        or self._members[names[0]]['size'] > max_text_bytes)):
                raise ReadingError(UNSUPPORTED)
            data = self._read(self._pages[unit])
            if self.kind == 'cbz':
                return {'kind': 'cbz', 'data': data}
            html = data.decode('utf-8')
            bounds = _MarkupBounds()
            bounds.feed(html)
            images = {'reader:/' + name: self._read(name) for name in self._members if name.startswith('EPUB/images/')}
            return {'kind': 'epub', 'html': html, 'images': images}

    def check(self):
        with self._reading():
            pass

    def _retire(self):
        if self._zip is not None:
            self._zip.close()
        if self._stream is not None:
            self._stream.close()

    def close(self):
        with self._gate:
            self._closed = True
            if not self._readers:
                self._retire()


class ReadingSession:
    def __init__(self, library, path, **limits):
        self.book = ReaderBook(library, path, **limits)
        self.store = ReadingStore(library)
        self.record = fresh_record(self.book.content, self.book.kind, self.book.units)
        self.warning = None
        self.can_save = True
        try:
            self.reload()
        except ReadingError as error:
            self.warning, self.can_save = str(error), False
        except BaseException:
            self.book.close()
            raise

    def reload(self):
        self.record = self.store.load(self.book.content, self.book.kind, self.book.units)
        self.can_save, self.warning = True, None
        return copy.deepcopy(self.record)

    def save(self, position, *, bookmark=False, preferences=None):
        candidate = copy.deepcopy(self.record)
        candidate['position'] = position
        if bookmark:
            bookmarks = candidate['bookmarks']
            if position in bookmarks:
                bookmarks.remove(position)
            elif len(bookmarks) >= 100:
                raise ReadingError('The bookmark limit is reached. Remove a bookmark before adding another.')
            else:
                bookmarks.append(dict(position))
        if preferences is not None:
            candidate['preferences'] = preferences
        candidate = checked_record(candidate)
        if self.can_save:
            try:
                candidate = self.store.save(candidate, self.record['revision'])
                self.warning = None
            except ReadingError as error:
                self.warning = str(error)
        self.record = candidate
        return copy.deepcopy(candidate), self.warning

    def close(self):
        self.book.close()
