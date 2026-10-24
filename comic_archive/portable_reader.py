"""Explicit desktop reading of Archiver exports without producer sidecars.

The inventory is derived in memory from the pinned original. It supplies no
producer completion, catalog import or connected-library authority.
"""
import hashlib
import re
import stat
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .local_reader import ReaderBook, ReadingError, ReadingSession, UNSUPPORTED, _MarkupBounds, _zip_bounds


class PortableReaderBook(ReaderBook):
    def _metadata(self, max_member_bytes, max_text_bytes):
        _zip_bounds(self._stream, self._identity[2])
        kind = self.path.suffix[1:]
        with zipfile.ZipFile(self._stream) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(set(names)) != len(names):
                raise ReadingError(UNSUPPORTED)
            if kind == 'epub':
                from .novels import EPUB_FILES
                if names[:len(EPUB_FILES)] != list(EPUB_FILES) or len(names) > len(EPUB_FILES) + 128:
                    raise ReadingError(UNSUPPORTED)
                image_bytes = sum(item.file_size for item in members[len(EPUB_FILES):])
                if image_bytes + members[3].file_size > max_member_bytes:
                    raise ReadingError(UNSUPPORTED)
            hashes, text = [], {}
            for item in members:
                name = item.filename
                if (item.orig_filename != name or len(name.encode('utf-8')) > 512
                        or '\\' in name or any(ord(c) < 32 for c in name)
                        or name.startswith('/') or any(p in {'', '.', '..'} for p in name.split('/'))
                        or item.is_dir() or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
                        or item.flag_bits & ~0x808 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or not 0 < item.file_size <= max_member_bytes):
                    raise ReadingError(UNSUPPORTED)
                if kind == 'cbz' and not re.fullmatch(r'[0-9]+\.(png|jpg|gif|webp)', name):
                    raise ReadingError(UNSUPPORTED)
                xml = kind == 'epub' and name in EPUB_FILES[1:]
                if xml and item.file_size > max_text_bytes:
                    raise ReadingError(UNSUPPORTED)
                digest, count, data = hashlib.sha256(), 0, bytearray()
                with archive.open(item) as source:
                    while chunk := source.read(64 * 1024):
                        count += len(chunk)
                        if count > item.file_size:
                            raise ReadingError(UNSUPPORTED)
                        digest.update(chunk)
                        if xml:
                            data.extend(chunk)
                if count != item.file_size:
                    raise ReadingError(UNSUPPORTED)
                hashes.append({'file': name, 'size': count, 'sha256': digest.hexdigest()})
                if xml:
                    raw = bytes(data)
                    if b'\x00' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                        raise ReadingError(UNSUPPORTED)
                    _MarkupBounds().feed(raw.decode('utf-8'))
                    text[name] = raw
        facts = {'title': self.path.stem, 'format': kind, 'content_hashes': hashes,
                 'content_type': 'comic' if kind == 'cbz' else 'novel',
                 'page_count': len(hashes) if kind == 'cbz' else 0}
        if kind == 'epub':
            from .novels import DC, OPF, XHTML
            package = ET.fromstring(text['EPUB/package.opf'])  # noqa: S314 -- declarations rejected above
            identity = package.find(f'{{{OPF}}}metadata/{{{DC}}}identifier')
            if identity is None or not isinstance(identity.text, str) or not identity.text.startswith('urn:uuid:'):
                raise ReadingError(UNSUPPORTED)
            export = identity.text.removeprefix('urn:uuid:')
            if str(uuid.UUID(export)) != export:
                raise ReadingError(UNSUPPORTED)
            chapter_bytes = text['EPUB/chapter.xhtml']
            chapter_root = ET.fromstring(chapter_bytes)  # noqa: S314 -- declarations rejected above
            chapter = chapter_root.find(f'{{{XHTML}}}body/{{{XHTML}}}div')
            ending = b'</div></body></html>'
            if chapter is None or not chapter_bytes.endswith(ending) or chapter_bytes.count(b'<div id="chapter">') != 1:
                raise ReadingError(UNSUPPORTED)
            fragment = chapter_bytes.split(b'<div id="chapter">', 1)[1][:-len(ending)]
            facts.update(export_id=export, chapter_count=1,
                         character_count=len(''.join(chapter.itertext())),
                         text_size=len(fragment), text_sha256=hashlib.sha256(fragment).hexdigest(),
                         illustration_count=len(hashes) - len(EPUB_FILES))
        # The inherited verifier checks canonical page order, all member hashes,
        # inert EPUB package/fragment/images and stable pinned original bytes.
        # This in-memory inventory has no schema_version/producer completion claim.
        return facts


class PortableReadingSession(ReadingSession):
    @staticmethod
    def _book(library, path, **limits):
        # Explicit desktop selection authorizes this one external original;
        # the session's unchanged ReadingStore remains in the active library.
        path = Path(path).absolute()
        return PortableReaderBook(path.parent, path, **limits)
