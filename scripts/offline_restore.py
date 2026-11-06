#!/usr/bin/env python3
"""Copy and check portable tar.zst/CBZ/EPUB/JSON without importing the archiver.

Zstandard is required for tar.zst; Pillow is optional for --decode-images.
Never copies private state, sessions or cache files.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import tarfile
import zipfile
import xml.etree.ElementTree as ET
import re


# Standalone copy of comic_archive/image_formats.py: this script must work without
# an installed application. Exercise both copies with the same format corpus.
import zlib


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
    def __init__(self, source, maximum):
        import zstandard
        self.source,self.maximum,self.total=source,maximum,0
        self.decoder=zstandard.ZstdDecompressor(max_window_size=8*1024*1024).decompressobj()
        self.first_chunk=True
        self.buffer=bytearray()

    def read(self,size=-1):
        if size<0: raise ValueError('Unbounded archive read.')
        while len(self.buffer)<size and not self.decoder.eof:
            raw=self.source.read(4096)
            if not raw: raise ValueError('Incomplete compressed frame.')
            if self.first_chunk:
                import zstandard
                if zstandard.get_frame_parameters(raw).window_size>8*1024*1024:
                    raise ValueError('Compressed window exceeds supported size.')
                self.first_chunk=False
            data=self.decoder.decompress(raw);self.total+=len(data)
            if self.total>self.maximum: raise ValueError('Archive exceeds manifest.')
            self.buffer.extend(data)
            if self.decoder.eof and (self.decoder.unused_data or self.source.read(1)):
                raise ValueError('Unexpected trailing compressed data.')
        data=bytes(self.buffer[:size]);del self.buffer[:size]
        return data

    def finish(self):
        while data:=self.read(65536):
            if any(data): raise ValueError('Unexpected data after TAR terminator.')


def sidecar(path):
    return path.with_name(path.name[:-8]+'.json') if path.name.endswith('.tar.zst') else path.with_suffix('.json')


def offloaded(source, metadata_path):
    """An episode uploaded and then removed here, by the library's own verified receipt for these exact metadata bytes."""
    try:
        episode = json.loads(metadata_path.read_text())
        export_id = episode['export_id']
        if not isinstance(export_id, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', export_id):
            return False
        archive = metadata_path.with_suffix({'epub': '.epub', 'tar.zst': '.tar.zst'}.get(episode.get('format'), '.cbz'))
        if archive.exists() or archive.is_symlink():
            return False
        relative = archive.relative_to(source).as_posix()
        digest = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
        state = source / '.archive-state/cloud'
        for path in [*state.glob('outbox/*/' + export_id + '.json'), *state.glob('history/*/' + export_id + '/*.json')]:
            if path.is_symlink() or path.stat().st_size > 1024**2:
                continue
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                continue
            files = record.get('files')
            if (record.get('state') == 'verified' and record.get('eviction_requested') is True
                    and record.get('archive') == relative and isinstance(files, list) and len(files) >= 2
                    and isinstance(files[1], dict) and files[1].get('sha256') == digest):
                return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
    return False


def restore(source, destination, decode=False):
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
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError('Restore destination must be separate from the source library.')
    destination.mkdir(parents=True, exist_ok=False)
    report = {'archive_count':0,'series_count':0,'image_count':0,'cover_count':0,
              'novel_chapter_count':0,'novel_character_count':0,'offloaded_episode_count':0,
              'original_image_bytes':0,'images_decoded':decode,
              'application_imported':False,'cache_or_session_copied':False}
    for series_path in sorted(source.glob('*/series.json')):
        if series_path.parent.name.startswith('.'):
            continue
        if series_path.is_symlink() or series_path.parent.is_symlink():
            raise ValueError('Refusing a symlinked series export.')
        folder = destination / series_path.parent.name
        folder.mkdir()
        shutil.copy2(series_path, folder / 'series.json')
        series = json.loads((folder / 'series.json').read_text())
        report['series_count'] += 1
        cover = series.get('cover_file')
        if cover is not None:
            if not isinstance(cover,str) or cover not in {'cover.jpg','cover.png','cover.webp','cover.gif'}:
                raise ValueError('Invalid cover filename.')
            path = series_path.parent / cover
            if path.is_symlink():
                raise ValueError('Refusing a symlinked cover.')
            shutil.copy2(path, folder / cover)
            data = (folder / cover).read_bytes()
            if len(data) != series['cover_size'] or hashlib.sha256(data).hexdigest() != series['cover_sha256']:
                raise ValueError('Restored cover does not match its metadata.')
            check_image(data, Path(cover).suffix)
            report['cover_count'] += 1
        episode_paths = sorted([*series_path.parent.glob('*.cbz'), *series_path.parent.glob('*.epub'), *series_path.parent.glob('*.tar.zst')])
        local = {sidecar(path) for path in episode_paths}
        # Uploaded, then removed from the library: its metadata is restored alone; the archive is in the cloud.
        remote = [path for path in sorted(series_path.parent.glob('*.json'))
                  if path.name != 'series.json' and path not in local and offloaded(source, path)]
        if len(episode_paths) + len(remote) != series['episode_count']:
            raise ValueError('Series episode count does not match its portable exports.')
        for metadata_path in remote:
            if metadata_path.is_symlink():
                raise ValueError('Refusing a symlinked episode export.')
            shutil.copy2(metadata_path, folder / metadata_path.name)
            report['offloaded_episode_count'] += 1
        for archive_path in episode_paths:
            metadata_path = sidecar(archive_path)
            if archive_path.is_symlink() or metadata_path.is_symlink():
                raise ValueError('Refusing a symlinked episode export.')
            copied = folder / archive_path.name
            shutil.copy2(archive_path,copied)
            shutil.copy2(metadata_path,sidecar(copied))
            episode = json.loads(sidecar(copied).read_text())
            hashes = episode['content_hashes']
            if archive_path.name.endswith('.tar.zst'):
                if (episode.get('format')!='tar.zst' or type(episode.get('page_count')) is not int
                        or not 0<episode['page_count']<=10000 or len(hashes)!=episode['page_count']
                        or any(type(p.get('size')) is not int or not 0<p['size']<=100*1024**2 for p in hashes)):
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
                            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                                raise ValueError('Unsupported restored EPUB XML.')
                            ET.fromstring(data)  # noqa: S314 - DOCTYPE and ENTITY are refused just above
                    chapter = ET.fromstring(archive.read('EPUB/chapter.xhtml')).find(  # noqa: S314 - checked in the loop above
                        '{http://www.w3.org/1999/xhtml}body/{http://www.w3.org/1999/xhtml}div')
                    if chapter is None or not ''.join(chapter.itertext()).strip() and not images:
                        raise ValueError('Restored EPUB chapter is empty.')
                    characters = len(''.join(chapter.itertext()))
                    if characters != episode['character_count']:
                        raise ValueError('Restored EPUB chapter text count differs from JSON.')
                    references = [node.get('src') for node in chapter.iter('{http://www.w3.org/1999/xhtml}img')]
                    if references != [name.removeprefix('EPUB/') for name in images]:
                        raise ValueError('Restored illustration references differ from manifest.')
                    text = archive.read('EPUB/chapter.xhtml').split(b'<div id="chapter">', 1)[1].removesuffix(b'</div></body></html>')
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
                            or not 0 < member.file_size <= 100*1024**2
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
            print('Offline restore failed (ModuleNotFoundError). The partial destination is retained.')
        return 1
    except Exception as error:
        print(f'Offline restore failed ({type(error).__name__}). The partial destination is retained.')
        return 1
    print(json.dumps(report | {'destination':str(destination)},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
