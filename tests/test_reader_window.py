"""Actual Qt controls, actual registered files and the production worker/GUI handoff."""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from comic_archive import app_actions, desktop_app, local_reader, reader_window, sites, window_pages  # noqa: E402
from comic_archive.local_library import LocalWorkspaceRegistry  # noqa: E402
from test_local_reader import make_book, original_hashes  # noqa: E402
from test_window_pages import Shell  # noqa: E402

Q = SimpleNamespace(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
APP = None


def setUpModule():
    global APP
    APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('The actual Qt operation did not complete')
        QtTest.QTest.qWait(10)
    APP.processEvents()


def image(width, height, color):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = (b'\0' + bytes((color, 40, 80)) * width) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class LargeImages:
    def retrieve(self, page):
        return image(480, 960, page.order * 80)


class ThreadedShell(Shell):
    def __init__(self, window):
        super().__init__()
        self.q, self.window = Q, window

    def background(self, work, done):
        return desktop_app.Shell.background(self, work, done)

    def run(self, work, done):
        return desktop_app.Shell.run(self, work, done)


class ReaderWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.library = self.base / 'Library'
        self.comic = make_book(self.library, adapter=LargeImages())
        self.novel = make_book(self.library, novel=True, key='82',
            text='<h2>Chapter one</h2>' + '<p>Readable <em>words</em> &amp; punctuation. A long offline chapter.</p>' * 100)
        self.registry = LocalWorkspaceRegistry(self.base)
        self.actions = app_actions.Actions(lambda: SimpleNamespace(registry=self.registry, site=sites.DEFAULT))
        self.window = QtWidgets.QMainWindow()
        self.shell = ThreadedShell(self.window)
        self.pages = window_pages.Pages(Q, self.shell, self.actions)
        self.window.setCentralWidget(self.pages.widget)
        self.window.resize(1180, 900)
        self.window.show()
        self.pages.state.workspace_id = self.registry.workspace_id
        self.pages.state.cloud_offered = False
        self.pages.state.library_folder = str(self.library)
        self.pages.go_to('library', location='device')
        wait_until(lambda: not self.pages.library.busy)
        self.before = original_hashes(self.library)
        self.addCleanup(self.cleanup_window)
        self.no_download = patch('comic_archive.local_library.submit_inspection',
                                 side_effect=AssertionError('Reading must never admit download or cloud work'))
        self.no_download.start()
        self.addCleanup(self.no_download.stop)

    def cleanup_window(self):
        drained = threading.Event()
        self.pages.close_readers(drained.set)
        wait_until(drained.is_set)
        self.window.close()
        self.window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def button(self, kind):
        for index, row in enumerate(self.pages.library.value['files']):
            if row['kind'] == kind:
                holder = self.pages.library.table.cellWidget(index, 7)
                return next(button for button in holder.findChildren(QtWidgets.QPushButton) if button.text() == 'Read here')
        raise AssertionError('Actual catalog did not offer the saved format')

    def opened(self, kind):
        before = set(self.pages.readers)
        button = self.button(kind)
        self.pages.library.table.scrollToItem(self.pages.library.table.item(
            next(i for i, row in enumerate(self.pages.library.value['files']) if row['kind'] == kind), 0))
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        wait_until(lambda: bool(self.pages.readers - before))
        reader = next(iter(self.pages.readers - before))
        wait_until(lambda: not reader.busy)
        APP.processEvents()
        return reader

    def capture(self, reader, name):
        directory = Path(os.environ.get('COMIC_READER_ARTIFACTS', self.base / 'artifacts'))
        directory.mkdir(parents=True, exist_ok=True)
        self.assertTrue(reader.grab().save(str(directory / (name + '.png'))))
        self.assertFalse(reader.grab().toImage().isNull())

    def test_actual_library_read_action_renders_comic_pages_and_keeps_external_open(self):
        holder = self.button('cbz').parentWidget()
        self.assertIn('Open', [button.text() for button in holder.findChildren(QtWidgets.QPushButton)])
        reader = self.opened('cbz')
        self.assertEqual(reader.counter.text(), 'Page 1 of 2')
        self.assertFalse(reader.page.pixmap().isNull())
        self.assertEqual(reader.image.pixelColor(0, 0).red(), 80)
        self.assertFalse(reader.previous.isEnabled())
        QtTest.QTest.mouseClick(reader.next, QtCore.Qt.MouseButton.LeftButton)
        wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
        self.assertEqual(reader.image.pixelColor(0, 0).red(), 160)
        self.assertFalse(reader.next.isEnabled())
        self.capture(reader, 'comic-page-two')
        self.assertEqual(original_hashes(self.library), self.before)

    def test_real_keyboard_navigation_and_escape_close_release_the_book(self):
        reader = self.opened('cbz')
        reader.activateWindow()
        reader.next.setFocus()
        APP.processEvents()
        QtTest.QTest.keyClick(reader.next, QtCore.Qt.Key.Key_Right)
        wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
        QtTest.QTest.keyClick(reader, QtCore.Qt.Key.Key_Escape)
        wait_until(lambda: reader not in self.pages.readers)
        self.assertTrue(reader.session.book._stream.closed)

    def test_comic_resume_bookmark_and_fit_choice_survive_a_real_window_reopen(self):
        reader = self.opened('cbz')
        reader.next.click()
        wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
        reader.fit.setCurrentIndex(reader.fit.findData('page'))
        reader.fit.activated.emit(reader.fit.currentIndex())
        wait_until(lambda: not reader.busy)
        reader.bookmark.click()
        wait_until(lambda: not reader.busy and len(reader.session.record['bookmarks']) == 1)
        self.assertEqual(reader.bookmark.text(), 'Remove bookmark')
        reader.close()
        wait_until(lambda: reader not in self.pages.readers)
        reopened = self.opened('cbz')
        self.assertEqual(reopened.counter.text(), 'Page 2 of 2')
        self.assertEqual(reopened.fit.currentData(), 'page')
        self.assertEqual(reopened.bookmarks.count(), 2)
        reopened.previous.click()
        wait_until(lambda: not reopened.busy and reopened.position['unit'] == 0)
        reopened.bookmarks.activated.emit(1)
        wait_until(lambda: not reopened.busy and reopened.position['unit'] == 1)
        reopened.bookmark.click()
        wait_until(lambda: not reopened.busy and not reopened.session.record['bookmarks'])

    def test_novel_text_scroll_bookmark_and_size_resume_without_a_site_session(self):
        reader = self.opened('epub')
        self.assertEqual(reader.stack.currentWidget(), reader.novel)
        self.assertIn('Readable words & punctuation', reader.novel.toPlainText())
        self.assertGreater(reader.novel.verticalScrollBar().maximum(), 0)
        reader.text_scale.setCurrentIndex(reader.text_scale.findData(1.5))
        reader.text_scale.activated.emit(reader.text_scale.currentIndex())
        wait_until(lambda: not reader.busy)
        APP.processEvents()
        bar = reader.novel.verticalScrollBar()
        bar.setValue(round(bar.maximum() * .65))
        wait_until(lambda: not reader.busy and reader.session.record['position']['offset'] > 6000)
        reader.bookmark.click()
        wait_until(lambda: not reader.busy and len(reader.session.record['bookmarks']) == 1)
        position = dict(reader.session.record['position'])
        self.capture(reader, 'novel-reading')
        reader.close()
        wait_until(lambda: reader not in self.pages.readers)
        reopened = self.opened('epub')
        APP.processEvents()
        self.assertEqual(reopened.session.record['position'], position)
        self.assertEqual(reopened.text_scale.currentData(), 1.5)
        bar = reopened.novel.verticalScrollBar()
        self.assertAlmostEqual(bar.value() / max(1, bar.maximum()), .65, delta=.03)
        self.assertEqual(original_hashes(self.library), self.before)

    def test_text_widget_refuses_file_http_data_and_undeclared_resource_requests(self):
        reader = self.opened('epub')
        resource = QtGui.QTextDocument.ResourceType.ImageResource
        for url in ('file:///PRIVATE/secret.png', 'https://invalid.test/secret', 'data:image/svg+xml,<svg/>',
                    'reader:/EPUB/images/unknown.png'):
            value = reader.novel.loadResource(resource, QtCore.QUrl(url))
            self.assertIsInstance(value, QtCore.QByteArray)
            self.assertEqual(value.size(), 0)
        self.assertFalse(reader.novel.openLinks())
        self.assertFalse(reader.novel.openExternalLinks())

    def test_verified_novel_illustration_uses_only_the_in_archive_image(self):
        path = make_book(self.library, novel=True, key='83',
            text='<p>Illustrated words.</p><img src="images/0001.png" alt="Synthetic illustration" />',
            illustrations=(image(160, 240, 100),))
        session = local_reader.ReadingSession(self.library, path)
        reader = reader_window.reader_window(Q, session, self.shell.run, self.window, self.pages.reader_closed)
        self.pages.readers.add(reader)
        reader.show()
        wait_until(lambda: not reader.busy)
        result = reader.novel.loadResource(QtGui.QTextDocument.ResourceType.ImageResource,
                                          QtCore.QUrl('reader:/EPUB/images/0001.png'))
        self.assertIsInstance(result, QtGui.QImage)
        self.assertEqual((result.width(), result.height()), (160, 240))
        APP.processEvents()
        rendered = reader.novel.viewport().grab().toImage()
        colored = sum(rendered.pixelColor(x, y).getRgb()[:3] == (100, 40, 80)
                      for y in range(0, rendered.height(), 4) for x in range(0, rendered.width(), 4))
        self.assertGreater(colored, 100, 'The actual text widget must paint the archive illustration')
        self.capture(reader, 'novel-illustration')

    def test_stale_progress_is_visible_and_reload_recovers_the_real_committed_position(self):
        reader = self.opened('cbz')
        other = local_reader.ReadingSession(self.library, self.comic)
        self.addCleanup(other.close)
        other.save({'unit': 1, 'offset': 0}, bookmark=True)
        path = other.store.folder / (other.book.content + '.json')
        before = path.read_bytes()
        reader.bookmark.click()
        wait_until(lambda: not reader.busy and 'another window' in reader.notice.text())
        self.assertEqual(path.read_bytes(), before)
        reader.reload.click()
        wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
        self.assertEqual(reader.session.record['bookmarks'], [{'unit': 1, 'offset': 0}])

    def test_corrupt_progress_still_renders_and_does_not_claim_a_save(self):
        content = hashlib.sha256(self.comic.read_bytes()).hexdigest()
        path = self.library / local_reader.READING_DIRECTORY / (content + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'PRIVATE corrupt reading record')
        reader = self.opened('cbz')
        self.assertFalse(reader.page.pixmap().isNull())
        self.assertIn('existing record is retained', reader.notice.text())
        reader.bookmark.click()
        wait_until(lambda: not reader.busy)
        self.assertEqual(path.read_bytes(), b'PRIVATE corrupt reading record')
        self.assertNotIn('PRIVATE', reader.notice.text())

    def test_close_waits_for_a_running_read_and_pages_shutdown_waits_for_reader_flush(self):
        reader = self.opened('cbz')
        entered, release, drained = threading.Event(), threading.Event(), threading.Event()
        original = reader.session.book.render

        def held(unit):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Held page was not released')
            return original(unit)

        with patch.object(reader.session.book, 'render', side_effect=held):
            reader.next.click()
            wait_until(entered.is_set)
            self.pages.close_readers(drained.set)
            self.assertFalse(drained.is_set())
            self.assertFalse(reader.session.book._stream.closed)
            release.set()
            wait_until(drained.is_set)
        self.assertTrue(reader.session.book._stream.closed)
        self.assertFalse(self.pages.readers)

    def test_quit_during_archive_open_closes_the_late_session_without_opening_a_reader(self):
        entered, release, drained = threading.Event(), threading.Event(), threading.Event()
        original = self.registry.open_reader
        sessions = []

        def held(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Held open was not released')
            value = original(*args)
            sessions.append(value)
            return value

        with patch.object(self.registry, 'open_reader', side_effect=held):
            self.button('cbz').click()
            wait_until(entered.is_set)
            self.pages.close_readers(drained.set)
            self.assertFalse(drained.is_set())
            release.set()
            wait_until(drained.is_set)
        self.assertFalse(self.pages.readers)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].book._stream.closed)

    def test_image_pixel_budget_is_checked_before_decode_and_scaled_output_stays_bounded(self):
        # Valid PNG header/CRC advertises a raster far larger than the small compressed bytes.
        payload = bytearray(image(1, 1, 80))
        struct.pack_into('>II', payload, 16, 100_000, 100_000)
        struct.pack_into('>I', payload, 29, zlib.crc32(payload[12:29]))
        with self.assertRaises(local_reader.ReadingError):
            reader_window.decoded_image(Q, bytes(payload))
        reader = self.opened('cbz')
        reader.resize(640, 480)
        APP.processEvents()
        self.assertLessEqual(reader.page.pixmap().width() * reader.page.pixmap().height(), 32_000_000)

    def test_changed_file_keeps_the_previous_page_and_reports_a_recovery_without_false_progress(self):
        reader = self.opened('cbz')
        self.comic.unlink()
        reader.next.click()
        wait_until(lambda: not reader.busy and 'missing, changed or incomplete' in reader.notice.text())
        self.assertEqual(reader.counter.text(), 'Page 1 of 2')
        self.assertEqual(reader.session.record['position']['unit'], 0)
        self.assertNotIn(str(self.base), reader.notice.text())

    def test_failed_progress_write_is_visible_while_the_next_page_remains_readable(self):
        reader = self.opened('cbz')
        with patch.object(reader.session.store, 'save', side_effect=local_reader.ReadingError(local_reader.SAVE_FAILED)):
            reader.next.click()
            wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
        self.assertIn('could not be saved', reader.notice.text())
        self.assertEqual(reader.counter.text(), 'Page 2 of 2')
        self.assertEqual(reader.session.record['revision'], 0)
        reader.reload.click()
        wait_until(lambda: not reader.busy and reader.position['unit'] == 0)

    def test_large_text_keeps_navigation_preferences_bookmarks_and_recovery_inside_the_reader(self):
        reader = self.opened('cbz')
        font = reader.font()
        font.setPointSizeF(max(10., font.pointSizeF()) * 2)
        reader.setFont(font)
        reader.resize(900, 760)
        APP.processEvents()
        for widget in (reader.previous, reader.next, reader.fit, reader.bookmark, reader.bookmarks, reader.reload):
            self.assertTrue(widget.isVisible())
            self.assertTrue(reader.rect().contains(widget.geometry()), widget.accessibleName())
        self.assertTrue(reader.next.isEnabled())
        self.capture(reader, 'reader-large-text')

    def test_library_context_change_retires_a_late_open_instead_of_launching_the_previous_library(self):
        entered, release = threading.Event(), threading.Event()
        original = self.registry.open_reader
        sessions = []

        def held(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Held open was not released')
            value = original(*args)
            sessions.append(value)
            return value

        with patch.object(self.registry, 'open_reader', side_effect=held):
            self.button('cbz').click()
            wait_until(entered.is_set)
            self.pages.state = copy.copy(self.pages.state)
            self.pages.state.workspace_id = 'b' * 32
            release.set()
            wait_until(lambda: self.pages.reader_opens == 0)
        self.assertFalse(self.pages.readers)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].book._stream.closed)

    def test_actual_v1_producer_records_are_retained_with_both_format_outputs(self):
        # CI keeps these real producer bytes, not a hand-authored historical stand-in.
        target = Path(os.environ.get('COMIC_READER_ARTIFACTS', self.base / 'artifacts')) / 'producer-v1/Library'
        for kind in ('cbz', 'epub'):
            reader = self.opened(kind)
            if kind == 'cbz':
                reader.next.click()
                wait_until(lambda: not reader.busy and reader.position['unit'] == 1)
            reader.preferences = {'fit': 'page', 'text_scale': 1.25}
            reader.position['offset'] = 2500 if kind == 'cbz' else 6500
            reader.save_position(bookmark=True)
            wait_until(lambda: not reader.busy and bool(reader.session.record['bookmarks']))
            self.assertEqual(reader.session.record['preferences'], {'fit': 'page', 'text_scale': 1.25})
            path = reader.session.store.folder / (reader.session.book.content + '.json')
            record_target = target / path.relative_to(self.library)
            record_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, record_target)
            archive = self.comic if kind == 'cbz' else self.novel
            folder = target / archive.parent.relative_to(self.library)
            folder.mkdir(parents=True, exist_ok=True)
            for source in (archive, archive.with_suffix('.json'), archive.parent / 'series.json'):
                shutil.copy2(source, folder / source.name)
            record = copy.deepcopy(reader.session.record)
            reader.close()
            wait_until(lambda: reader not in self.pages.readers)
            loaded = local_reader.ReadingStore(target).load(record['content_sha256'], kind, record['units'])
            self.assertEqual(loaded, record)
        # Retain the synthetic publication provenance/inventory needed for a real
        # portable-library adoption, without any session, registry key or lock.
        for source in (self.library / '.archive-state').rglob('*'):
            relative = source.relative_to(self.library)
            if (not source.is_file() or relative.parts[1] == 'reading' or source.name == 'writer.lock'
                    or source.name.endswith(('.tmp', '-wal', '-shm'))):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
