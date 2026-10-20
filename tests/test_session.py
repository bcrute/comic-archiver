from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from support import PRIVATE_FILE
from comic_archive import session
from comic_archive.models import ArchiveError


class ForgetSessionTests(unittest.TestCase):
    def test_the_saved_sign_in_goes_and_a_link_goes_without_what_it_points_to(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); folder = root / 'session'; folder.mkdir()
        elsewhere = root / 'elsewhere.json'; elsewhere.write_text('KEEP')
        (folder / 'state.json').symlink_to(elsewhere); (folder / 'state.tmp').write_text('PRIVATE')
        (folder / 'site').write_text('tapas'); (folder / 'speed').write_text('fast')
        with patch('comic_archive.session.sync_directory') as synced:
            session.forget_session(folder)
        synced.assert_called_once_with(folder)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['site', 'speed'])
        self.assertEqual(elsewhere.read_text(), 'KEEP')
        session.forget_session(folder)  # Nothing to remove.


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.real_api = session.browser_api
        self.driver = MagicMock()
        self.browser = self.driver.firefox.launch.return_value
        factory = MagicMock(); factory.__enter__.return_value = self.driver
        patched = patch('comic_archive.session.browser_api', return_value=Mock(return_value=factory))
        patched.start(); self.addCleanup(patched.stop)

    def test_a_missing_browser_package_is_reported_with_the_command_that_installs_it(self):
        with patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            with self.assertRaises(ArchiveError) as raised:
                self.real_api()
        self.assertIn('playwright install', str(raised.exception))

    def test_startup_failures_are_named_by_cause_and_never_echo_the_error_text(self):
        for text, cause in [('Error: Missing X server or $DISPLAY PRIVATE', 'desktop display is unavailable'),
                            ('cannot open display :0 PRIVATE', 'desktop display is unavailable'),
                            ('error while loading shared libraries: libgtk PRIVATE', 'system libraries are missing'),
                            ('Host system is missing dependencies PRIVATE', 'system libraries are missing'),
                            ('SIGSEGV PRIVATE', 'crashed during startup'),
                            ('something else PRIVATE', 'browser startup failed')]:
            with self.subTest(text):
                reason = session.browser_failure_reason(RuntimeError(text))
                self.assertIn(cause, reason); self.assertNotIn('PRIVATE', reason)

    def test_an_unsupported_browser_name_is_refused_before_anything_is_launched(self):
        for name in ('safari', 'webkit', '', None):
            with self.subTest(name), self.assertRaises(ArchiveError):
                session.launch_browser(self.driver, name)
        self.assertEqual(self.driver.mock_calls, [])

    def test_the_built_in_engine_needs_no_browser_package_and_is_the_only_engine_tried_for_qt(self):
        from comic_archive import qt_engine
        with patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            self.assertIs(self.real_api('qt'), qt_engine.Driver)
        self.assertIs(session.launch_browser(self.driver, 'qt', headless=True), self.driver.qt.launch.return_value)
        self.driver.qt.launch.assert_called_once_with(headless=True, timeout=15000)
        self.driver.chromium.launch.assert_not_called(); self.driver.firefox.launch.assert_not_called()
        with self.assertRaisesRegex(ArchiveError, 'chromium, firefox or qt'):
            session.launch_browser(self.driver, 'safari')

    def test_an_engine_that_cannot_start_is_named_by_cause_without_its_error_text(self):
        from comic_archive import qt_engine
        self.driver.qt.launch.side_effect = qt_engine.Error('The built-in engine is not installed. PRIVATE')
        with self.assertRaises(ArchiveError) as raised:
            session.launch_browser(self.driver, 'qt')
        self.assertIn('qt: the app', str(raised.exception)); self.assertIn('built-in engine is unavailable', str(raised.exception))
        self.assertNotIn('PRIVATE', str(raised.exception))

    def test_the_browser_check_loads_no_account_data_and_closes_the_browser_even_when_it_fails(self):
        with redirect_stdout(io.StringIO()) as output:
            session.doctor('firefox', headless=True)
        self.driver.firefox.launch.assert_called_once_with(headless=True, timeout=15000)
        self.browser.new_context.assert_called_once_with()  # A fresh context: no saved state is handed to it.
        self.browser.close.assert_called_once_with()
        self.assertIn('No account data was loaded', output.getvalue())
        self.browser.reset_mock()
        self.browser.new_context.side_effect = RuntimeError('PRIVATE')
        with self.assertRaises(RuntimeError):
            session.doctor('firefox')
        self.browser.close.assert_called_once_with()

    def test_a_saved_sign_in_is_on_disk_before_it_takes_the_old_ones_place_and_the_rename_reaches_the_disk(self):
        events, fsync, replace_file = [], os.fsync, session.replace_file

        def written(descriptor):
            events.append('file')
            fsync(descriptor)

        def replaced(source, target):
            events.append(('replace', target.name))
            replace_file(source, target)

        directory = self.root / 'session'
        with patch('comic_archive.session.os.fsync', side_effect=written), \
                patch('comic_archive.session.replace_file', side_effect=replaced), \
                patch('comic_archive.session.sync_directory', side_effect=lambda path: events.append(('folder', path))):
            session.store_session({'cookies': [{'domain': '.tapas.io', 'name': 'session', 'value': 'PRIVATE', 'expires': -1}],
                                   'origins': []}, directory)
        self.assertEqual(events, ['file', ('replace', 'site'), ('folder', directory),
                                  'file', ('replace', 'state.json'), ('folder', directory)])
        self.assertEqual(json.loads((directory / 'state.json').read_text())['cookies'][0]['value'], 'PRIVATE')
        self.assertEqual((directory / 'state.json').stat().st_mode & 0o777, PRIVATE_FILE)
