from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from support import PRIVATE_FILE
from comic_archive import session
from comic_archive.models import ArchiveError


COLUMNS = 'name TEXT,value TEXT,host TEXT,path TEXT,expiry INTEGER,isSecure INTEGER,isHttpOnly INTEGER,originAttributes TEXT'


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

    def test_login_saves_only_tapas_state_privately_and_always_closes_the_browser(self):
        context = self.browser.new_context.return_value
        context.storage_state.return_value = {
            'cookies': [{'domain': '.tapas.io', 'name': 'session', 'value': 'PRIVATE', 'expires': -1},
                        {'domain': '.google.com', 'name': 'oauth', 'value': 'OTHER', 'expires': -1}],
            'origins': [{'origin': 'https://tapas.io'}, {'origin': 'https://accounts.google.com'}]}
        directory = self.root / 'session'
        with patch('builtins.input', return_value=''), redirect_stdout(io.StringIO()):
            session.login(directory, 'firefox')
        context.new_page.return_value.goto.assert_called_once_with('https://tapas.io/account/signin')
        saved = json.loads((directory / 'state.json').read_text())
        self.assertEqual(([c['name'] for c in saved['cookies']], [o['origin'] for o in saved['origins']]),
                         (['session'], ['https://tapas.io']))
        self.assertEqual((directory / 'state.json').stat().st_mode & 0o777, PRIVATE_FILE)
        self.browser.close.assert_called_once_with()
        # Giving up at the prompt saves nothing and still closes the browser.
        other = self.root / 'abandoned'; self.browser.reset_mock()
        with patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()):
            with self.assertRaises(EOFError):
                session.login(other, 'firefox')
        self.assertFalse(other.exists())
        self.browser.close.assert_called_once_with()

    def database(self, profile, rows, columns=COLUMNS, check=False):
        profile.mkdir(exist_ok=True)
        names = [part.split()[0] for part in columns.split(',')]
        with closing(sqlite3.connect(profile / 'cookies.sqlite')) as db:
            db.execute(f'CREATE TABLE moz_cookies ({columns})')
            if check:
                db.execute('PRAGMA ignore_check_constraints=ON')
            db.executemany(f'INSERT INTO moz_cookies ({",".join(names)}) VALUES ({",".join("?" * len(names))})', rows)
            db.commit()

    def test_firefox_import_without_a_write_ahead_log_or_same_site_column_leaves_the_profile_untouched(self):
        profile, destination = self.root / 'firefox', self.root / 'session'
        later = int(time.time()) + 3600
        self.database(profile, [('session', 'PRIVATE', '.tapas.io', '/', later, 1, 1, ''),
                                ('other', 'OTHER', '.example.com', '/', later, 1, 1, '')])
        before = {path.name: path.read_bytes() for path in profile.iterdir()}
        with redirect_stdout(io.StringIO()):
            session.import_firefox(profile, destination)
        self.assertEqual({path.name: path.read_bytes() for path in profile.iterdir()}, before)
        cookies = json.loads((destination / 'state.json').read_text())['cookies']
        self.assertEqual([(c['name'], c['sameSite'], c['expires'], c['secure'], c['httpOnly']) for c in cookies],
                         [('session', 'Lax', later, True, True)])
        self.assertEqual((destination / 'state.json').stat().st_mode & 0o777, PRIVATE_FILE)

    def test_firefox_import_refuses_a_database_it_cannot_trust_and_saves_nothing(self):
        later = int(time.time()) + 3600
        cases = {
            'a different cookie table': dict(columns='name TEXT,value TEXT,host TEXT', rows=[('a', 'b', '.tapas.io')],
                                             fragment='Unsupported Firefox cookie database'),
            'a damaged snapshot': dict(columns=COLUMNS.replace('expiry INTEGER', 'expiry INTEGER CHECK(expiry >= 0)'),
                                       rows=[('session', 'PRIVATE', '.tapas.io', '/', -5, 1, 1, '')], check=True,
                                       fragment='inconsistent'),
            'only expired, foreign and container cookies': dict(columns=COLUMNS, fragment='No unexpired Tapas cookies', rows=[
                ('old', 'EXPIRED', '.tapas.io', '/', 1, 1, 1, ''), ('other', 'OTHER', '.example.com', '/', later, 1, 1, ''),
                ('box', 'CONTAINER', '.tapas.io', '/', later, 1, 1, '^userContextId=1')])}
        for name, case in cases.items():
            with self.subTest(name):
                profile, destination = self.root / name.replace(' ', '-'), self.root / ('session-' + name.replace(' ', '-'))
                self.database(profile, case['rows'], case['columns'], case.get('check', False))
                with self.assertRaises(ArchiveError) as raised:
                    session.import_firefox(profile, destination)
                self.assertIn(case['fragment'], str(raised.exception)); self.assertNotIn('PRIVATE', str(raised.exception))
                self.assertFalse(destination.exists())
