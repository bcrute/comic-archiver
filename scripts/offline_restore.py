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
            if image_module:
                with image_module.open(io.BytesIO(data)) as image:
                    image.load()
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
                            if image_module:
                                with image_module.open(io.BytesIO(data)) as image: image.load()
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
                            valid = ((extension == '.png' and data.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in data[-16:])
                                     or (extension == '.jpg' and data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'))
                                     or (extension == '.gif' and data.startswith((b'GIF87a', b'GIF89a')) and data.endswith(b';'))
                                     or (extension == '.webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
                                         and int.from_bytes(data[4:8], 'little') + 8 == len(data)))
                            if not valid:
                                raise ValueError('Restored illustration format differs from manifest.')
                            if image_module:
                                with image_module.open(io.BytesIO(data)) as image:
                                    image.load()
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
                    if image_module:
                        with image_module.open(io.BytesIO(data)) as image:
                            image.load()
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
