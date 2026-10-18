"""Scoped novel text and self-contained EPUBs; no scripts or remote resources."""
from html import escape
from html.parser import HTMLParser
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlsplit
import base64
import binascii

from .models import ArchiveError
from .errors import UnsupportedContent

MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_TAGS = frozenset({'div', 'p', 'span', 'em', 'strong', 'i', 'b', 'u', 's', 'del',
                      'sub', 'sup', 'blockquote', 'ul', 'ol', 'li', 'h1', 'h2', 'h3',
                      'h4', 'h5', 'h6', 'br', 'hr', 'strike'})
VOID_TAGS = frozenset({'br', 'hr'})
MAX_ILLUSTRATION_BYTES = 10 * 1024 * 1024
MAX_ILLUSTRATIONS = 1000
IMAGE_TYPES = {'.jpg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}
STYLES = {'font-style': {'normal', 'italic'}, 'font-weight': {'normal', 'bold', '400', '700'},
          'text-align': {'left', 'right', 'center', 'justify'},
          'text-decoration': {'none', 'underline', 'line-through'}}
EPUB_FILES = ('mimetype', 'META-INF/container.xml', 'EPUB/package.opf',
              'EPUB/chapter.xhtml', 'EPUB/nav.xhtml')
XHTML = 'http://www.w3.org/1999/xhtml'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
# Characters XML 1.0 cannot hold even escaped, such as most control characters.
NOT_XML = re.compile('[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


def xml_text(value, quote=True):
    """Escaped text without the characters XML cannot hold."""
    return escape(NOT_XML.sub('', value), quote=quote)


def incomplete(reason=None):
    error = UnsupportedContent('Novel reader lacks a complete supported text chapter; its queue is retained.')
    error.code = 'novel_manifest_incomplete'
    error.novel_reason = reason
    return error


def safe_style(value):
    result = []
    for declaration in (value or '').split(';'):
        key, sep, item = declaration.strip().partition(':')
        key, item = key.strip().lower(), item.strip().lower()
        if sep and item in STYLES.get(key, ()):
            result.append(f'{key}: {item}')
        elif key in {'display', 'visibility', 'opacity'} and item in {'none', 'hidden', '0'}:
            raise incomplete()
    return '; '.join(result)


def illustration_names(illustrations):
    from .archive import image_extension
    if not isinstance(illustrations, tuple) or len(illustrations) > MAX_ILLUSTRATIONS:
        raise ArchiveError('Novel illustrations exceed the supported count.')
    names = []
    total = 0
    for index, data in enumerate(illustrations, 1):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ILLUSTRATION_BYTES:
            raise ArchiveError('Novel illustration exceeds the supported size.')
        total += len(data)
        if total > MAX_ILLUSTRATION_BYTES:
            raise ArchiveError('Novel illustrations exceed the supported total size.')
        names.append(f'images/{index:04d}{image_extension(data)}')
    return names


def validate_fragment(fragment, illustrations=()):
    if not isinstance(fragment, str) or len(fragment.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ArchiveError('Novel text exceeds the supported size.')
    try:
        root = ET.fromstring('<div>' + fragment + '</div>')
        names = illustration_names(illustrations)
        observed = []
        if not ''.join(root.itertext()).strip() and not names:
            raise ValueError
        for element in root.iter():
            if element.tag == 'img':
                if (set(element.attrib) - {'src', 'alt', 'style'}
                        or element.get('src') not in names or list(element) or element.text):
                    raise ValueError
                observed.append(element.get('src'))
            elif element.tag not in TEXT_TAGS or set(element.attrib) - {'style'}:
                raise ValueError
            if element.get('style') is not None and safe_style(element.get('style')) != element.get('style'):
                raise ValueError
        if observed != names:
            raise ValueError
    except (ET.ParseError, ValueError, UnsupportedContent):
        raise ArchiveError('Novel text is empty or contains unsupported markup.') from None
    return root


class NovelHTML(HTMLParser):
    """Require one closed epub-<id> inside the exact episode-<id> reader."""
    def __init__(self, episode_id):
        super().__init__(convert_charrefs=True)
        self.episode_id = str(episode_id)
        self.stack = []
        self.body_depth = None
        self.body_count = self.episode_count = 0
        self.closed = False
        self.parts = []
        self.illustrations = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        scoped = attrs.get('id') == 'episode-' + self.episode_id or bool(self.stack and self.stack[-1][1])
        if attrs.get('id') == 'episode-' + self.episode_id:
            self.episode_count += 1
        if attrs.get('id') == 'epub-' + self.episode_id:
            if not scoped or tag != 'div' or 'ep-epub-content' not in attrs.get('class', '').split():
                raise incomplete()
            self.body_count += 1
            if self.body_count != 1 or self.body_depth is not None:
                raise incomplete()
            self.body_depth = len(self.stack)
            safe_style(attrs.get('style'))
            if 'hidden' in attrs:
                raise incomplete()
        elif self.body_depth is not None:
            if tag not in TEXT_TAGS | {'img'} or 'hidden' in attrs:
                raise incomplete()
            style = safe_style(attrs.get('style'))
            attr = f' style="{xml_text(style)}"' if style else ''
            if tag == 'img':
                source = attrs.get('src', '')
                match = re.fullmatch(r'data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/=\s]+)', source)
                if not match:
                    raise incomplete('illustration_source_unsupported')
                if 'srcset' in attrs:
                    raise incomplete('illustration_source_ambiguous')
                if len(self.illustrations) >= MAX_ILLUSTRATIONS:
                    raise incomplete('illustration_count_limit')
                try:
                    encoded = re.sub(r'\s', '', match[2])
                    if len(encoded) > 4 * ((MAX_ILLUSTRATION_BYTES + 2) // 3):
                        raise incomplete('illustration_size_limit')
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise incomplete('illustration_base64_invalid') from None
                self.illustrations.append(data)
                try:
                    name = illustration_names(tuple(self.illustrations))[-1]
                except ArchiveError:
                    raise incomplete('illustration_format_incomplete') from None
                # Editors sometimes label a complete PNG as JPEG. Package the
                # original bytes using their verified format, never that label.
                alt = attrs.get('alt', '')
                self.parts.append(f'<img src="{name}" alt="{xml_text(alt)}"{attr} />')
            else:
                self.parts.append(f'<{tag}{attr}' + (' />' if tag in VOID_TAGS else '>'))
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                       'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, scoped))
        if len(self.stack) > 128:
            raise incomplete()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS | {'img'}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.body_depth is not None:
            if not self.stack or self.stack[-1][0] != tag:
                raise incomplete()
            if len(self.stack) - 1 == self.body_depth:
                self.body_depth = None
                self.closed = True
            else:
                self.parts.append(f'</{tag}>')
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.body_depth is not None:
            self.parts.append(xml_text(data, quote=False))

    def text(self):
        if self.episode_count != 1 or self.body_count != 1 or not self.closed or self.body_depth is not None:
            raise incomplete()
        fragment = ''.join(self.parts)
        try:
            validate_fragment(fragment, tuple(self.illustrations))
        except ArchiveError:
            raise incomplete() from None
        return fragment


def novel_structure(html, episode_id):
    """Describe only the requested chapter up to the parser's first rejection.

    Counts and fixed tag names explain holds without exporting prose, URLs,
    attributes or unrelated account/reader content. This grants no new support.
    """
    known = TEXT_TAGS | {'a', 'img', 'table', 'thead', 'tbody', 'tr', 'td', 'th',
                         'script', 'style', 'iframe', 'svg', 'video', 'audio',
                         'font', 'small', 'center', 'code', 'pre', 'figure', 'figcaption',
                         'o:p', 'ins', 'strike', 'tt', 'cite', 'q'}

    class Structure(NovelHTML):
        def __init__(self):
            super().__init__(episode_id)
            self.tags = {}
            self.rejected_tag = None
            self.hidden_content = False
            self.media_shapes = []

        def handle_starttag(self, tag, attrs):
            if self.body_depth is not None:
                safe_tag = tag if tag in known else 'other'
                self.tags[safe_tag] = self.tags.get(safe_tag, 0) + 1
                if tag not in TEXT_TAGS and self.rejected_tag is None:
                    self.rejected_tag = safe_tag
                values = dict(attrs)
                if tag == 'img':
                    from .transport import asset_url
                    try:
                        source = asset_url(values.get('src', ''))
                        parsed = urlsplit(source)
                        self.media_shapes.append({'host': parsed.hostname,
                                                  'has_query': bool(parsed.query),
                                                  'source_attribute': 'src'})
                    except (ArchiveError, ValueError, TypeError):
                        source = values.get('src', '')
                        kind = ('embedded_image' if source.startswith('data:image/') else
                                'https_external' if source.startswith('https://') else
                                'protocol_relative' if source.startswith('//') else
                                'missing' if not source else 'other')
                        self.media_shapes.append({'source_supported': False, 'source_kind': kind})
                if 'hidden' in values:
                    self.hidden_content = True
                try:
                    safe_style(values.get('style'))
                except UnsupportedContent:
                    self.hidden_content = True
            super().handle_starttag(tag, attrs)

    parser = Structure()
    supported = False
    failure = None
    characters = size = None
    try:
        parser.feed(html)
        fragment = parser.text()
        characters = len(''.join(validate_fragment(fragment, tuple(parser.illustrations)).itertext()))
        size = len(fragment.encode('utf-8'))
        supported = True
    except UnsupportedContent as error:
        failure = error.novel_reason
    return {'episode_container_count': parser.episode_count,
            'chapter_container_count': parser.body_count,
            'chapter_closed': parser.closed, 'supported_text_manifest': supported,
            'first_unsupported_tag': None if supported else parser.rejected_tag,
            'hidden_content_detected': parser.hidden_content,
            'observed_tag_counts': dict(sorted(parser.tags.items())),
            'scoped_image_source_shapes': parser.media_shapes,
            'text_character_count': characters, 'text_utf8_bytes': size,
            'embedded_illustration_count': len(parser.illustrations),
            'validation_failure': failure,
            'observation_scope': 'requested_chapter_until_first_rejection',
            'media_requested': False, 'chapter_text_exported': False}


def epub_entries(series, episode, modified, *, export_id=None):
    names = illustration_names(episode.illustrations)
    validate_fragment(episode.text_html, episode.illustrations)
    media_items = ''.join(f'<item id="image-{index:04d}" href="{name}" media-type="{IMAGE_TYPES["." + name.rsplit(".", 1)[1]]}" />'
                          for index, name in enumerate(names, 1))
    title = xml_text(series.title + ' — ' + episode.title)
    chapter_title = xml_text(episode.title)
    identity = xml_text('urn:uuid:' + export_id if export_id else f'{series.source}:{series.id}:{episode.id}')
    creators = ''.join(f'<dc:creator>{xml_text(name)}</dc:creator>' for name in series.creators)
    head = '<?xml version="1.0" encoding="utf-8"?>'
    entries = {
        'mimetype': b'application/epub+zip',
        'META-INF/container.xml': (head + '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml" /></rootfiles></container>').encode(),
        'EPUB/package.opf': (head + f'<package xmlns="{OPF}" version="3.0" unique-identifier="book-id"><metadata xmlns:dc="{DC}"><dc:identifier id="book-id">{identity}</dc:identifier><dc:title>{title}</dc:title><dc:language>und</dc:language>{creators}<meta property="dcterms:modified">{modified}</meta></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" /><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />{media_items}</manifest><spine><itemref idref="chapter" /></spine></package>').encode(),
        'EPUB/chapter.xhtml': (head + f'<html xmlns="{XHTML}" xml:lang="und"><head><title>{chapter_title}</title></head><body><h1>{chapter_title}</h1><div id="chapter">{episode.text_html}</div></body></html>').encode(),
        'EPUB/nav.xhtml': (head + f'<html xmlns="{XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="und"><head><title>Contents</title></head><body><nav epub:type="toc"><h1>Contents</h1><ol><li><a href="chapter.xhtml">{chapter_title}</a></li></ol></nav></body></html>').encode(),
    }
    entries.update({'EPUB/' + name: data for name, data in zip(names, episode.illustrations, strict=True)})
    return entries


def epub_files(meta):
    count = meta.get('illustration_count', 0)
    if type(count) is not int or not 0 <= count <= MAX_ILLUSTRATIONS:
        raise ValueError
    names = [item['file'] for item in meta['content_hashes']][len(EPUB_FILES):]
    if len(names) != count:
        raise ValueError
    for index, name in enumerate(names, 1):
        if not re.fullmatch(rf'EPUB/images/{index:04d}\.(jpg|png|gif|webp)', name):
            raise ValueError
    sizes = [item['size'] for item in meta['content_hashes'][len(EPUB_FILES):]]
    if any(type(size) is not int or size < 1 for size in sizes) or sum(sizes) > MAX_ILLUSTRATION_BYTES:
        raise ValueError
    return [*EPUB_FILES, *names]


def verify_epub(z, meta):
    """Check fixed package structure and passive text after file hashes match."""
    expected = epub_files(meta)
    illustrations = tuple(z.read(name) for name in expected[len(EPUB_FILES):])
    names = illustration_names(illustrations)
    if (expected != [*EPUB_FILES, *('EPUB/' + name for name in names)]
            or z.namelist() != expected or meta.get('page_count') != 0
            or meta.get('chapter_count') != 1 or meta.get('content_type') != 'novel'
            or z.read('mimetype') != b'application/epub+zip'
            or z.getinfo('mimetype').compress_type != zipfile.ZIP_STORED
            or z.getinfo('mimetype').extra):
        raise ValueError
    roots = {}
    for name in EPUB_FILES[1:]:
        data = z.read(name)
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError
        roots[name] = ET.fromstring(data)
    container = roots['META-INF/container.xml']
    rootfiles = container.findall('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')
    if len(rootfiles) != 1 or rootfiles[0].get('full-path') != 'EPUB/package.opf':
        raise ValueError
    package = roots['EPUB/package.opf']
    items = package.findall(f'{{{OPF}}}manifest/{{{OPF}}}item')
    if (package.get('version') != '3.0' or
            [(i.get('id'), i.get('href'), i.get('media-type'), i.get('properties')) for i in items] !=
            [('chapter', 'chapter.xhtml', 'application/xhtml+xml', None),
             ('nav', 'nav.xhtml', 'application/xhtml+xml', 'nav')] +
             [(f'image-{i:04d}', name, IMAGE_TYPES['.' + name.rsplit('.', 1)[1]], None)
              for i, name in enumerate(names, 1)] or
            [i.get('idref') for i in package.findall(f'{{{OPF}}}spine/{{{OPF}}}itemref')] != ['chapter']):
        raise ValueError
    identifier = package.find(f'{{{OPF}}}metadata/{{{DC}}}identifier')
    expected_identity = ('urn:uuid:' + meta['export_id'] if meta.get('export_id')
                         else f"{meta['source']}:{meta['series_id']}:{meta['source_episode_id']}")
    if identifier is None or identifier.text != expected_identity:
        raise ValueError
    chapter = roots['EPUB/chapter.xhtml'].find(f'{{{XHTML}}}body/{{{XHTML}}}div')
    if chapter is None or chapter.get('id') != 'chapter':
        raise ValueError
    if len(''.join(chapter.itertext())) != meta.get('character_count'):
        raise ValueError
    content = z.read('EPUB/chapter.xhtml').split(b'<div id="chapter">', 1)[1].removesuffix(b'</div></body></html>')
    validate_fragment(content.decode('utf-8'), illustrations)
    if len(content) != meta.get('text_size') or hashlib.sha256(content).hexdigest() != meta.get('text_sha256'):
        raise ValueError
    for name in ('EPUB/chapter.xhtml', 'EPUB/nav.xhtml'):
        for node in roots[name].iter():
            tag = node.tag.removeprefix('{' + XHTML + '}')
            if tag not in TEXT_TAGS | {'html', 'head', 'title', 'body', 'nav', 'a', 'img'}:
                raise ValueError
            for key, value in node.attrib.items():
                if key == 'style':
                    if safe_style(value) != value:
                        raise ValueError
                elif tag == 'img' and (key == 'alt' or key == 'src' and value in names):
                    pass
                elif (key, value) not in {('id', 'chapter'), ('href', 'chapter.xhtml'),
                        ('{http://www.w3.org/XML/1998/namespace}lang', 'und'),
                        ('{http://www.idpf.org/2007/ops}type', 'toc')}:
                    raise ValueError
