from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import base64

from comic_archive.adapters.tapas_http import TapasHTTPAdapter, access_state
from comic_archive.publication import PublicationStore as Store
from comic_archive.archive import digest, verify, verify_episode, verify_episode_contents
from comic_archive.audit import audit_library
from comic_archive.backup import backup_library
from comic_archive.errors import AccessDenied, UnsupportedContent
from comic_archive.hosted import WorkspaceRegistry
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered, sync, DownloadBudget
from comic_archive.models import ArchiveError, Episode, Manifest, Series
from comic_archive.novels import EPUB_FILES, NovelHTML, epub_entries, epub_files, novel_structure
from scripts.offline_restore import restore


INFO = {'id': 81, 'title': 'Chapter & one', 'scene': 1, 'book': True, 'free': True,
        'unlocked': False, 'must_pay': False, 'scheduled': False, 'closing_date': None}
HTML = '''<script>var seriesId=42;</script><div id="episode-82"><div id="epub-82"
class="ep-epub-content"><p>Other chapter.</p></div></div><div id="episode-81">
<p>Reader header</p><article class="viewer__body ep-epub-contents">
<div id="epub-81" class="ep-epub-content"><div><p>First &amp; second.</p>
<p><span style="font-style: italic; background: url(https://invalid.example/secret)">Words</span>
<strong>bold</strong><br/>尾声<sup>1</sup></p></div></div></article>
<div>Comments and account controls.</div></div>'''


def fragment():
    parser = NovelHTML('81')
    parser.feed(HTML)
    return parser.text()


def envelope(value):
    return json.dumps({'data': value, 'error_details': None}).encode()


class NovelAdapter:
    supports_novels = True
    supports_comic_series_classification = True
    supports_series_metadata = False

    def __init__(self):
        self.denied = set()
        self.calls = []
        self.access = {'81': 'tapas_free', '82': 'tapas_unlocked'}
        self.series = Series('novel', 'Novel', platform_id='42')

    def account_identity(self):
        return '11'

    def episode_url(self, key):
        return 'https://tapas.io/episode/' + key

    def episode_access(self, url):
        key = url.rsplit('/', 1)[1]
        if key in self.denied:
            raise AccessDenied('Current access denied.')
        return {'access_state': self.access[key]}

    def capture_automatic(self, url):
        key = url.rsplit('/', 1)[1]
        self.calls.append(key)
        access = self.episode_access(url)['access_state']
        chapter = Episode(key, self.series.id, 'Chapter ' + key, int(key)-80,
                          (), access, url, content_type='novel', text_html=fragment())
        return Manifest(self.series, (chapter,))

    def retrieve(self, *args, **kwargs):
        raise AssertionError('Text chapters must not request comic assets.')

    def inspect_series(self, key, strict_order=False, comic_only=False):
        if comic_only:
            raise AssertionError('Novel-capable discovery must enumerate novel series.')
        return {'authenticated': True, 'complete': True, 'source_series_id': key,
                'title': 'Novel', 'episodes': [
                    {'id': k, 'title': 'Chapter ' + k, 'order': int(k)-80, 'access_state': access}
                    for k, access in self.access.items()]}


class NovelTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / 'Library'
        self.adapter = NovelAdapter()

    def seed(self, legacy=False):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Novel', [
                {'id': key, 'title': 'Chapter ' + key, 'order': int(key)-80,
                 'access_state': 'unavailable_or_unverified' if legacy else access,
                 'authorization_reason': 'novel' if legacy else None}
                for key, access in self.adapter.access.items()], 'sync')
            if legacy:
                inventory.record_novel_series_hold('42')

    def test_exact_closed_scope_preserves_unicode_paragraphs_and_inline_formatting(self):
        text = fragment()
        self.assertIn('First &amp; second.', text)
        self.assertIn('font-style: italic', text)
        self.assertIn('<strong>bold</strong><br />尾声<sup>1</sup>', text)
        for unrelated in ('Other chapter', 'Reader header', 'Comments', 'account', 'invalid.example'):
            self.assertNotIn(unrelated, text)

    def test_missing_wrong_duplicate_truncated_hidden_and_media_chapters_hold(self):
        for html in (HTML.replace('epub-81', 'epub-99'),
                     HTML.replace('episode-81', 'episode-99'),
                     HTML[:HTML.index('</div></article>')],
                     HTML + HTML,
                     HTML.replace('<strong>', '<img src="https://invalid.example/a"/><strong>'),
                     HTML.replace('<strong>', '<script>run()</script><strong>'),
                     HTML.replace('<strong>', '<iframe src="https://invalid.example"/><strong>'),
                     HTML.replace('<strong>', '<strong hidden>'),
                     HTML.replace('<strong>', '<strong style="display:none">')):
            with self.subTest(html=html[-80:]):
                parser = NovelHTML('81')
                with self.assertRaises(UnsupportedContent) as error:
                    parser.feed(html)
                    parser.text()
                self.assertEqual(error.exception.code, 'novel_manifest_incomplete')

    def test_structure_inspection_counts_only_requested_chapter_and_exports_no_prose(self):
        result = novel_structure(HTML, '81')
        self.assertTrue(result['supported_text_manifest'])
        self.assertTrue(result['chapter_closed'])
        self.assertEqual(result['episode_container_count'], 1)
        self.assertEqual(result['chapter_container_count'], 1)
        self.assertEqual(result['observed_tag_counts']['p'], 2)
        self.assertFalse(result['media_requested'])
        self.assertFalse(result['chapter_text_exported'])
        for private in ('Words', 'Comments', 'account', 'https', 'secret', 'Other chapter'):
            self.assertNotIn(private, json.dumps(result))

    def test_structure_inspection_reports_first_unsupported_tag_without_values(self):
        for tag, expected in (('img', 'img'), ('a', 'a'), ('private-session-token', 'other')):
            html = HTML.replace('<strong>', f'<{tag} src="https://private/secret">PRIVATE</{tag}><strong>')
            result = novel_structure(html, '81')
            self.assertFalse(result['supported_text_manifest'])
            self.assertEqual(result['first_unsupported_tag'], expected)
            self.assertIsNone(result['text_character_count'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('private', json.dumps(result))

    def test_structure_inspection_keeps_hidden_and_truncated_chapters_held(self):
        result = novel_structure(HTML.replace('<strong>', '<strong style="display:none">'), '81')
        self.assertFalse(result['supported_text_manifest'])
        self.assertTrue(result['hidden_content_detected'])
        result = novel_structure(HTML[:HTML.index('</div></article>')], '81')
        self.assertFalse(result['supported_text_manifest'])
        self.assertFalse(result['chapter_closed'])

    def test_embedded_illustrations_preserve_bytes_and_strikethrough_offline(self):
        from PIL import Image
        pixels = io.BytesIO()
        Image.new('RGB', (2, 3), 'red').save(pixels, format='PNG')
        image = pixels.getvalue()
        source = 'data:image/png;base64,' + base64.b64encode(image).decode()
        html = HTML.replace('<strong>bold</strong>', f'<strike>old</strike><img src="{source}" alt="A &amp; B"/>')
        parser = NovelHTML('81')
        parser.feed(html)
        chapter = Episode('81', 'novel', 'Illustrated', 1, (), 'tapas_free',
                          content_type='novel', text_html=parser.text(), illustrations=tuple(parser.illustrations))
        self.assertEqual(chapter.illustrations, (image,))
        self.assertIn('<strike>old</strike>', chapter.text_html)
        self.assertNotIn('data:image', chapter.text_html)
        self.assertIn('images/0001.png', chapter.text_html)
        store = Store(self.root)
        try:
            store.archive(Manifest(self.adapter.series, (chapter,)), self.adapter, progress=lambda _: None)
        finally:
            store.close()
        path = next(self.root.glob('*/*.epub'))
        self.assertEqual(verify(self.root, decode_images=True), [])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('EPUB/images/0001.png'), image)
            self.assertIn(b'media-type="image/png"', archive.read('EPUB/package.opf'))
        restored = restore(self.root, Path(self.work.name) / 'Restore', decode=True)
        self.assertEqual(restored['image_count'], 1)
        self.assertEqual(restored['novel_chapter_count'], 1)
        self.assertEqual(restored['original_image_bytes'], len(image))
        budget = DownloadBudget(self.adapter, max_pages=1)
        with self.assertRaises(ArchiveError):
            budget.consume_novel(chapter)
        budget = DownloadBudget(self.adapter, max_pages=2, max_bytes=len(image)+len(chapter.text_html.encode()))
        budget.consume_novel(chapter)
        self.assertEqual(budget.pages, 2)
        self.assertEqual(budget.chapters, 1)

    def test_embedded_media_requires_complete_declared_supported_images(self):
        for source in ('data:image/png;base64,%%%%', 'data:image/png;base64,SGVsbG8=',
                       'https://tapas.io/remote.png', 'data:image/svg+xml;base64,PHN2Zy8+'):
            parser = NovelHTML('81')
            with self.assertRaises(UnsupportedContent):
                parser.feed(HTML.replace('<strong>', f'<img src="{source}"/><strong>'))
                parser.text()

    def test_illustration_failure_diagnostics_explain_holds_without_payloads(self):
        html = HTML.replace('<strong>', '<img src="data:image/png;base64,SGVsbG8="/><strong>')
        result = novel_structure(html, '81')
        self.assertEqual(result['validation_failure'], 'illustration_format_incomplete')
        self.assertNotIn('SGVsbG8', json.dumps(result))
        self.assertNotIn('Hello', json.dumps(result))

    def test_mislabeled_embedded_bitmap_uses_its_original_verified_format(self):
        from PIL import Image
        pixels = io.BytesIO()
        Image.new('RGB', (2, 3), 'red').save(pixels, format='PNG')
        data = pixels.getvalue()
        source = 'data:image/jpeg;base64,' + base64.b64encode(data).decode()
        parser = NovelHTML('81')
        parser.feed(HTML.replace('<strong>', f'<img src="{source}"/><strong>'))
        self.assertIn('images/0001.png', parser.text())
        self.assertEqual(tuple(parser.illustrations), (data,))

    def test_illustration_reference_must_match_the_complete_ordered_manifest(self):
        from comic_archive.novels import validate_fragment
        with self.assertRaises(ArchiveError):
            validate_fragment('<p>Text<img src="images/0001.png" /></p>')
        with self.assertRaises(ArchiveError):
            Episode('81', 'novel', 'Chapter', 1, (), 'tapas_free', content_type='novel',
                    text_html='<p>Text</p>', illustrations=(b'not an image',))

    def test_novel_authorization_requires_current_free_or_unlock_and_keeps_other_holds(self):
        self.assertEqual(access_state(INFO), 'tapas_free')
        self.assertEqual(access_state(INFO | {'free': False, 'unlocked': True}), 'tapas_unlocked')
        for updates in ({'free': False}, {'must_pay': True}, {'scheduled': True},
                        {'closing_date': 'temporary'}, {'unlocked': 1}, {'book': 'true'}):
            with self.assertRaises(ArchiveError):
                access_state(INFO | updates)

    def test_http_capture_checks_identity_access_and_novel_series_before_export(self):
        transport = Mock()
        transport.get.side_effect = [envelope({'user': {'id': 11}}), envelope(INFO), HTML.encode(),
                                    envelope({'id': 42, 'title': 'Novel', 'url': 'novel', 'book': True, 'comic': False})]
        manifest = TapasHTTPAdapter(transport=transport).capture('https://tapas.io/episode/81')
        self.assertEqual(manifest.episodes[0].content_type, 'novel')
        self.assertEqual(manifest.episodes[0].pages, ())
        self.assertEqual(manifest.episodes[0].text_html, fragment())
        self.assertNotIn('First &', repr(manifest))
        self.assertEqual([c.args[0] for c in transport.get.call_args_list], [
            'https://tapas.io/who-am-i', 'https://tapas.io/episode/81/info',
            'https://tapas.io/episode/81', 'https://tapas.io/series/42'])

    def test_locked_novel_stops_before_reader_and_wrong_series_content_stops_capture(self):
        for updates in ({'must_pay': True}, {'free': False}, {'scheduled': True}, {'closing_date': 'temporary'}):
            transport = Mock()
            transport.get.side_effect = [envelope({'user': {'id': 11}}), envelope(INFO | updates)]
            with self.assertRaises(ArchiveError):
                TapasHTTPAdapter(transport=transport).capture('https://tapas.io/episode/81')
            self.assertEqual(transport.get.call_count, 2)
        transport = Mock()
        transport.get.side_effect = [envelope({'user': {'id': 11}}), envelope(INFO), HTML.encode(),
                                    envelope({'id': 42, 'title': 'Comic', 'url': 'comic', 'book': False, 'comic': True})]
        with self.assertRaises(ArchiveError):
            TapasHTTPAdapter(transport=transport).capture('https://tapas.io/episode/81')

    def test_unexpected_series_content_flags_hold_before_metadata_html(self):
        for flags in ({'book': {}, 'comic': False}, {'book': True, 'comic': []},
                      {'book': True, 'comic': True}, {'book': 1, 'comic': False}):
            transport = Mock()
            transport.get.side_effect = [envelope({'user': {'id': 11}}),
                                        envelope({'id': 42, 'title': 'Novel', 'url': 'novel'} | flags)]
            with self.assertRaises(UnsupportedContent):
                TapasHTTPAdapter(transport=transport).series_metadata('42')
            self.assertEqual(transport.get.call_count, 2)

    @patch('builtins.print')
    def test_legacy_novel_holds_require_fresh_authorization_and_never_grant_access(self, output):
        self.seed(legacy=True)
        self.adapter.denied.add('82')
        result = archive_discovered(self.adapter, self.root, download_workers=2)
        self.assertEqual(result['archived_episode_ids'], ['81'])
        self.assertEqual(result['unavailable_episode_ids'], ['82'])
        self.assertEqual(result['downloaded_chapters'], 1)
        self.assertEqual(result['downloaded_pages'], 1)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(dict(inventory.db.execute('SELECT id,status FROM episodes')), {'81': 'archived', '82': 'unavailable'})
            self.assertEqual(inventory.status()['series_without_scan_checkpoint_count'], 1)
        self.assertEqual(verify(self.root, decode_images=True), [])

    @patch('builtins.print')
    def test_chapter_limit_and_byte_limit_retain_unfinished_candidates(self, output):
        self.seed()
        first = archive_discovered(self.adapter, self.root, max_pages=1, download_workers=2)
        self.assertEqual(first['archived_episode_ids'], ['82'])
        self.assertEqual(first['downloaded_chapters'], 1)
        second = archive_discovered(self.adapter, self.root, max_bytes=1)
        self.assertEqual(second['archived_episode_ids'], [])
        self.assertEqual(second['downloaded_pages'], 0)
        self.assertEqual(second['downloaded_bytes'], 0)
        third = archive_discovered(self.adapter, self.root, max_bytes=len(fragment().encode()))
        self.assertEqual(third['archived_episode_ids'], ['81'])
        self.assertEqual(third['downloaded_chapters'], 1)
        self.assertEqual(verify(self.root), [])

    @patch('builtins.print')
    def test_novel_series_discovery_removes_the_old_hold_and_daily_preserves_only_free(self, output):
        self.seed(legacy=True)
        result = sync(self.adapter, self.root, refresh_lists=False, free_only=True)
        self.assertEqual(result['archived_episode_ids'], ['81'])
        self.assertEqual(result['refresh']['successful_series_checks'], 1)
        self.assertEqual(result['refresh']['series_scan_gaps'], [])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.pending()[0]['id'], '82')

    @patch('builtins.print')
    def test_fast_metadata_scan_preserves_access_checks_without_assets_and_restores_pacing(self, output):
        self.seed()
        self.adapter.metadata_interval = 1.25
        intervals = []
        original = self.adapter.inspect_series
        def inspect(*args, **kwargs):
            intervals.append(self.adapter.metadata_interval)
            return original(*args, **kwargs)
        self.adapter.inspect_series = inspect
        result = sync(self.adapter, self.root, refresh_lists=False, scan_only=True, metadata_speed='fast')
        self.assertEqual(intervals, [.5])
        self.assertEqual(self.adapter.metadata_interval, 1.25)
        self.assertEqual(result['archived_episode_ids'], [])
        self.assertEqual(result['downloaded_pages'], 0)
        with self.assertRaises(ArchiveError):
            sync(self.adapter, self.root, refresh_lists=False, metadata_speed='fast')

    @patch('builtins.print')
    def test_novel_export_is_portable_verified_and_reruns_keep_bytes_and_import_time(self, output):
        self.seed()
        archive_discovered(self.adapter, self.root, limit=1)
        path = next(self.root.glob('*/*.epub'))
        before = (path.read_bytes(), path.with_suffix('.json').read_bytes())
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.namelist(), list(EPUB_FILES))
            self.assertEqual(z.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
            self.assertIn('尾声'.encode(), z.read('EPUB/chapter.xhtml'))
            self.assertNotIn(b'https://invalid', z.read('EPUB/chapter.xhtml'))
        store = Store(self.root)
        try:
            store.archive(self.adapter.capture_automatic('https://tapas.io/episode/82'), self.adapter, progress=lambda _: None)
        finally:
            store.close()
        self.assertEqual((path.read_bytes(), path.with_suffix('.json').read_bytes()), before)
        self.assertEqual(verify_episode(path), [])

    @patch('builtins.print')
    def test_novel_integrity_failure_and_unsafe_output_are_detected(self, output):
        self.seed()
        archive_discovered(self.adapter, self.root, limit=1)
        path = next(self.root.glob('*/*.epub'))
        metadata = json.loads(path.with_suffix('.json').read_text())
        with zipfile.ZipFile(path) as z:
            members = {name: z.read(name) for name in z.namelist()}
        members['EPUB/chapter.xhtml'] = members['EPUB/chapter.xhtml'].replace(b'<strong>', b'<script>')
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in members.items():
                z.writestr(name, data)
        for item in metadata['content_hashes']:
            item.update(size=len(members[item['file']]), sha256=digest(members[item['file']]))
        path.with_suffix('.json').write_text(json.dumps(metadata))
        self.assertTrue(verify_episode(path))
        with Inventory(self.root) as inventory:
            inventory.reconcile(full=True)
            self.assertEqual(inventory.pending()[0]['error_code'], 'archive_corrupt')

    @patch('builtins.print')
    def test_novel_audit_backup_and_independent_restore(self, output):
        self.seed()
        archive_discovered(self.adapter, self.root)
        audit = audit_library(self.root)
        self.assertEqual(audit['verified_inventory_episode_count'], 2)
        self.assertTrue(audit['archive_integrity_passed'])
        backup = Path(self.work.name) / 'backup'
        self.assertEqual(backup_library(self.root, backup, reserve_bytes=0)['archive_count'], 2)
        self.assertEqual(verify(backup), [])
        report = restore(backup, Path(self.work.name)/'restore', decode=True)
        self.assertEqual(report['novel_chapter_count'], 2)
        self.assertEqual(report['image_count'], 0)
        self.assertFalse(report['application_imported'])

    @patch('builtins.print')
    def test_novel_and_sidecar_downloads_are_verified_and_owner_scoped(self, output):
        registry = WorkspaceRegistry(Path(self.work.name)/'hosted')
        workspace = registry.create('owner')
        self.root = workspace.library
        self.seed()
        archive_discovered(self.adapter, self.root, limit=1)
        exports = registry.exports('owner', workspace.id)
        for kind in ('epub', 'episode_metadata'):
            export = next(item for item in exports if item['kind'] == kind)
            with registry.open_export('owner', workspace.id, export['id']) as download:
                data = download.stream.read()
                self.assertEqual(len(data), export['size'])
            with self.assertRaises(ArchiveError):
                registry.open_export('another-owner', workspace.id, export['id'])

    def test_model_refuses_active_or_empty_text_and_mixed_comic_text(self):
        for text in ('', '<p></p>', '<script>bad()</script>', '<p onclick="bad()">Text</p>'):
            with self.assertRaises(ArchiveError):
                Episode('81', '42', 'E', 1, (), 'tapas_free', content_type='novel', text_html=text)

    def test_text_and_illustration_limits_refuse_oversized_unreferenced_and_misordered_data(self):
        from test_archive import png
        from comic_archive import novels
        from comic_archive.novels import MAX_ILLUSTRATION_BYTES, MAX_TEXT_BYTES, illustration_names, validate_fragment
        with self.assertRaises(ArchiveError): validate_fragment('<p>' + 'a' * MAX_TEXT_BYTES + '</p>')
        with self.assertRaises(ArchiveError): validate_fragment(b'<p>text</p>')
        for bad in ([png(1)], (b'',), ('text',), (b'\0' * (MAX_ILLUSTRATION_BYTES + 1),)):
            with self.subTest(bad=str(bad)[:30]), self.assertRaises(ArchiveError): illustration_names(bad)
        one, two = png(1), png(2)
        with patch.object(novels, 'MAX_ILLUSTRATIONS', 1), self.assertRaises(ArchiveError):
            illustration_names((one, one))
        with patch.object(novels, 'MAX_ILLUSTRATION_BYTES', len(one) * 2 - 1), self.assertRaises(ArchiveError):
            illustration_names((one, one))
        self.assertEqual(illustration_names((one, one)), ['images/0001.png', 'images/0002.png'])
        validate_fragment('<p>Text<img src="images/0001.png" /><img src="images/0002.png" /></p>', (one, two))
        for fragment_text, images in (('<p>Text</p>', (one,)),
                ('<p><img src="images/0002.png" /><img src="images/0001.png" /></p>', (one, two)),
                ('<p style="color: red">Text</p>', ())):
            with self.subTest(fragment=fragment_text), self.assertRaises(ArchiveError):
                validate_fragment(fragment_text, images)
        validate_fragment('<p style="font-style: italic">Text</p>')

    def test_ambiguous_invalid_or_too_many_embedded_images_hold_the_chapter_with_a_reason(self):
        from test_archive import png
        from comic_archive import novels
        source = 'data:image/png;base64,' + base64.b64encode(png(1)).decode()
        image = f'<img src="{source}"/>'
        cases = (('illustration_source_ambiguous', f'<img src="{source}" srcset="https://invalid.example/a.png"/>', None, 0),
                 ('illustration_base64_invalid', '<img src="data:image/png;base64,AAAAA"/>', None, 0),
                 ('illustration_count_limit', image + image, 'MAX_ILLUSTRATIONS', 1),
                 ('illustration_size_limit', image, 'MAX_ILLUSTRATION_BYTES', 30))
        for reason, markup, constant, value in cases:
            html = HTML.replace('<strong>', markup + '<strong>')
            limit = patch.object(novels, constant, value) if constant else nullcontext()
            with self.subTest(reason=reason), limit:
                with self.assertRaises(UnsupportedContent) as error:
                    parser = NovelHTML('81')
                    parser.feed(html)
                self.assertEqual(error.exception.novel_reason, reason)
                self.assertEqual(novel_structure(html, '81')['validation_failure'], reason)

    def test_hidden_deeply_nested_or_mismatched_chapter_markup_holds_but_stray_outside_tags_do_not(self):
        container = '<div id="epub-81" class="ep-epub-content">'
        for html in (HTML.replace(container, container[:-1] + ' hidden>'),
                     HTML.replace('<p>First &amp; second.</p>',
                                  '<div>' * 130 + '<p>First &amp; second.</p>' + '</div>' * 130),
                     HTML.replace('<strong>bold</strong>', '<strong>bold</em>')):
            with self.subTest(html=html[-60:]), self.assertRaises(UnsupportedContent) as error:
                parser = NovelHTML('81')
                parser.feed(html)
                parser.text()
            self.assertEqual(error.exception.code, 'novel_manifest_incomplete')
        noisy = '</section>' + HTML.replace('<p>Reader header</p>', '<p>Reader header</i></p></b>')
        self.assertEqual(self.fragment_of(noisy), fragment())

    def fragment_of(self, html):
        parser = NovelHTML('81')
        parser.feed(html)
        return parser.text()

    def test_self_closed_text_tags_stay_balanced_and_an_empty_chapter_is_held(self):
        self.assertIn('<span></span>bold<br />', self.fragment_of(HTML.replace('<strong>bold</strong>', '<span/>bold')))
        empty = '<div id="episode-81"><div id="epub-81" class="ep-epub-content">  </div></div>'
        with self.assertRaises(UnsupportedContent) as error:
            self.fragment_of(empty)
        self.assertEqual(error.exception.code, 'novel_manifest_incomplete')
        result = novel_structure(empty, '81')
        self.assertFalse(result['supported_text_manifest'])
        self.assertTrue(result['chapter_closed'])

    def test_structure_inspection_describes_tapas_hosted_images_by_shape_and_flags_hidden_attributes(self):
        result = novel_structure(HTML.replace('<strong>', '<img src="https://us-a.tapas.io/pc/a.png?signature=PRIVATE"/><strong>'), '81')
        self.assertEqual(result['scoped_image_source_shapes'],
                         [{'host': 'us-a.tapas.io', 'has_query': True, 'source_attribute': 'src'}])
        self.assertFalse(result['supported_text_manifest'])
        self.assertFalse(result['hidden_content_detected'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertTrue(novel_structure(HTML.replace('<strong>', '<strong hidden>'), '81')['hidden_content_detected'])

    def epub_parts(self):
        series = Series('42', 'Novel')
        episode = Episode('81', '42', 'Chapter', 1, (), 'tapas_free', content_type='novel',
                          text_html='<p>Hello <em>world</em>.</p>')
        text = episode.text_html.encode()
        meta = {'format': 'epub', 'source': 'tapas', 'series_id': '42', 'source_episode_id': '81', 'page_count': 0,
                'chapter_count': 1, 'content_type': 'novel', 'character_count': len('Hello world.'),
                'text_sha256': digest(text), 'text_size': len(text)}
        return epub_entries(series, episode, '2026-01-01T00:00:00Z'), meta

    def verify_package(self, entries, meta, deflate_mimetype=False):
        meta = meta | {'content_hashes': [{'file': name, 'sha256': digest(data), 'size': len(data)}
                                          for name, data in entries.items()]}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            for name, data in entries.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                if deflate_mimetype and name == 'mimetype': info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)
        buffer.seek(0)
        return verify_episode_contents(buffer, meta)

    def test_epub_with_correct_hashes_but_altered_structure_or_metadata_fails_verification(self):
        corrupt = ['Unreadable, incomplete or corrupt archive/metadata']
        entries, meta = self.epub_parts()
        self.assertEqual(self.verify_package(entries, meta), [])
        def edited(name, old, new):
            entries, meta = self.epub_parts()
            self.assertIn(old, entries[name])
            entries[name] = entries[name].replace(old, new, 1)
            return entries, meta
        entries, meta = self.epub_parts()
        cases = {'mimetype stored compressed': (entries, meta, True),
                 'two chapters claimed': (entries, meta | {'chapter_count': 2}, False),
                 'doctype in nav': (*edited('EPUB/nav.xhtml', b'?>', b'?><!DOCTYPE html>'), False),
                 'package from another path': (*edited('META-INF/container.xml', b'EPUB/package.opf', b'EPUB/other.opf'), False),
                 'package version': (*edited('EPUB/package.opf', b'version="3.0"', b'version="2.0"'), False),
                 'identity of another episode': (entries, meta | {'source_episode_id': '82'}, False),
                 'chapter container renamed': (*edited('EPUB/chapter.xhtml', b'<div id="chapter">', b'<div id="other">'), False),
                 'wrong character count': (entries, meta | {'character_count': 99}, False),
                 'wrong text hash': (entries, meta | {'text_sha256': '0' * 64}, False),
                 'wrong text size': (entries, meta | {'text_size': 1}, False),
                 'script in nav': (*edited('EPUB/nav.xhtml', b'</nav>', b'<script></script></nav>'), False),
                 'unsafe style in nav': (*edited('EPUB/nav.xhtml', b'<li>', b'<li style="color: red">'), False),
                 'unknown attribute in nav': (*edited('EPUB/nav.xhtml', b'<li>', b'<li onclick="x()">'), False)}
        for name, (package, metadata, deflate) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.verify_package(package, metadata, deflate), corrupt)

    def test_epub_file_list_rejects_inconsistent_illustration_metadata(self):
        base = [{'file': name, 'size': 1, 'sha256': '0'} for name in EPUB_FILES]
        def image(index, extension='png', size=5):
            return {'file': f'EPUB/images/{index:04d}.{extension}', 'size': size, 'sha256': '0'}
        self.assertEqual(epub_files({'content_hashes': base}), list(EPUB_FILES))
        self.assertEqual(epub_files({'content_hashes': base + [image(1)], 'illustration_count': 1}),
                         [*EPUB_FILES, 'EPUB/images/0001.png'])
        for meta in ({'content_hashes': base, 'illustration_count': -1}, {'content_hashes': base, 'illustration_count': True},
                     {'content_hashes': base, 'illustration_count': 1001}, {'content_hashes': base + [image(1)]},
                     {'content_hashes': base + [image(2)], 'illustration_count': 1},
                     {'content_hashes': base + [image(1, 'bmp')], 'illustration_count': 1},
                     {'content_hashes': base + [image(1, size=0)], 'illustration_count': 1},
                     {'content_hashes': base + [image(1, size=True)], 'illustration_count': 1},
                     {'content_hashes': base + [image(1, size=10 * 1024 * 1024 + 1)], 'illustration_count': 1}):
            with self.subTest(meta=str(meta)[-60:]), self.assertRaises(ValueError):
                epub_files(meta)


if __name__ == '__main__':
    unittest.main()
