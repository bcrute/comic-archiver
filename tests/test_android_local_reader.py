"""Actual pinned archives/progress behind the native-only reader bridge; no network."""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from comic_archive.android_local_reader import ReaderHost
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.local_reader import ReaderBook, ReadingError, ReadingSession, UNSUPPORTED
from comic_archive.models import ArchiveError
from test_app_bridge import app_bridge
from test_local_reader import make_book, original_hashes


class AndroidReaderHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project'
        self.root.mkdir()
        self.library = self.root / 'Library'
        self.comic = make_book(self.library)
        self.novel = make_book(self.library, novel=True, key='82')
        self.registry = LocalWorkspaceRegistry(self.root)
        self.host = ReaderHost()
        self.addCleanup(self.host.close_all)
        self.token = 'a' * 32

    def identifier(self, path):
        offered = self.registry._exports(self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id))
        return next(key for key, value in offered.items() if value[0] == path)

    def open(self, path=None, token=None):
        return self.host.open(self.root, self.registry, self.identifier(path or self.comic), token or self.token)

    def test_both_formats_render_original_verified_bytes_without_network_or_source_writes(self):
        before = original_hashes(self.library)
        with patch('httpx.Client.request', side_effect=AssertionError('Reader must be offline')):
            for path in (self.comic, self.novel):
                book = self.open(path)
                result = self.host.page(self.root, self.token, 0)
                self.assertEqual(result['reader_id'], self.token)
                with closing(ReaderBook(self.library, path)) as original:
                    expected = original.render(0)
                if book['kind'] == 'cbz':
                    self.assertEqual(base64.b64decode(result['data']), expected['data'])
                else:
                    self.assertEqual(result['html'], expected['html'])
                    self.assertEqual(result['images'], {})
                self.host.close(self.root, self.token)
        self.assertEqual(original_hashes(self.library), before)

    def test_save_resume_bookmark_preferences_and_revision_use_the_shared_format(self):
        book = self.open()
        saved = self.host.save(self.root, self.token, {'unit': 1, 'offset': 4200}, bookmark=True,
                               preferences={'fit': 'page', 'text_scale': 1.5})
        self.assertEqual(saved['record']['revision'], 1)
        self.assertIsNone(saved['warning'])
        self.host.close(self.root, self.token)
        restored = self.open(token='b' * 32)
        self.assertEqual(restored['record'], saved['record'])
        self.assertEqual(restored['content_sha256'], book['content_sha256'])
        self.assertEqual(restored['record']['bookmarks'], [{'unit': 1, 'offset': 4200}])

    def test_stale_close_cannot_retire_a_new_reader_and_successful_close_retires_its_descriptor(self):
        self.open()
        old = self.host._session.book._stream
        self.open(self.novel, token='b' * 32)
        self.assertTrue(old.closed)
        current = self.host._session.book._stream
        self.assertEqual(self.host.close(self.root, self.token), {'closed': False})
        self.assertFalse(current.closed)
        self.assertEqual(self.host.close(self.root, 'b' * 32), {'closed': True})
        self.assertTrue(current.closed)

    def test_bad_root_ticket_identifier_or_reused_ticket_cannot_select_another_file(self):
        self.open()
        for root, token in ((self.root.parent, self.token), (self.root, 'b' * 32), (self.root, 'PRIVATE')):
            with self.assertRaises(ReadingError):
                self.host.page(root, token, 0)
        with self.assertRaises(ReadingError):
            self.open(self.novel)
        for identifier in ('../PRIVATE', 'f' * 64, None):
            with self.assertRaises(ArchiveError) as caught:
                self.host.open(self.root, self.registry, identifier, 'c' * 32)
            self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(self.host.page(self.root, self.token, 0)['kind'], 'cbz')

    def test_damaged_or_future_progress_stays_readable_and_untouched_until_explicit_repair_reload(self):
        book = self.open()
        record = self.library / '.archive-state/reading' / (book['content_sha256'] + '.json')
        self.host.close_all()
        record.parent.mkdir(parents=True, exist_ok=True)
        for raw in (b'{broken', b'{"v":99,"future":"retained"}'):
            record.write_bytes(raw)
            value = self.open()
            self.assertFalse(value['can_save'])
            self.assertTrue(value['warning'])
            self.host.page(self.root, self.token, 1)
            self.host.save(self.root, self.token, {'unit': 1, 'offset': 0})
            self.assertEqual(record.read_bytes(), raw)
            self.assertFalse(self.host.reload(self.root, self.token)['can_save'])
            self.host.close_all()
        record.unlink()
        self.open()
        self.assertTrue(self.host.reload(self.root, self.token)['can_save'])

    def test_conflicting_writer_is_retained_and_reload_reconciles_the_current_revision(self):
        self.open()
        with closing(ReadingSession(self.library, self.comic)) as other:
            other.save({'unit': 0, 'offset': 1000}, bookmark=True)
        result = self.host.save(self.root, self.token, {'unit': 1, 'offset': 0})
        self.assertIn('another window', result['warning'])
        loaded = self.host.reload(self.root, self.token)
        self.assertEqual(loaded['record']['position'], {'unit': 0, 'offset': 1000})
        self.assertEqual(loaded['record']['bookmarks'], [{'unit': 0, 'offset': 1000}])
        self.assertIsNone(loaded['warning'])
        self.assertEqual(self.host.save(self.root, self.token, {'unit': 1, 'offset': 100})['record']['revision'], 2)

    def test_native_payload_limit_refuses_before_decompression_and_keeps_originals(self):
        self.open()
        before = original_hashes(self.library)
        with patch('comic_archive.android_local_reader.MAX_ANDROID_RENDER', 1), patch.object(
                self.host._session.book, '_read', side_effect=AssertionError('Must refuse before reading')):
            with self.assertRaisesRegex(ReadingError, 'supported limits'):
                self.host.page(self.root, self.token, 0)
        self.assertEqual(original_hashes(self.library), before)
        self.host.close_all()
        with patch('comic_archive.android_local_reader.MAX_ANDROID_RENDER', 1), patch(
                'comic_archive.local_reader.verify_episode_contents', side_effect=AssertionError('No initial decompression')):
            with self.assertRaisesRegex(ReadingError, 'supported limits'):
                self.open()

    def test_chapter_and_illustrations_share_one_aggregate_budget_before_any_member_read(self):
        from io import BytesIO
        from PIL import Image
        from comic_archive.novels import illustration_names

        images = []
        for color in (80, 160):
            stream = BytesIO()
            Image.new('RGB', (160, 240), (color, 40, 80)).save(stream, format='PNG')
            images.append(stream.getvalue())
        illustrations = tuple(images)
        text = '<p>Illustrated words.</p>' + ''.join(f'<img src="{name}" />' for name in illustration_names(illustrations))
        path = make_book(self.library, novel=True, key='83', illustrations=illustrations, text=text)
        with closing(ReaderBook(self.library, path)) as book:
            text_size = book._members['EPUB/chapter.xhtml']['size']
            largest_image = max(row['size'] for name, row in book._members.items() if name.startswith('EPUB/images/'))
            with patch.object(book, '_read', side_effect=AssertionError('No member allocation')):
                with self.assertRaises(ReadingError) as caught:
                    book.render(0, max_bytes=text_size + largest_image)
                self.assertEqual(str(caught.exception), UNSUPPORTED)
                with self.assertRaises(ReadingError):
                    book.render(0, max_text_bytes=text_size - 1)
        self.open(path)
        value = self.host.page(self.root, self.token, 0)
        with closing(ReaderBook(self.library, path)) as book:
            expected = book.render(0)
        self.assertEqual({name: base64.b64decode(raw) for name, raw in value['images'].items()}, expected['images'])
        self.host.save(self.root, self.token, {'unit': 0, 'offset': 2500}, bookmark=True,
                       preferences={'fit': 'width', 'text_scale': 1.5})
        self.host.close_all()
        selected = self.library / '.archive-state/reader-copies' / str(uuid.uuid4()) / path.name
        selected.parent.mkdir(parents=True)
        shutil.copyfile(path, selected)
        try:
            opened = self.host.open_selected(self.root, selected.relative_to(self.library).as_posix(), self.token)
            self.assertEqual(opened['record']['position'], {'unit': 0, 'offset': 2500})
            rendered = self.host.page(self.root, self.token, 0)
            self.assertEqual({name: base64.b64decode(raw) for name, raw in rendered['images'].items()}, expected['images'])
        finally:
            self.host.close_all()
            shutil.rmtree(selected.parent)
        artifacts = os.environ.get('COMIC_ANDROID_READER_ARTIFACTS')
        if artifacts:
            target = Path(artifacts) / 'illustrated/Library'
            for source in self.library.rglob('*'):
                if (not source.is_file() or source.name == 'writer.lock'
                        or source.name.endswith(('.tmp', '-wal', '-shm'))):
                    continue
                output = target / source.relative_to(self.library)
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)

    def test_malformed_save_arguments_are_public_refusals_without_progress_writes(self):
        self.open()
        for position, bookmark, preferences in ((None, False, None), ({'unit': True, 'offset': 0}, False, None),
                ({'unit': 0, 'offset': 0}, 1, None), ({'unit': 0, 'offset': 0}, False, {'fit': 'PRIVATE'})):
            with self.assertRaises(ReadingError) as caught:
                self.host.save(self.root, self.token, position, bookmark=bookmark, preferences=preferences)
            self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertFalse((self.library / '.archive-state/reading').exists())

    def test_replacement_never_reuses_or_paints_the_prior_content_version(self):
        self.open()
        replacement = self.comic.with_suffix('.new')
        shutil.copyfile(self.comic, replacement)
        replacement.replace(self.comic)
        with self.assertRaises(ReadingError):
            self.host.page(self.root, self.token, 0)

    def test_last_supported_revision_remains_readable_and_cannot_be_overwritten_with_an_invalid_revision(self):
        book = self.open()
        path = self.library / '.archive-state/reading' / (book['content_sha256'] + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        record = book['record'] | {'revision': 2**53 - 2}
        raw = json.dumps(record).encode()
        path.write_bytes(raw)
        self.host.reload(self.root, self.token)
        self.assertTrue(self.host.page(self.root, self.token, 0))
        result = self.host.save(self.root, self.token, {'unit': 1, 'offset': 0})
        self.assertIsNotNone(result['warning'])
        self.assertEqual(path.read_bytes(), raw)

    def test_first_frozen_producer_records_keep_every_field_through_the_native_projection(self):
        self.host.close_all()
        frozen = Path(__file__).parent / 'fixtures/local-reading-v1'
        root = self.root / 'frozen'
        shutil.copytree(frozen / 'payload', root / 'Library')
        registry = LocalWorkspaceRegistry(root)
        manifest = json.loads((frozen / 'manifest.json').read_text())
        before = original_hashes(root / 'Library')
        for record in manifest['records']:
            path = root / 'Library' / record['archive']
            offered = registry._exports(registry.resolve(LOCAL_OWNER, registry.workspace_id))
            identifier = next(key for key, value in offered.items() if value[0] == path)
            value = self.host.open(root, registry, identifier, self.token)
            self.assertEqual(value['record'], json.loads((root / 'Library' / record['state']).read_text()))
            self.host.page(root, self.token, value['record']['position']['unit'])
            self.host.close_all()
        self.assertEqual(original_hashes(root / 'Library'), before)


class NativeReaderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.cleanup)
        if app_bridge._reader_host is not None:
            app_bridge._reader_host.close_all()
        app_bridge._reader_host = None
        app_bridge._workspace.update(root=None, base=None, site_key='tapas')
        app_bridge._catalog_cache.update(root=None, registry=None)
        app_bridge._browser.update(factory=None)
        self.token = 'a' * 32
        self.assertTrue(self.call('prepare_workspace', {'root': str(self.root)})['ok'])
        self.comic = make_book(self.root / 'Library')
        registry = app_bridge._catalog_registry(self.root)
        self.identifier = next(key for key, value in registry._exports(
            registry.resolve(LOCAL_OWNER, registry.workspace_id)).items() if value[0] == self.comic)

    def cleanup(self):
        from comic_archive import network_android
        if app_bridge._reader_host is not None:
            app_bridge._reader_host.close_all()
        app_bridge._reader_host = None
        network_android.configure(None)
        os.chdir(self.previous)
        self.temporary.cleanup()

    def call(self, op, args):
        return json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': op, 'args': args})))

    def args(self, **values):
        return {'root': str(self.root), 'reader_id': self.token} | values

    def test_actual_dispatch_open_page_save_reload_close_has_no_paths_sessions_or_download_calls(self):
        before = original_hashes(self.root / 'Library')
        with patch.object(app_bridge, '_cookie_seams', side_effect=AssertionError('No provider authority')):
            opened = self.call('reader_open', self.args(id=self.identifier))
            self.assertTrue(opened['ok'])
            self.assertTrue(self.call('reader_page', self.args(unit=1))['ok'])
            saved = self.call('reader_save', self.args(position={'unit': 1, 'offset': 2300}, bookmark=True))
            self.assertTrue(saved['ok'])
            self.assertEqual(self.call('reader_reload', self.args())['result']['record'], saved['result']['record'])
            self.assertNotIn(str(self.root), json.dumps(opened))
            self.assertTrue(self.call('reader_close', self.args())['result']['closed'])
        self.assertEqual(original_hashes(self.root / 'Library'), before)

    def test_selected_export_dispatch_uses_actual_private_bytes_and_retires_on_workspace_change(self):
        target = self.root / 'Library/.archive-state/reader-copies' / str(uuid.uuid4()) / self.comic.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(self.comic, target)
        choice = target.relative_to(self.root / 'Library').as_posix()
        with patch.object(app_bridge, '_cookie_seams', side_effect=AssertionError('No sign-in authority')):
            opened = self.call('reader_open', self.args(selected=choice))
            self.assertTrue(opened['ok'], opened)
            self.assertTrue(self.call('reader_page', self.args(unit=1))['ok'])
            stream = app_bridge._reader_host._session.book._stream
            self.assertFalse(self.call('reader_open', self.args(id=self.identifier, selected=choice))['ok'])
            self.assertFalse(self.call('reader_open', self.args(selected='../PRIVATE'))['ok'])
            other = self.root / 'other'
            other.mkdir()
            self.assertTrue(self.call('prepare_workspace', {'root': str(other)})['ok'])
            self.assertTrue(stream.closed)
            self.assertFalse(self.call('reader_page', self.args(unit=0))['ok'])
        self.assertFalse(target.with_suffix('.json').exists())

    def test_workspace_change_retires_the_pinned_file_and_refuses_all_old_reader_work(self):
        self.assertTrue(self.call('reader_open', self.args(id=self.identifier))['ok'])
        stream = app_bridge._reader_host._session.book._stream
        other = self.root / 'another'
        other.mkdir()
        self.assertTrue(self.call('prepare_workspace', {'root': str(other)})['ok'])
        self.assertTrue(stream.closed)
        for op, extra in [('reader_page', {'unit': 0}), ('reader_save', {'position': {'unit': 0, 'offset': 0}}),
                          ('reader_reload', {})]:
            self.assertFalse(self.call(op, self.args(**extra))['ok'])
        self.assertFalse(self.call('reader_close', self.args())['result']['closed'])

    def test_unregistered_files_unknown_fields_and_unbound_workspaces_never_get_reader_authority(self):
        other = self.root / 'PRIVATE'
        other.mkdir()
        for args in (self.args(id='../PRIVATE'), self.args(id='f' * 64),
                self.args(id=self.identifier, cookie_source='PRIVATE'),
                self.args(id=self.identifier) | {'root': str(other)}):
            answer = self.call('reader_open', args)
            self.assertFalse(answer['ok'])
            self.assertNotIn('PRIVATE', json.dumps(answer))
        self.assertIsNone(app_bridge._reader_host._session)

    def test_every_new_native_operation_has_an_explicit_bad_request_response(self):
        for op in ('reader_open', 'reader_page', 'reader_save', 'reader_reload', 'reader_close'):
            answer = self.call(op, {})
            self.assertFalse(answer['ok'])
            self.assertNotEqual(answer['code'], 'unknown_op')
            self.assertNotIn('Traceback', json.dumps(answer))

    def test_slow_archive_verification_does_not_block_pause_on_the_workspace_lock(self):
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        original = ReaderBook.__init__
        results = []

        def held(book, *args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Held archive verification was not released')
            return original(book, *args, **kwargs)

        with patch.object(ReaderBook, '__init__', held):
            opening = threading.Thread(target=lambda: results.append(self.call('reader_open', self.args(id=self.identifier))))
            opening.start()
            self.assertTrue(entered.wait(3))
            stopping = threading.Thread(target=lambda: (results.append(self.call('pause', {'root': str(self.root)})), paused.set()))
            stopping.start()
            try:
                self.assertTrue(paused.wait(3), 'Reader verification blocked Pause')
            finally:
                release.set(); opening.join(10); stopping.join(10)
        self.assertFalse(opening.is_alive()); self.assertFalse(stopping.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(value['ok'] for value in results))

    def test_workspace_transition_invalidates_an_uncommitted_open_and_retires_its_descriptor(self):
        entered, release = threading.Event(), threading.Event()
        original = ReaderBook.__init__
        books, responses = [], []
        other = self.root / 'other'
        other.mkdir()

        def held(book, *args, **kwargs):
            original(book, *args, **kwargs)
            books.append(book)
            entered.set()
            if not release.wait(10):
                raise AssertionError('Held reader was not released')

        with patch.object(ReaderBook, '__init__', held):
            opening = threading.Thread(target=lambda: responses.append(self.call('reader_open', self.args(id=self.identifier))))
            opening.start()
            self.assertTrue(entered.wait(3))
            epoch = app_bridge._reader_epoch
            retiring = threading.Event()
            host = app_bridge._reader_host
            close = host.close_all

            def retire():
                retiring.set()
                return close()

            transition = threading.Thread(target=lambda: responses.append(self.call('prepare_workspace', {'root': str(other)})))
            with patch.object(host, 'close_all', side_effect=retire):
                transition.start()
                try:
                    self.assertTrue(retiring.wait(3))
                    self.assertGreater(app_bridge._reader_epoch, epoch)
                finally:
                    release.set(); opening.join(10); transition.join(10)
        self.assertFalse(opening.is_alive()); self.assertFalse(transition.is_alive())
        self.assertGreater(app_bridge._reader_epoch, epoch)
        self.assertTrue(books[0]._stream.closed)
        self.assertIsNone(app_bridge._reader_host._session)
        self.assertEqual(sum(value['ok'] for value in responses), 1)
