#!/usr/bin/env python3
"""Copy and check portable tar.zst/CBZ/EPUB/JSON without importing the archiver.

Zstandard is required for tar.zst; Pillow is optional for --decode-images.
Never copies private state, sessions or cache files.
"""
import argparse
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes
from datetime import datetime
import hashlib
from functools import lru_cache
import io
import json
from itertools import islice
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tarfile
import zipfile
import xml.etree.ElementTree as ET
import re


# Standalone copy of comic_archive/image_formats.py: this script must work without
# an installed application. Exercise both copies with the same format corpus.
import zlib


# Keep the per-archive policy in step with comic_archive.archive. This script
# also runs on its own, so it carries the small budget check with it.
MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_ARCHIVE_BYTES = 2 * 1024**3
MAX_RESTORE_EXPANDED_BYTES = 2 * 1024**4
MAX_PACKED_ARCHIVE_BYTES = MAX_EXPANDED_ARCHIVE_BYTES + 64 * 1024**2
MAX_RESTORE_PACKED_BYTES = 2 * 1024**4
MAX_SIDECAR_BYTES = 16 * 1024**2
EXPORT_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
SHA256 = re.compile(r'[0-9a-f]{64}')
ARCHIVE_FORMATS = {'cbz': '.cbz', 'epub': '.epub', 'tar.zst': '.tar.zst'}
COVER_NAMES = {'cover.jpg', 'cover.png', 'cover.gif', 'cover.webp'}
WINDOWS = os.name == 'nt'


def expanded_archive_bytes(members):
    if not isinstance(members, list) or not 0 < len(members) <= MAX_ARCHIVE_MEMBERS:
        raise ValueError('Archive member count exceeds the supported range.')
    total = 0
    for member in members:
        size = member.get('size') if isinstance(member, dict) else None
        if type(size) is not int or not 0 < size <= MAX_MEMBER_BYTES:
            raise ValueError('Archive member size exceeds the supported range.')
        total += size
        if total > MAX_EXPANDED_ARCHIVE_BYTES:
            raise ValueError('Expanded archive exceeds the supported size.')
    return total


class ImageStructureError(ValueError):
    """Only format names, byte counts and fixed reason codes; never source content."""

    def __init__(self, kind, size, offset, reason):
        self.kind, self.size, self.offset, self.reason = kind, size, offset, reason
        super().__init__(f'format={kind} bytes={size} offset={offset} reason={reason}')


def _invalid(data, kind, offset, reason):
    raise ImageStructureError(kind, len(data), min(offset, len(data)), reason)


def _jpeg(data):
    offset, frame, scan, pixels, height = 2, False, False, False, False
    entropy = False
    while offset < len(data):
        if entropy:
            marker = data.find(b'\xff', offset)
            if marker < 0:
                _invalid(data, 'jpeg', len(data), 'missing_eoi')
            pixels |= marker > offset
            offset = marker
        if data[offset] != 255:
            _invalid(data, 'jpeg', offset, 'expected_marker')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset == len(data):
            _invalid(data, 'jpeg', offset, 'truncated_marker')
        code, offset = data[offset], offset + 1
        if entropy and code == 0:  # A stuffed FF byte, not a marker.
            pixels = True
            continue
        if entropy and (0xD0 <= code <= 0xD7 or code == 1):
            continue  # Restart and TEM markers have no length field.
        in_scan, entropy = entropy, False
        if code == 0xD9:
            if not (frame and scan and pixels and height):
                _invalid(data, 'jpeg', offset, 'incomplete_image')
            return offset
        if code == 1:
            continue
        if code < 0xC0 or code == 0xD8 or 0xD0 <= code <= 0xD7 or offset + 2 > len(data):
            _invalid(data, 'jpeg', offset, 'invalid_marker_or_length')
        size = int.from_bytes(data[offset:offset + 2], 'big')
        end = offset + size
        if size < 2 or end > len(data):
            _invalid(data, 'jpeg', offset, 'invalid_segment_length')
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            if size < 8 or not data[offset + 7] or size != 8 + 3 * data[offset + 7]:
                _invalid(data, 'jpeg', offset, 'invalid_frame')
            if not int.from_bytes(data[offset + 5:offset + 7], 'big'):
                _invalid(data, 'jpeg', offset, 'invalid_width')
            frame = True
            height = bool(int.from_bytes(data[offset + 3:offset + 5], 'big'))
        elif code == 0xDA:
            if not frame or size < 6 or not 1 <= data[offset + 2] <= 4 or size != 6 + 2 * data[offset + 2]:
                _invalid(data, 'jpeg', offset, 'invalid_scan')
            scan, entropy = True, True
        elif code == 0xDC:  # DNL supplies a frame height initially recorded as zero.
            if size != 4 or not in_scan or not int.from_bytes(data[offset + 2:end], 'big'):
                _invalid(data, 'jpeg', offset, 'invalid_dnl')
            height = True
            entropy = in_scan
        offset = end
    _invalid(data, 'jpeg', offset, 'missing_eoi')


def _png(data):
    offset, header, image, palette, closed = 8, False, False, False, False
    colour = None
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        end = offset + 8 + size
        if size > 0x7fffffff or end + 4 > len(data):
            _invalid(data, 'png', offset, 'truncated_chunk')
        if zlib.crc32(memoryview(data)[offset + 4:end]) != int.from_bytes(data[end:end + 4], 'big'):
            _invalid(data, 'png', offset, 'crc_mismatch')
        if not header:
            if kind != b'IHDR' or size != 13:
                _invalid(data, 'png', offset, 'invalid_ihdr')
            if not all(0 < int.from_bytes(data[start:start + 4], 'big') <= 0x7fffffff
                       for start in (offset + 8, offset + 12)):
                _invalid(data, 'png', offset, 'invalid_dimensions')
            depth, colour, compression, filtering, interlace = data[offset + 16:offset + 21]
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(colour, ()) or compression or filtering or interlace not in (0, 1):
                _invalid(data, 'png', offset, 'invalid_ihdr_fields')
            header = True
        elif kind == b'IHDR':
            _invalid(data, 'png', offset, 'duplicate_ihdr')
        elif kind == b'IEND':
            if not (size == 0 and image and (colour != 3 or palette)):
                _invalid(data, 'png', offset, 'incomplete_image')
            return end + 4
        elif kind == b'IDAT':
            if closed or (colour == 3 and not palette):
                _invalid(data, 'png', offset, 'invalid_idat_order')
            image |= size > 0
        else:
            closed |= image
            if kind == b'PLTE':
                if image or palette or colour in (0, 4) or not 0 < size <= 768 or size % 3:
                    _invalid(data, 'png', offset, 'invalid_palette')
                palette = True
            elif not kind or not kind[0] & 32:  # Unknown critical chunks need a decoder that understands them.
                _invalid(data, 'png', offset, 'unknown_critical_chunk')
        offset = end + 4
    _invalid(data, 'png', offset, 'missing_iend')


def _gif_blocks(data, offset):
    while offset < len(data):
        size, offset = data[offset], offset + 1
        if not size:
            return offset
        offset += size
        if offset > len(data):
            break
    _invalid(data, 'gif', offset, 'truncated_subblock')


def _gif(data):
    if len(data) < 13 or not int.from_bytes(data[6:8], 'little') or not int.from_bytes(data[8:10], 'little'):
        _invalid(data, 'gif', 6, 'invalid_screen_descriptor')
    packed = data[10]
    offset = 13 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
    image = False
    while offset < len(data):
        code, offset = data[offset], offset + 1
        if code == 0x3B:
            if not image:
                _invalid(data, 'gif', offset, 'missing_image')
            return offset
        if code == 0x21:
            if offset >= len(data):
                _invalid(data, 'gif', offset or len(data), 'truncated_extension')
            label, offset = data[offset], offset + 1
            if label == 0xF9 and (offset + 6 > len(data) or data[offset] != 4 or data[offset + 5] != 0):
                _invalid(data, 'gif', offset or len(data), 'invalid_graphic_control')
        elif code == 0x2C:
            if offset + 9 > len(data) or not int.from_bytes(data[offset + 4:offset + 6], 'little') or not int.from_bytes(data[offset + 6:offset + 8], 'little'):
                _invalid(data, 'gif', offset or len(data), 'invalid_image_descriptor')
            packed = data[offset + 8]
            offset += 9 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
            if offset + 1 >= len(data) or not 2 <= data[offset] <= 8 or not data[offset + 1]:
                _invalid(data, 'gif', offset or len(data), 'invalid_colour_table_or_lzw_header')
            offset += 1  # LZW minimum code size, followed by counted data sub-blocks.
            image = True
        else:
            _invalid(data, 'gif', offset or len(data), 'invalid_block')
        offset = _gif_blocks(data, offset)
        if offset is None:
            _invalid(data, 'gif', offset or len(data), 'truncated_subblock')
    _invalid(data, 'gif', offset or len(data), 'missing_trailer')


def _webp_chunks(data, offset, end, animation=True):
    image, extended, control = False, False, False
    while offset + 8 <= end:
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], 'little')
        start = offset + 8
        following = start + size + (size & 1)
        if following > end or (size & 1 and data[following - 1] != 0):
            _invalid(data, 'webp', offset, 'invalid_chunk_bounds_or_padding')
        if kind == b'VP8 ':
            if size < 10 or data[start] & 1 or data[start + 3:start + 6] != b'\x9d\x01\x2a':
                _invalid(data, 'webp', offset, 'invalid_vp8_header')
            if not all(int.from_bytes(data[p:p + 2], 'little') & 0x3fff for p in (start + 6, start + 8)):
                _invalid(data, 'webp', offset, 'invalid_dimensions')
            image = True
        elif kind == b'VP8L':
            if size < 5 or data[start] != 0x2f or data[start + 4] & 0xe0:
                _invalid(data, 'webp', offset, 'invalid_vp8l_header')
            image = True
        elif kind == b'VP8X':
            if not animation or size != 10:
                _invalid(data, 'webp', offset, 'invalid_vp8x_header')
            extended = True
        elif kind == b'ANIM':
            if not animation or not extended or size != 6:
                _invalid(data, 'webp', offset, 'invalid_animation_control')
            control = True
        elif kind == b'ANMF':
            if not animation or not control or size < 16 or not _webp_chunks(data, start + 16, start + size, False):
                _invalid(data, 'webp', offset, 'invalid_animation_frame')
            image = True
        offset = following
    if not image or offset != end:
        _invalid(data, 'webp', offset, 'incomplete_image')
    return end


def inspect_image_structure(data):
    """Return (extension, logical end); leave every original byte untouched."""
    if not isinstance(data, (bytes, bytearray)):
        raise ImageStructureError('unknown', 0, 0, 'not_bytes')
    if data.startswith(b'\xff\xd8'):
        return '.jpg', _jpeg(data)
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png', _png(data)
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif', _gif(data)
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        end = int.from_bytes(data[4:8], 'little') + 8
        if not 12 <= end <= len(data):
            _invalid(data, 'webp', 4, 'invalid_riff_length')
        return '.webp', _webp_chunks(data, 12, end)
    _invalid(data, 'unknown', 0, 'unsupported_signature')


def image_stream_extension(data):
    """Identify a complete structure, independently of optional pixel decoding."""
    return inspect_image_structure(data)[0]

class FrameReader:
    """Standalone copy of the app's bounded single-frame Zstandard reader."""
    def __init__(self, source, maximum):
        import zstandard
        self.source, self.maximum, self.total = source, maximum, 0
        self.decoder = zstandard.ZstdDecompressor().decompressobj()
        self.block_limit = None
        self.prefix = bytearray()
        self.buffer = bytearray()

    def _input(self, size, *, allow_end=False):
        data = self.prefix[:size]
        del self.prefix[:size]
        while len(data) < size:
            chunk = self.source.read(size - len(data))
            if not chunk:
                if allow_end:
                    break
                raise ValueError('Incomplete compressed frame')
            data.extend(chunk)
        return data

    def _block(self):
        if self.block_limit is None:
            import zstandard
            header = self._input(18, allow_end=True)
            if not header.startswith(b'\x28\xb5\x2f\xfd'):
                raise ValueError('Invalid Zstandard frame header')
            header_size = zstandard.frame_header_size(bytes(header))
            params = zstandard.get_frame_parameters(bytes(header))
            if params.window_size > 8 * 1024 * 1024:
                raise ValueError('Compressed window exceeds the supported size')
            self.block_limit = min(params.window_size, 128 * 1024)
            self.checksum = params.has_checksum
            self.prefix = header[header_size:]
            self.decoder.decompress(bytes(header[:header_size]))

        header = self._input(3)
        descriptor = int.from_bytes(header, 'little')
        last = bool(descriptor & 1)
        kind = (descriptor >> 1) & 3
        length = descriptor >> 3
        if kind == 3 or length > self.block_limit:
            raise ValueError('Invalid compressed block header')
        if kind != 2 and self.total + length > self.maximum:
            raise ValueError('Archive exceeds its manifest')
        block = self._input(1 if kind == 1 else length)
        if last and self.checksum:
            block.extend(self._input(4))
        data = self.decoder.decompress(bytes(header + block))
        self.total += len(data)
        if self.total > self.maximum:
            raise ValueError('Archive exceeds its manifest')
        if self.decoder.eof != last:
            raise ValueError('Incomplete compressed frame')
        if last and (self.decoder.unused_data or self.prefix or self.source.read(1)):
            raise ValueError('Unexpected data after compressed frame')
        return data

    def read(self, size=-1):
        if size < 0: raise ValueError('Unbounded archive reads are unsupported')
        while len(self.buffer) < size and not self.decoder.eof:
            self.buffer.extend(self._block())
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def finish(self):
        while data:=self.read(65536):
            if any(data): raise ValueError('Unexpected data after TAR terminator.')


def sidecar(path):
    return path.with_name(path.name[:-8]+'.json') if path.name.endswith('.tar.zst') else path.with_suffix('.json')


DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [('attributes', DWORD), ('times', DWORD * 6), ('volume', DWORD),
                ('size_high', DWORD), ('size_low', DWORD), ('links', DWORD),
                ('index_high', DWORD), ('index_low', DWORD)]


@lru_cache(maxsize=1)
def _windows_file_api():
    from msvcrt import open_osfhandle

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    def bind(name, result, *arguments):
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, list(arguments)
        return function

    return {'open': bind('CreateFileW', HANDLE, ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p,
                         DWORD, DWORD, HANDLE),
            'info': bind('GetFileInformationByHandle', ctypes.c_int, HANDLE, ctypes.c_void_p),
            'type': bind('GetFileType', DWORD, HANDLE),
            'close': bind('CloseHandle', ctypes.c_int, HANDLE), 'descriptor': open_osfhandle}


def _windows_name(path):
    text = str(path)
    prefix = '\\\\?\\'
    if text.startswith(prefix):
        return text
    return prefix + ('UNC\\' + text[2:] if text.startswith('\\\\') else text)


def _open_windows_entry(path, *, directory):
    api = _windows_file_api()
    read_attributes, read_data, share_read, open_existing = 0x80, 0x80000000, 1, 3
    no_follow = 0x02200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    handle = api['open'](_windows_name(path), read_attributes if directory else read_data,
                         share_read, None, open_existing, no_follow, None)
    if handle in (None, HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _WindowsFileInformation()
        if (api['type'](handle) != 1 or not api['info'](handle, ctypes.byref(info))
                or info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory):
            raise ValueError('Refusing a link or non-disk entry in the restore source.')
        return handle
    except BaseException:
        api['close'](handle)
        raise


@contextmanager
def _hold_windows_directory(directory):
    directory = directory.absolute()
    if '..' in directory.parts:
        raise ValueError('Invalid restore source path.')
    close = _windows_file_api()['close']
    with ExitStack() as held:
        for folder in (*reversed(directory.parents), directory):
            held.callback(close, _open_windows_entry(folder, directory=True))
        yield


def _source_parts(source, path):
    parts = path.relative_to(source).parts
    if any(part in {'.', '..'} for part in parts):
        raise ValueError('Invalid restore source path.')
    return parts


def source_entry(source, path):
    """Check the selected source and each component before visiting the leaf."""
    parts = _source_parts(source, path)
    hold = _hold_windows_directory(path.parent) if WINDOWS else nullcontext()
    with hold:
        return _source_entry(source, parts)


def _source_entry(source, parts):
    entry = source
    result = entry.lstat()
    for part in (*parts, None):
        if (stat.S_ISLNK(result.st_mode)
                or getattr(result, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Refusing a link or reparse point in the restore source.')
        if part is None:
            return result
        if not stat.S_ISDIR(result.st_mode):
            raise ValueError('Invalid restore source path.')
        entry /= part
        result = entry.lstat()


def source_glob(source, directory, pattern, limit=None):
    parts = _source_parts(source, directory)
    hold = _hold_windows_directory(directory) if WINDOWS else nullcontext()
    with hold:
        if not stat.S_ISDIR(_source_entry(source, parts).st_mode):
            raise ValueError('Restore source must be a directory.')
        return list(islice(directory.glob(pattern), limit))


def open_source_fd(source, path, flags):
    """Use directory-relative no-follow opens on platforms that provide them."""
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        return os.open(path, flags)
    *parents, name = _source_parts(source, path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(source, directory_flags)
    try:
        for parent in parents:
            child = os.open(parent, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(name, flags, dir_fd=directory)
    finally:
        os.close(directory)


@contextmanager
def open_source(source, path):
    if WINDOWS:
        _source_parts(source, path)
        api = _windows_file_api()
        with _hold_windows_directory(path.parent):
            handle = _open_windows_entry(path, directory=False)
            try:
                descriptor = api['descriptor'](handle, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
            except BaseException:
                api['close'](handle)
                raise
            with os.fdopen(descriptor, 'rb') as stream:
                yield stream, os.fstat(descriptor).st_size
        return
    before = source_entry(source, path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Restore source must be a regular file.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(open_source_fd(source, path, flags), 'rb') as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or not os.path.samestat(before, current):
            raise ValueError('Restore source changed before reading.')
        yield stream, current.st_size


def read_source(source, path, limit):
    with open_source(source, path) as (stream, size):
        if size > limit:
            raise ValueError('Restore source file exceeds its size limit.')
        data = stream.read(limit + 1)
        if len(data) != size:
            raise ValueError('Restore source file changed while reading.')
        return data


def copy_archive(source, path, destination, limit):
    with open_source(source, path) as (stream, size):
        if size > limit:
            raise ValueError('Packed archives exceed the restore disk budget.')
        if size > shutil.disk_usage(destination.parent).free:
            raise ValueError('Insufficient space to stage the restored archive.')
        remaining = size
        with destination.open('xb') as output:
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Restore source file became shorter while copying.')
                output.write(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ValueError('Restore source file grew while copying.')
        return size


def checked_receipt(record, library_id, series_id, episode_id, relative, archive_format, sidecar_hash):
    """Match the historical receipt provenance used by the working-library importer."""
    if (not isinstance(record, dict) or record.get('state') != 'verified'
            or record.get('eviction_requested') is not True or record.get('library_id') != library_id
            or record.get('series_id') != series_id or record.get('episode_id') != episode_id
            or record.get('archive') != relative or record.get('remote_root') != 'Comic-Archive'):
        return False
    target_id = record.get('target_id')
    if not isinstance(target_id, str) or not EXPORT_ID.fullmatch(target_id):
        return False
    try:
        if datetime.fromisoformat(record['verified_at']).tzinfo is None:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    archive = Path(relative)
    names = ['episode.' + archive_format, 'episode.json', 'series.json']
    paths = [relative, sidecar(archive).as_posix(), (archive.parent / 'series.json').as_posix()]
    files = record.get('files')
    if not isinstance(files, list) or len(files) not in {3, 4} or any(not isinstance(item, dict) for item in files):
        return False
    if len(files) == 4:
        cover = files[3].get('name')
        if not isinstance(cover, str) or cover not in COVER_NAMES:
            return False
        names.append(cover)
        paths.append((archive.parent / cover).as_posix())
    for item, name, path in zip(files, names, paths, strict=True):
        checksum = item.get('sha256')
        if item.get('name') != name or item.get('path') != path or not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            return False
    version = hashlib.sha256((files[0]['sha256'] + files[1]['sha256']).encode('ascii')).hexdigest()
    return files[1]['sha256'] == sidecar_hash and record.get('version') == version


def offloaded_metadata(source, metadata_path, series_id):
    """Return the exact sidecar bytes bound to a verified local eviction receipt."""
    try:
        raw = read_source(source, metadata_path, MAX_SIDECAR_BYTES)
        episode = json.loads(raw)
        export_id = episode['export_id']
        archive_format = episode['format']
        if (episode.get('schema_version') != 2 or not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id)
                or not isinstance(series_id, str) or not EXPORT_ID.fullmatch(series_id)
                or archive_format not in ARCHIVE_FORMATS
                or episode.get('content_type') != ('novel' if archive_format == 'epub' else 'comic')):
            return None
        archive = metadata_path.with_suffix(ARCHIVE_FORMATS[archive_format])
        try:
            source_entry(source, archive)
        except FileNotFoundError:
            pass
        else:
            return None
        relative = archive.relative_to(source).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        state = source / '.archive-state/cloud'
        targets = json.loads(read_source(source, state / 'targets.json', 1024**2))
        library_id = targets.get('library_id')
        if targets.get('schema_version') != 1 or not isinstance(library_id, str) or not EXPORT_ID.fullmatch(library_id):
            return None
        paths = [state / 'outbox' / 'b2' / (export_id + '.json')]
        history = state / 'history' / 'b2' / export_id
        try:
            if stat.S_ISDIR(source_entry(source, history).st_mode):
                paths.extend(source_glob(source, history, '*.json', 4096))
        except FileNotFoundError:
            pass
        for path in paths:
            try:
                record = json.loads(read_source(source, path, 1024**2))
            except FileNotFoundError:
                continue
            if checked_receipt(record, library_id, series_id, export_id, relative, archive_format, digest):
                if path.parent == history and path.name != f"{record['target_id']}-{record['version']}.json":
                    continue
                return raw
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return None


def restore(source, destination, decode=False):
    source = source.absolute()
    hold = _hold_windows_directory(source) if WINDOWS else nullcontext()
    with hold:
        source_entry(source, source)
        physical = source.resolve(strict=True)
        return _restore_staged(source if WINDOWS else physical, physical, destination, decode)


def _restore_staged(source, physical, destination, decode):
    destination = destination.absolute()
    resolved = destination.resolve()
    if resolved == physical or physical in resolved.parents:
        raise ValueError('Restore destination must be separate from the source library.')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.comic-restore-', dir=destination.parent) as work:
        staging = Path(work)
        report = _restore(source, staging, decode)
        # Reserve a fresh destination without replacing any existing directory.
        destination.mkdir()
        for item in staging.iterdir():
            if item.name != 'restore-report.json':
                item.rename(destination / item.name)
        (staging / 'restore-report.json').rename(destination / 'restore-report.json')
        return report


# Keep these parsing restrictions aligned with comic_archive.novels. The
# standalone recovery script must also work without an installed archiver.
class _PassiveXML(ET.TreeBuilder):
    def pi(self, target, text):
        raise ValueError('Processing instructions are unsupported.')

    def doctype(self, name, public_id, system_id):
        raise ValueError('Document type declarations are unsupported.')


def passive_xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsupported restored EPUB XML.')
    return ET.fromstring(data, parser=ET.XMLParser(target=_PassiveXML()))  # noqa: S314 - parser refuses DTDs and PIs


def _restore(source, destination, decode):
    image_module = None
    if decode:
        from PIL import Image
        image_module = Image
    def check_image(data, extension):
        if image_stream_extension(data) != extension:
            raise ValueError('Restored image format differs from its manifest.')
        if image_module:
            from PIL import ImageSequence
            with image_module.open(io.BytesIO(data)) as image:
                image.verify()
            with image_module.open(io.BytesIO(data)) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()
    report = {'archive_count':0,'series_count':0,'image_count':0,'cover_count':0,
              'novel_chapter_count':0,'novel_character_count':0,'offloaded_episode_count':0,
              'original_image_bytes':0,'images_decoded':decode,
              'application_imported':False,'cache_or_session_copied':False}
    remaining = MAX_RESTORE_EXPANDED_BYTES
    packed_remaining = MAX_RESTORE_PACKED_BYTES

    def write_small(path, data):
        nonlocal packed_remaining
        if len(data) > packed_remaining:
            raise ValueError('Packed archives and metadata exceed the restore disk budget.')
        packed_remaining -= len(data)
        path.write_bytes(data)

    for directory in sorted(source_glob(source, source, '*')):
        if directory.name.startswith('.') or not stat.S_ISDIR(source_entry(source, directory).st_mode):
            continue
        series_path = directory / 'series.json'
        try:
            raw_series = read_source(source, series_path, MAX_SIDECAR_BYTES)
        except FileNotFoundError:
            continue
        folder = destination / directory.name
        folder.mkdir()
        write_small(folder / 'series.json', raw_series)
        series = json.loads(raw_series)
        report['series_count'] += 1
        cover = series.get('cover_file')
        if cover is not None:
            if not isinstance(cover,str) or cover not in COVER_NAMES:
                raise ValueError('Invalid cover filename.')
            path = series_path.parent / cover
            data = read_source(source, path, 10 * 1024**2)
            if len(data) != series['cover_size'] or hashlib.sha256(data).hexdigest() != series['cover_sha256']:
                raise ValueError('Restored cover does not match its metadata.')
            check_image(data, Path(cover).suffix)
            write_small(folder / cover, data)
            report['cover_count'] += 1
        episode_paths = sorted(path for pattern in ('*.cbz', '*.epub', '*.tar.zst')
                               for path in source_glob(source, directory, pattern))
        local = {sidecar(path) for path in episode_paths}
        # Uploaded, then removed from the library: its metadata is restored alone; the archive is in the cloud.
        remote_count = 0
        for metadata_path in sorted(source_glob(source, directory, '*.json')):
            if metadata_path == series_path or metadata_path in local:
                continue
            raw = offloaded_metadata(source, metadata_path, series.get('export_id'))
            if raw is None:
                continue
            write_small(folder / metadata_path.name, raw)
            remote_count += 1
            report['offloaded_episode_count'] += 1
        if len(episode_paths) + remote_count != series['episode_count']:
            raise ValueError('Series episode count does not match its portable exports.')
        for archive_path in episode_paths:
            metadata_path = sidecar(archive_path)
            raw = read_source(source, metadata_path, MAX_SIDECAR_BYTES)
            episode = json.loads(raw)
            hashes = episode['content_hashes']
            remaining -= expanded_archive_bytes(hashes)
            if remaining < 0:
                raise ValueError('Expanded archives exceed the budget for one restore.')
            copied = folder / archive_path.name
            packed_remaining -= copy_archive(source, archive_path, copied,
                                             min(MAX_PACKED_ARCHIVE_BYTES, packed_remaining))
            write_small(sidecar(copied), raw)
            if archive_path.name.endswith('.tar.zst'):
                if (episode.get('format')!='tar.zst' or type(episode.get('page_count')) is not int
                        or len(hashes)!=episode['page_count']):
                    raise ValueError('Invalid restored compressed comic manifest.')
                body_size=sum(512+((p['size']+511)//512)*512 for p in hashes)
                maximum=((body_size+1024+10239)//10240)*10240
                with copied.open('rb') as stream:
                    reader=FrameReader(stream,maximum)
                    with tarfile.open(fileobj=reader,mode='r|',bufsize=512) as archive:
                        for position,page in enumerate(hashes,1):
                            member=archive.next()
                            width=max(4,len(str(len(hashes))))
                            if (member is None or not member.isfile() or member.name!=page['file']
                                    or Path(member.name).name!=member.name or Path(member.name).stem!=f'{position:0{width}d}'
                                    or member.size!=page['size'] or member.linkname or member.pax_headers):
                                raise ValueError('Invalid restored compressed comic membership/order/size.')
                            data=archive.extractfile(member).read(page['size']+1)
                            if len(data)!=page['size'] or hashlib.sha256(data).hexdigest()!=page['sha256']:
                                raise ValueError('Restored compressed comic page hash differs from JSON.')
                            check_image(data, Path(page['file']).suffix)
                            report['image_count']+=1;report['original_image_bytes']+=len(data)
                        if archive.next() is not None: raise ValueError('Unexpected restored archive member.')
                    reader.finish()
                report['archive_count']+=1
                continue
            if archive_path.suffix == '.epub':
                expected = ['mimetype', 'META-INF/container.xml', 'EPUB/package.opf',
                            'EPUB/chapter.xhtml', 'EPUB/nav.xhtml']
                count = episode.get('illustration_count', 0)
                if type(count) is not int or not 0 <= count <= 1000:
                    raise ValueError('Invalid restored illustration count.')
                images = [item['file'] for item in hashes][len(expected):]
                if len(images) != count or any(not re.fullmatch(
                        rf'EPUB/images/{index:04d}\.(jpg|png|gif|webp)', name)
                        for index, name in enumerate(images, 1)):
                    raise ValueError('Invalid restored illustration membership.')
                expected += images
                if (episode.get('format') != 'epub' or episode.get('content_type') != 'novel'
                        or episode.get('chapter_count') != 1 or episode.get('page_count') != 0
                        or [p['file'] for p in hashes] != expected):
                    raise ValueError('Invalid restored EPUB metadata.')
                with zipfile.ZipFile(copied) as archive:
                    if archive.namelist() != expected or archive.read('mimetype') != b'application/epub+zip':
                        raise ValueError('Invalid restored EPUB membership or mimetype.')
                    if archive.getinfo('mimetype').compress_type != zipfile.ZIP_STORED:
                        raise ValueError('Restored EPUB mimetype must be uncompressed.')
                    chapter = None
                    chapter_data = b''
                    for item in hashes:
                        member = archive.getinfo(item['file'])
                        cap = 10*1024**2 if item['file'] in images else 3*1024**2
                        if not 0 < member.file_size <= cap or member.file_size != item['size']:
                            raise ValueError('Invalid restored EPUB file size.')
                        data = archive.read(member)
                        if hashlib.sha256(data).hexdigest() != item['sha256']:
                            raise ValueError('Restored EPUB content hash differs from JSON.')
                        if item['file'] in images:
                            extension = Path(item['file']).suffix
                            check_image(data, extension)
                            report['image_count'] += 1
                            report['original_image_bytes'] += len(data)
                        elif item['file'] != 'mimetype':
                            document = passive_xml(data)
                            if item['file'] == 'EPUB/chapter.xhtml':
                                chapter_data = data
                                chapter = document.find(
                                    '{http://www.w3.org/1999/xhtml}body/{http://www.w3.org/1999/xhtml}div')
                    if chapter is None or not ''.join(chapter.itertext()).strip() and not images:
                        raise ValueError('Restored EPUB chapter is empty.')
                    characters = len(''.join(chapter.itertext()))
                    if characters != episode['character_count']:
                        raise ValueError('Restored EPUB chapter text count differs from JSON.')
                    references = [node.get('src') for node in chapter.iter('{http://www.w3.org/1999/xhtml}img')]
                    if references != [name.removeprefix('EPUB/') for name in images]:
                        raise ValueError('Restored illustration references differ from manifest.')
                    text = chapter_data.split(b'<div id="chapter">', 1)[1].removesuffix(b'</div></body></html>')
                    if len(text) != episode['text_size'] or hashlib.sha256(text).hexdigest() != episode['text_sha256']:
                        raise ValueError('Restored chapter text hash differs from JSON.')
                    report['novel_chapter_count'] += 1
                    report['novel_character_count'] += characters
                report['archive_count'] += 1
                continue
            if type(episode['page_count']) is not int or episode['page_count'] < 1 or len(hashes) != episode['page_count']:
                raise ValueError('Invalid restored episode page count.')
            with zipfile.ZipFile(copied) as archive:
                members = archive.infolist()
                if [m.filename for m in members] != [p['file'] for p in hashes]:
                    raise ValueError('Restored CBZ page membership/order differs from JSON.')
                if len({m.filename for m in members}) != len(members):
                    raise ValueError('Duplicate CBZ pages.')
                for position,(member,page) in enumerate(zip(members,hashes,strict=True),1):
                    if (Path(member.filename).name != member.filename
                            or Path(member.filename).stem != f'{position:04d}'
                            or member.file_size != page['size']):
                        raise ValueError('Invalid restored CBZ page name/size.')
                    data = archive.read(member)
                    if hashlib.sha256(data).hexdigest() != page['sha256']:
                        raise ValueError('Restored CBZ image hash differs from JSON.')
                    check_image(data, Path(page['file']).suffix)
                    report['image_count'] += 1
                    report['original_image_bytes'] += len(data)
            report['archive_count'] += 1
    if not report['archive_count'] and not report['offloaded_episode_count']:
        raise ValueError('No portable episode archives were found.')
    (destination/'restore-report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--destination',type=Path,help='New directory outside the source library; default: a temporary directory')
    parser.add_argument('--decode-images',action='store_true',help='Decode every comic page and cover using Pillow')
    args = parser.parse_args()
    destination = args.destination or Path(tempfile.mkdtemp(prefix='comic-offline-restore-'))/'Library'
    try:
        report = restore(args.source,destination,args.decode_images)
    except ModuleNotFoundError as error:
        if args.decode_images and error.name=='PIL':
            print('Image decoding requires Pillow. Use a Python with Pillow installed, or omit --decode-images to check page order, sizes and hashes.')
        else:
            print('Offline restore failed (ModuleNotFoundError).')
        return 1
    except Exception as error:
        print(f'Offline restore failed ({type(error).__name__}).')
        return 1
    print(json.dumps(report | {'destination':str(destination)},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
