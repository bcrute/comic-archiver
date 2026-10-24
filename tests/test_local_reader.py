"""Real archive/publication and portable progress boundaries; no accounts or remote sources."""
from contextlib import closing
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive import local_reader, osdep
from comic_archive.archive import Store, sidecar_path
from comic_archive.local_reader import ReaderBook, ReadingError, ReadingSession, ReadingStore, checked_record
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter, png


def make_book(root, *, novel=False, legacy=False, key='81', text=None, adapter=None, illustrations=()):
    series = Series('reader', 'Reader fixture', platform_id='42')
    episode = Episode(key, series.id, 'Saved chapter', 1,
        () if novel else (Page(1, 'https://invalid.test/1', False), Page(2, 'https://invalid.test/2', False)),
        'tapas_free', content_type='novel' if novel else 'comic',
        text_html=(text or '<h2>Chapter one</h2><p>Readable <em>words</em> &amp; punctuation.</p>') if novel else None,
        illustrations=illustrations)
    with closing((Store if legacy else PublicationStore)(root)) as store:
        folder = store.archive(Manifest(series, (episode,)), adapter or MemoryAdapter(), progress=lambda _: None)
        return store.episode_path(series, episode, folder)


def original_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and local_reader.READING_DIRECTORY not in path.as_posix()}


class LocalReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'Library'
        self.archive = make_book(self.root)

    def session(self, path=None):
        result = ReadingSession(self.root, path or self.archive)
        self.addCleanup(result.close)
        return result

    def record_path(self, session):
        return self.root / local_reader.READING_DIRECTORY / (session.book.content + '.json')

    def test_comic_reads_ordered_original_bytes_without_creating_reading_state(self):
        before = original_hashes(self.root)
        reader = self.session()
        self.assertEqual((reader.book.kind, reader.book.units), ('cbz', 2))
        self.assertEqual(reader.book.render(0)['data'], png(1))
        self.assertEqual(reader.book.render(1)['data'], png(2))
        self.assertEqual(reader.book.content, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertFalse(self.record_path(reader).exists())
        self.assertEqual(original_hashes(self.root), before)

    def test_novel_reads_verified_inert_chapter_with_no_remote_requests(self):
        path = make_book(self.root, novel=True, key='82')
        before = original_hashes(self.root)
        with patch('httpx.Client.request', side_effect=AssertionError('Reading must remain offline')):
            reader = self.session(path)
            value = reader.book.render(0)
        self.assertEqual((reader.book.kind, reader.book.units), ('epub', 1))
        self.assertIn('Readable <em>words</em>', value['html'])
        self.assertEqual(value['images'], {})
        self.assertEqual(original_hashes(self.root), before)

    def test_earlier_legacy_comic_and_novel_output_stays_readable_without_migration(self):
        for novel in (False, True):
            root = self.base / ('old-novel' if novel else 'old-comic')
            path = make_book(root, novel=novel, legacy=True)
            before = original_hashes(root)
            with closing(ReadingSession(root, path)) as reader:
                self.assertEqual(reader.book.kind, 'epub' if novel else 'cbz')
                self.assertTrue(reader.book.render(0))
            self.assertEqual(original_hashes(root), before)

    def test_position_bookmarks_and_preferences_survive_reopen_for_both_formats(self):
        for novel in (False, True):
            path = make_book(self.root, novel=True, key='82') if novel else self.archive
            reader = self.session(path)
            position = {'unit': 0 if novel else 1, 'offset': 6500}
            preferences = {'fit': 'page', 'text_scale': 1.5}
            record, warning = reader.save(position, bookmark=True, preferences=preferences)
            self.assertIsNone(warning)
            reader.close()
            reopened = self.session(path)
            self.assertEqual(reopened.record['position'], position)
            self.assertEqual(reopened.record['bookmarks'], [position])
            self.assertEqual(reopened.record['preferences'], preferences)
            self.assertEqual(reopened.record['revision'], record['revision'])

    def test_bookmark_toggle_removes_only_the_selected_position(self):
        reader = self.session()
        first, second = {'unit': 0, 'offset': 0}, {'unit': 1, 'offset': 2500}
        reader.save(first, bookmark=True)
        reader.save(second, bookmark=True)
        record, warning = reader.save(first, bookmark=True)
        self.assertIsNone(warning)
        self.assertEqual(record['bookmarks'], [second])

    def test_portable_record_has_no_path_title_site_or_account_data(self):
        reader = self.session()
        reader.save({'unit': 1, 'offset': 0}, bookmark=True)
        raw = self.record_path(reader).read_text()
        self.assertNotIn(str(self.base), raw)
        self.assertNotIn(self.archive.name, raw)
        self.assertNotIn('Reader fixture', raw)
        self.assertNotIn('tapas', raw)
        self.assertEqual(set(json.loads(raw)), {'v', 'content_sha256', 'kind', 'units', 'revision',
                                                'position', 'bookmarks', 'preferences'})

    def test_identical_copy_and_renamed_archive_resume_but_replacement_has_separate_progress(self):
        reader = self.session()
        reader.save({'unit': 1, 'offset': 4000}, bookmark=True)
        target = self.base / 'moved/Library'
        shutil.copytree(self.root, target)
        copied = target / self.archive.relative_to(self.root)
        renamed = copied.with_name('renamed.cbz')
        copied.rename(renamed)
        sidecar_path(copied).rename(sidecar_path(renamed))
        with closing(ReadingSession(target, renamed)) as moved:
            self.assertEqual(moved.record['position'], {'unit': 1, 'offset': 4000})
        replacement = make_book(self.root, key='83')
        # Equal public metadata does not make distinct archive bytes the same version.
        raw = bytearray(replacement.read_bytes())
        offset = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<H', raw, offset + 20, 1)
        raw += b'x'
        replacement.write_bytes(raw)
        with closing(ReadingSession(self.root, replacement)) as changed:
            self.assertNotEqual(changed.book.content, reader.book.content)
            self.assertEqual(changed.record['position'], {'unit': 0, 'offset': 0})

    def test_stale_writer_cannot_erase_newer_progress_or_bookmarks(self):
        first, second = self.session(), self.session()
        one, two = {'unit': 1, 'offset': 1000}, {'unit': 0, 'offset': 2500}
        first.save(one, bookmark=True)
        committed = self.record_path(first).read_bytes()
        _, warning = second.save(two, bookmark=True)
        self.assertIn('another window', warning)
        self.assertEqual(self.record_path(first).read_bytes(), committed)
        second.reload()
        record, warning = second.save(two, bookmark=True)
        self.assertIsNone(warning)
        self.assertEqual(record['bookmarks'], [one, two])

    def test_corrupt_future_and_oversized_progress_never_blocks_reading_or_gets_overwritten(self):
        initial = self.session()
        path = self.record_path(initial)
        path.parent.mkdir(parents=True, exist_ok=True)
        for raw in (b'not json PRIVATE', b'{"v":99}', b'x' * (local_reader.MAX_RECORD + 1)):
            path.write_bytes(raw)
            reader = self.session()
            self.assertFalse(reader.can_save)
            self.assertTrue(reader.warning)
            self.assertEqual(reader.book.render(1)['data'], png(2))
            reader.save({'unit': 1, 'offset': 5000}, bookmark=True)
            self.assertEqual(path.read_bytes(), raw)

    def test_wrong_content_or_unit_count_record_is_not_adopted_as_a_resume(self):
        reader = self.session()
        path = self.record_path(reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        for delta in ({'content_sha256': '0' * 64}, {'kind': 'epub'}, {'units': 3}):
            value = copy.deepcopy(reader.record) | delta
            path.write_text(json.dumps(value))
            fresh = self.session()
            self.assertFalse(fresh.can_save)
            self.assertEqual(fresh.record['position'], {'unit': 0, 'offset': 0})
            self.assertNotIn('PRIVATE', fresh.warning)

    def test_optional_v1_fields_default_without_changing_the_existing_record(self):
        reader = self.session()
        value = {key: reader.record[key] for key in ('v', 'content_sha256', 'kind', 'units')}
        path = self.record_path(reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        raw = path.read_bytes()
        reopened = self.session()
        self.assertEqual(reopened.record, reader.record)
        self.assertEqual(ReadingStore(self.root).load(reader.book.content, 'cbz', 2), reader.record)
        self.assertEqual(path.read_bytes(), raw)

    def test_record_validation_refuses_invalid_positions_bookmarks_preferences_and_revisions(self):
        original = self.session().record
        for delta in ({'revision': True}, {'revision': -1}, {'v': True}, {'v': 2}, {'units': True},
                      {'position': {'unit': 2, 'offset': 0}}, {'position': {'unit': 0, 'offset': -1}},
                      {'position': {'unit': 0, 'offset': 10001}}, {'bookmarks': [{}]},
                      {'bookmarks': [{'unit': 0, 'offset': 0}] * 2},
                      {'preferences': {'text_scale': float('nan')}}, {'preferences': {'text_scale': 3}},
                      {'preferences': {'fit': 'other'}}, {'PRIVATE': 'secret'}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                checked_record(original | delta)

    def test_bookmark_limit_is_an_actionable_refusal_and_keeps_the_committed_record(self):
        reader = self.session()
        reader.record['bookmarks'] = [{'unit': 0, 'offset': n} for n in range(100)]
        reader.record = reader.store.save(reader.record, 0)
        before = self.record_path(reader).read_bytes()
        with self.assertRaisesRegex(ReadingError, 'bookmark limit'):
            reader.save({'unit': 1, 'offset': 0}, bookmark=True)
        self.assertEqual(self.record_path(reader).read_bytes(), before)

    def test_failed_atomic_save_retains_the_last_committed_record_and_can_retry(self):
        reader = self.session()
        reader.save({'unit': 0, 'offset': 1000})
        before = self.record_path(reader).read_bytes()
        with patch('comic_archive.archive.replace_file', side_effect=OSError('PRIVATE')):
            _, warning = reader.save({'unit': 1, 'offset': 0})
        self.assertIn('could not be saved', warning)
        self.assertNotIn('PRIVATE', warning)
        self.assertEqual(self.record_path(reader).read_bytes(), before)
        _, warning = reader.save({'unit': 1, 'offset': 0})
        self.assertIsNone(warning)
        self.assertEqual(json.loads(self.record_path(reader).read_text())['position']['unit'], 1)

    def test_other_process_lock_refusal_does_not_overwrite_or_leak_a_descriptor(self):
        reader = self.session()
        reader.save({'unit': 0, 'offset': 0})
        descriptor = os.open(reader.store.folder / 'writer.lock', os.O_RDWR)
        try:
            osdep.lock(descriptor)
            before = self.record_path(reader).read_bytes()
            with patch('comic_archive.local_reader.os.close', wraps=os.close) as closed:
                _, warning = reader.save({'unit': 1, 'offset': 0})
            self.assertTrue(warning)
            self.assertEqual(self.record_path(reader).read_bytes(), before)
            closed.assert_called_once()
        finally:
            osdep.unlock(descriptor)
            os.close(descriptor)

    def test_symlink_archive_sidecar_and_progress_paths_are_refused_without_following_them(self):
        outside = self.base / 'outside'
        outside.write_text('PRIVATE sentinel')
        link = self.archive.parent / 'link.cbz'
        link.symlink_to(self.archive)
        with self.assertRaises(ReadingError):
            ReaderBook(self.root, link)
        reader = self.session()
        path = self.record_path(reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(outside)
        reopened = self.session()
        self.assertFalse(reopened.can_save)
        reopened.save({'unit': 1, 'offset': 0})
        self.assertEqual(outside.read_text(), 'PRIVATE sentinel')
        sidecar = sidecar_path(self.archive)
        sidecar.unlink()
        sidecar.symlink_to(outside)
        with self.assertRaises(ReadingError):
            self.session()

    def test_outside_library_and_unsupported_files_are_not_read(self):
        with self.assertRaises(ReadingError):
            ReaderBook(self.base / 'other', self.archive)
        with self.assertRaises(ReadingError):
            ReaderBook(self.root, sidecar_path(self.archive))

    def test_missing_corrupt_and_private_exceptions_are_plain_public_refusals(self):
        self.archive.write_bytes(b'PRIVATE broken file')
        with self.assertRaises(ReadingError) as caught:
            self.session()
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn(str(self.base), str(caught.exception))
        self.archive.unlink()
        with self.assertRaises(ReadingError):
            self.session()

    def test_replacement_and_in_place_changes_stop_an_open_reader(self):
        reader = self.session()
        replacement = self.archive.with_name('replacement')
        replacement.write_bytes(self.archive.read_bytes())
        replacement.replace(self.archive)
        with self.assertRaises(ReadingError):
            reader.book.render(0)
        reopened = self.session()
        with self.archive.open('r+b') as stream:
            stream.write(b'X')
        with self.assertRaises(ReadingError):
            reopened.book.render(0)

    def test_forged_directory_count_is_refused_before_zipfile_allocates_members(self):
        raw = bytearray(self.archive.read_bytes())
        offset = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', raw, offset + 8, 1, 1)
        self.archive.write_bytes(raw)
        with patch('comic_archive.local_reader.zipfile.ZipFile') as opened:
            with self.assertRaises(ReadingError):
                self.session()
            opened.assert_not_called()

    def test_declared_archive_limit_is_checked_before_opening_the_zip_directory(self):
        with patch('comic_archive.local_reader.MAX_ARCHIVE', 1), self.assertRaisesRegex(ReadingError, 'supported limits'):
            self.session()

    def test_combined_expansion_budget_refuses_a_small_zip_bomb_directory(self):
        central = bytearray()
        for _ in range(100):
            entry = bytearray(46)
            entry[:4] = b'PK\x01\x02'
            struct.pack_into('<I', entry, 24, 100 * 1024**2)
            central.extend(entry)
        raw = bytes(central) + struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 100, 100, len(central), 0, 0)
        with self.assertRaisesRegex(ReadingError, 'supported limits'):
            local_reader._zip_bounds(io.BytesIO(raw), len(raw))

    def test_deep_but_otherwise_valid_epub_markup_does_not_reach_the_text_widget(self):
        path = make_book(self.root, novel=True, key='82', text='<div>' * 140 + 'Words' + '</div>' * 140)
        reader = self.session(path)
        with self.assertRaises(ReadingError):
            reader.book.render(0)

    def test_close_waits_for_active_reads_to_retire_and_discards_their_late_result(self):
        reader = self.session()
        entered, release = threading.Event(), threading.Event()
        original = reader.book._read
        results = []

        def held(name):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Read was not released')
            return original(name)

        def work():
            try:
                results.append(reader.book.render(0))
            except ReadingError:
                results.append('closed')

        with patch.object(reader.book, '_read', side_effect=held):
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                reader.close()
                self.assertFalse(reader.book._stream.closed)
                with self.assertRaises(ReadingError):
                    reader.book.render(0)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, ['closed'])
        self.assertTrue(reader.book._stream.closed)
        reader.close()

    def test_unexpected_progress_failure_retires_the_already_open_archive(self):
        books = []

        def opened(*args):
            book = ReaderBook(*args)
            books.append(book)
            return book

        with patch('comic_archive.local_reader.ReaderBook', side_effect=opened), \
                patch.object(ReadingStore, 'load', side_effect=RuntimeError('PRIVATE failure')):
            with self.assertRaises(RuntimeError):
                ReadingSession(self.root, self.archive)
        self.assertEqual(len(books), 1)
        self.assertTrue(books[0]._stream.closed)
