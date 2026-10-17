from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from support import PRIVATE_FILE, PRIVATE_FOLDER, STATE_DEFAULT, STATE_HOME, home_environment
from comic_archive import app_runtime, sites
from comic_archive.account_ui import DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL
from comic_archive.errors import LibraryBusy
from comic_archive.local_web import LOCAL_OWNER
from comic_archive.models import ArchiveError
from comic_archive.session import load_session
from comic_archive.window_signin import WindowSignIn
from comic_archive.worker import WORKER_VERSION


class AppRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root, self.state = self.home / 'Comic Archiver', self.home / 'state'
        self.background = {'status': Mock(return_value={'running': False, 'state': 'stopped'}),
                           'start': Mock(return_value={'running': True}), 'stop': Mock(return_value={'running': False})}
        for wrapper in [patch.dict(os.environ, home_environment(self.home, self.state)),
                        patch('comic_archive.app_runtime.RETRY_SECONDS', .01),
                        *(patch('comic_archive.app_runtime.background.' + name, value) for name, value in self.background.items())]:
            wrapper.start(); self.addCleanup(wrapper.stop)
        self.addCleanup(self.reset_logging)
        self.runtime = app_runtime.Runtime(self.root)
        self.addCleanup(self.runtime.stop, 0)

    def reset_logging(self):
        for handler in list(app_runtime.LOG.handlers):
            app_runtime.LOG.removeHandler(handler); handler.close()
        app_runtime.LOG.addHandler(logging.NullHandler())

    def sign_in(self):
        session = self.root / '.comic-session'; session.mkdir(parents=True, exist_ok=True)
        (session / 'state.json').write_text('{}')
        return session

    def fetch(self, url):
        with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
            return response.status, response.headers, response.read().decode()

    def test_default_library_is_one_fixed_folder_in_the_real_home_never_the_working_directory(self):
        self.assertEqual(app_runtime.default_root(), self.home / 'Comic Archiver')
        real = self.home / 'real'; real.mkdir()
        link = self.home / 'link'; link.symlink_to(real, target_is_directory=True)
        with patch.dict(os.environ, home_environment(link)):
            root = app_runtime.default_root()
            self.assertEqual(root, real / 'Comic Archiver')
            self.assertNotIn(Path.cwd(), (root, root.parent))
            runtime = app_runtime.Runtime(root); self.addCleanup(runtime.stop, 0)
            runtime.start()
        self.assertTrue((real / 'Comic Archiver/Library').is_dir())

    def test_the_apps_private_folders_beside_and_inside_the_library_are_hidden_from_the_file_manager(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        with patch('comic_archive.osdep.hide') as hide, patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            self.runtime.start()
            self.assertIsNone(self.runtime.change_site(example))
        hidden = {call.args[0] for call in hide.call_args_list}
        for place in (self.root, self.root / 'Example'):
            for name in ('.comic-session', '.comic-web', 'Library/.archive-state'):
                self.assertIn(place / name, hidden)
                self.assertEqual(stat.S_IMODE((place / name).stat().st_mode), PRIVATE_FOLDER)

    def test_a_picked_folder_becomes_a_library_folder_without_mixing_into_other_files(self):
        pick = app_runtime.library_root
        empty, photos, named = self.home / 'empty', self.home / 'photos', self.home / 'drive/Comic Archiver'
        empty.mkdir(); photos.mkdir(); (photos / 'holiday.jpg').write_text('x'); named.mkdir(parents=True); (named / 'notes.txt').write_text('x')
        existing = self.home / 'comics'; (existing / 'Library/.archive-state').mkdir(parents=True)
        plain = self.home / 'books/Library'; plain.mkdir(parents=True); (plain / 'novel.epub').write_text('x')
        self.assertEqual((app_runtime.is_library(existing), app_runtime.is_library(plain.parent), app_runtime.present(existing),
                          app_runtime.present(self.home / 'gone'), app_runtime.present(Path('/media/\0'))), (True, False, True, False, False))
        link = self.home / 'link'; link.symlink_to(photos, target_is_directory=True)
        for chosen, root in [(empty, empty), (self.home / 'not-there-yet', self.home / 'not-there-yet'),
                             (photos, photos / 'Comic Archiver'), (named, named),
                             (existing, existing), (existing / 'Library', existing),  # The folder of saved files itself.
                             (plain, plain / 'Comic Archiver'),  # Somebody else's folder that happens to be called Library,
                             (plain.parent, plain.parent / 'Comic Archiver'),  # and the folder that folder is in.
                             (link, photos / 'Comic Archiver')]:  # Storage refuses symlinked parents.
            self.assertEqual(pick(str(chosen)), root)
        with patch.object(Path, 'resolve', side_effect=RuntimeError('loop')):
            self.assertEqual(pick(str(photos)), photos)  # Left for start() to refuse in its own words.
        with patch.object(Path, 'iterdir', side_effect=PermissionError('PRIVATE')):
            self.assertEqual(pick(str(empty)), empty / 'Comic Archiver')

    def test_the_chosen_library_folder_is_kept_for_the_next_start_and_the_standard_one_needs_no_record(self):
        saved, drive = self.state / 'comic-archiver/library', self.home / 'drive/Comic Archiver'
        self.assertIsNone(app_runtime.saved_root())
        app_runtime.remember_root(drive)
        self.assertEqual((app_runtime.saved_root(), saved.read_text()), (drive, f'{drive}\n'))
        self.assertEqual(sorted(path.name for path in saved.parent.iterdir()), ['library'])
        for text in ('', 'relative/folder\n', '\n', '/media/dri\0ve\n'):
            saved.write_text(text)
            self.assertIsNone(app_runtime.saved_root())
        app_runtime.remember_root(drive)
        for root in (None, app_runtime.default_root()):
            saved.write_text(f'{drive}\n')
            app_runtime.remember_root(root)
            self.assertFalse(saved.exists())
        app_runtime.remember_root(None)  # Nothing to forget.
        with patch('comic_archive.app_runtime.state_dir', side_effect=ArchiveError('blocked')):
            app_runtime.remember_root(drive)
            self.assertIsNone(app_runtime.saved_root())
        self.assertFalse(saved.exists())

    def test_the_dashboard_only_leaves_a_request_about_the_folder_for_the_window_to_pick_up(self):
        self.runtime.start()
        location = self.runtime.location
        self.assertIs(self.runtime.server.get_app().registry.location, location)
        self.assertEqual((location.get(), self.runtime.root, location.take()), (str(self.root / 'Library'), self.root, None))
        location.ask('open'); location.ask('choose')
        self.assertEqual((location.take(), location.take()), ('choose', None))
        with self.assertRaises(ArchiveError):
            location.ask('delete')
        self.assertIsNone(location.take())

    def test_changing_the_library_folder_restarts_there_keeps_the_sign_in_and_never_moves_saved_episodes(self):
        def signed_in(root, value):
            cookie = dict(name='session', value=value, domain='.tapas.io', path='/', expires=-1, httpOnly=True, secure=True, sameSite='Lax')
            (root / '.comic-session/state.json').write_text(json.dumps({'cookies': [cookie], 'origins': []}))

        def cookie(root):
            return load_session(root / '.comic-session')['cookies'][0]['value']
        self.runtime.start()
        signed_in(self.root, 'PRIVATE_FIRST')
        (self.root / 'Library/kept.cbz').write_text('saved')
        drive = self.home / 'drive'; drive.mkdir(); (drive / 'holiday.jpg').write_text('x')
        target = app_runtime.library_root(drive)
        fresh, problem = app_runtime.move(self.runtime, target); self.addCleanup(fresh.stop, 0)
        self.assertEqual((problem, fresh.root, fresh.theme, fresh.location.get()), (None, target, self.runtime.theme, str(target / 'Library')))
        # The old dashboard and downloader are gone before the new ones exist.
        self.assertEqual(self.runtime.stop(), 'stopped_before')
        self.assertEqual(self.background['stop'].call_args.args[0], self.root / '.comic-session')
        with self.assertRaises(URLError):
            self.fetch(self.runtime.url)
        self.assertEqual(self.fetch(fresh.url)[0], 200)
        # A folder with nothing in it starts signed in as before, privately, and its downloader starts.
        self.assertEqual((cookie(target), stat.S_IMODE((target / '.comic-session/state.json').stat().st_mode)), ('PRIVATE_FIRST', PRIVATE_FILE))
        self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [target / '.comic-session'])
        self.assertEqual(((self.root / 'Library/kept.cbz').read_text(), (target / 'Library/kept.cbz').exists()), ('saved', False))
        self.assertEqual((app_runtime.saved_root(), cookie(self.root)), (target, 'PRIVATE_FIRST'))
        # An existing library keeps its own sign-in; the standard folder needs no record.
        signed_in(target, 'PRIVATE_SECOND')
        again, problem = app_runtime.move(fresh, self.root); self.addCleanup(again.stop, 0)
        self.assertEqual((problem, again.root, cookie(self.root), app_runtime.saved_root()), (None, self.root, 'PRIVATE_FIRST', None))
        # An existing library without a sign-in is not handed somebody's: it may belong to another account.
        other = self.home / 'other'; (other / 'Library').mkdir(parents=True)
        third, problem = app_runtime.move(again, other); self.addCleanup(third.stop, 0)
        self.assertEqual((problem, (other / '.comic-session/state.json').exists(), self.background['start'].call_count), (None, False, 2))
        # A sign-in that cannot be read is left behind rather than stopping the change.
        (other / '.comic-session/state.json').write_text('PRIVATE nonsense')
        empty = self.home / 'empty'
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            fourth, problem = app_runtime.move(third, empty); self.addCleanup(fourth.stop, 0)
        self.assertEqual((problem, fourth.root, (empty / '.comic-session/state.json').exists()), (None, empty, False))
        self.assertNotIn('PRIVATE', ''.join(logs.output))

    def test_a_folder_that_cannot_be_used_leaves_the_app_on_the_old_one_with_the_reason(self):
        self.runtime.start()
        self.sign_in()
        blocked = self.home / 'blocked'; blocked.write_text('a file in the way')
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            back, problem = app_runtime.move(self.runtime, blocked / 'Comic Archiver'); self.addCleanup(back.stop, 0)
        self.assertEqual((problem, back.root, back.theme), (app_runtime.UNWRITABLE, self.root, self.runtime.theme))
        self.assertNotIn(str(self.home), ''.join(logs.output))
        self.assertIsNot(back, self.runtime)
        self.assertEqual((self.fetch(back.url)[0], app_runtime.saved_root()), (200, None))
        self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [self.root / '.comic-session'])
        link = self.home / 'link'; link.symlink_to(self.home / 'elsewhere', target_is_directory=True)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            again, problem = app_runtime.move(back, link / 'Comic Archiver'); self.addCleanup(again.stop, 0)
        self.assertIn('shortcut (symlink)', problem); self.assertEqual(again.root, self.root)

    def test_the_dashboard_is_handed_the_list_of_sites_when_the_app_starts(self):
        listed = [{'key': 'example', 'name': 'Example', 'available': True, 'current': True}]
        with patch('comic_archive.sites.choices', return_value=listed):
            self.runtime.start()
        self.assertIs(self.runtime.server.get_app().registry.sites, listed)

    def test_each_site_has_its_own_folder_sign_in_and_downloader_and_the_choice_outlives_the_app(self):
        example, saved = replace(sites.DEFAULT, key='example', name='Example', available=True), self.state / 'comic-archiver/site'
        def context(cookie):
            request = Request(self.runtime.url + 'api/v1/context', headers={'Cookie': cookie})
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                value = json.loads(response.read())
            return [site['key'] for site in value['sites'] if site['current']], value['site_switching'], value['workspace_ids']
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example, replace(sites.DEFAULT, key='later', name='Later', available=False))):
            self.runtime.start(); self.sign_in()
            (self.root / 'Library/kept.cbz').write_text('saved')
            url, cookie = self.runtime.url, self.fetch(self.runtime.url)[1]['Set-Cookie'].split(';')[0]
            tapas = context(cookie)[2]
            # The dashboard only leaves the request; the page waits until the window has settled it.
            choice = self.runtime.server.get_app().registry.site_choice
            self.assertIs(choice, self.runtime.site_choice)
            choice.ask(example)
            self.assertEqual((context(cookie)[:2], choice.take(), choice.take()), ((['tapas'], True), example, None))
            self.assertIsNone(self.runtime.change_site(example))
            choice.settle()
            place = self.root / 'Example'
            self.assertEqual((self.runtime.site, self.runtime.root, self.runtime.location.get(), self.runtime.session),
                             (example, self.root, str(place / 'Library'), place / '.comic-session'))
            # The same page and its cookie, on the same port, now read the other site's own library.
            switched = context(cookie)
            self.assertEqual((self.runtime.url, switched[:2]), (url, (['example'], False)))
            self.assertNotEqual(switched[2], tapas)
            # Tapas's downloader was stopped; one site's sign-in is never handed to another, so the new one waits for its own.
            self.assertEqual([call.args[0] for call in self.background['stop'].call_args_list], [self.root / '.comic-session'])
            self.assertFalse((place / '.comic-session/state.json').exists())
            self.background['start'].assert_not_called()
            self.assertEqual((app_runtime.saved_site(), saved.read_text()), (example, 'example\n'))
            # Back to Tapas: its own library and sign-in as they were, and its downloader starts; no record is needed.
            self.assertIsNone(self.runtime.change_site(sites.DEFAULT))
            self.assertEqual((self.runtime.session, (self.root / 'Library/kept.cbz').read_text(), context(cookie)),
                             (self.root / '.comic-session', 'saved', (['tapas'], False, tapas)))
            self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [self.root / '.comic-session'])
            self.assertEqual((saved.exists(), app_runtime.saved_site()), (False, sites.DEFAULT))
            # Another library folder keeps the site.
            self.assertIsNone(self.runtime.change_site(example))
            drive = self.home / 'drive'
            moved, problem = app_runtime.move(self.runtime, drive); self.addCleanup(moved.stop, 0)
            self.assertEqual((problem, moved.site, moved.location.get()), (None, example, str(drive / 'Example/Library')))
            self.assertTrue(app_runtime.is_library(drive))  # A folder only another site has saved into is a library too.
            # A site that can no longer be chosen, or nonsense, counts as none.
            for text in ('later\n', 'PRIVATE nonsense', ''):
                saved.write_text(text)
                self.assertEqual(app_runtime.saved_site(), sites.DEFAULT)
            # A site whose folder cannot be made leaves the app where it was, its downloader untouched, with the reason.
            self.assertIsNone(moved.change_site(sites.DEFAULT))
            (drive / 'Example').rename(drive / 'Example-old'); (drive / 'Example').write_text('a file in the way')
            stops = self.background['stop'].call_count
            with self.assertLogs('comic_archive.desktop', 'WARNING'):
                problem = moved.change_site(example)
            self.assertEqual((problem, moved.site, moved.root, saved.exists(), self.background['stop'].call_count),
                             (app_runtime.UNWRITABLE, sites.DEFAULT, drive, False, stops))
            self.assertEqual(self.fetch(moved.url)[0], 200)

    def test_a_switch_settles_only_the_latest_request(self):
        choice, example = app_runtime.SiteChoice(), replace(sites.DEFAULT, key='example')
        self.assertFalse(choice.waiting())
        choice.ask(example); choice.take(); choice.ask(sites.DEFAULT)
        choice.settle()  # The first request is done; the page still waits for the second.
        self.assertEqual((choice.waiting(), choice.take()), (True, sites.DEFAULT))
        choice.settle()
        self.assertFalse(choice.waiting())

    def test_the_theme_follows_the_computer_until_one_is_chosen_and_the_choice_outlives_the_app(self):
        theme, saved = self.runtime.theme, self.state / 'comic-archiver/theme'
        self.assertEqual(theme.get(), 'system')
        self.runtime.start()
        self.assertIs(self.runtime.server.get_app().registry.theme, theme)  # The dashboard's header changes it.
        theme.set('dark')
        self.assertEqual((theme.get(), saved.read_text(), app_runtime.Theme().get()), ('dark', 'dark\n', 'dark'))
        self.assertEqual(sorted(path.name for path in saved.parent.iterdir() if path.name.startswith('theme')), ['theme'])
        with self.assertRaises(ArchiveError):
            theme.set('sepia')
        self.assertEqual((theme.get(), saved.read_text()), ('dark', 'dark\n'))
        saved.write_text('PRIVATE nonsense')
        self.assertEqual(app_runtime.Theme().get(), 'system')
        # A settings folder that cannot be used keeps the choice for this run only.
        with patch('comic_archive.app_runtime.state_dir', side_effect=ArchiveError('blocked')):
            fresh = app_runtime.Theme()
            self.assertEqual(fresh.get(), 'system')
            fresh.set('light')
            self.assertEqual(fresh.get(), 'light')
        self.assertEqual(saved.read_text(), 'PRIVATE nonsense')

    def test_start_creates_private_library_and_serves_dashboard_on_a_free_loopback_port_without_worker(self):
        url = self.runtime.start()
        self.assertRegex(url, r'^http://127\.0\.0\.1:[1-9][0-9]*/$')
        self.assertEqual((url, self.runtime.url), (f'http://127.0.0.1:{self.runtime.port}/', url))
        self.assertEqual(self.runtime.server.server_address, ('127.0.0.1', self.runtime.port))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), PRIVATE_FOLDER)
        status, headers, body = self.fetch(url)
        self.assertEqual(status, 200)
        self.assertIn('id="account-download"', body)
        self.assertIn('comic_archive_local=', headers['Set-Cookie'])
        signin = self.runtime.server.get_app().registry.site_signin
        self.assertIsInstance(signin, WindowSignIn)  # No separate browser window: the app's own window signs in.
        self.assertIs(signin, self.runtime.signin)
        self.assertEqual((signin.browser, signin.workspace.session), (DESKTOP_BROWSER, self.root / '.comic-session'))
        self.assertEqual(signin.status(), {'status': 'idle'})
        self.assertEqual(self.runtime.start_worker(), 'signed_out')
        self.background['start'].assert_not_called()
        self.assertFalse(self.runtime.busy())

    def test_unusable_library_folder_or_port_fails_before_any_worker_and_leaves_nothing_running(self):
        link = self.home / 'link'; link.symlink_to(self.home, target_is_directory=True)
        blocked = self.home / 'file'; blocked.write_text('not a folder')
        for root in (link / 'Comic Archiver', blocked, self.home / 'missing/parent'):
            runtime = app_runtime.Runtime(root)
            with self.assertRaises(ArchiveError): runtime.start()
            self.assertEqual(runtime.start_worker(), 'signed_out')
            self.assertFalse(runtime.busy())
            self.assertEqual(runtime.stop(), 'not_started')
        self.assertEqual(blocked.read_text(), 'not a folder')
        with patch('comic_archive.app_runtime.create_server', side_effect=ArchiveError('port unavailable')):
            with self.assertRaises(ArchiveError): self.runtime.start()
        self.assertIsNone(self.runtime.thread)
        self.background['start'].assert_not_called(); self.background['stop'].assert_not_called()

    def test_saved_sign_in_starts_the_slow_built_in_engine_downloader_and_replaces_an_older_version(self):
        session = self.sign_in(); self.runtime.start()
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.background['start'].assert_called_once_with(session, DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL, restart=False)
        self.assertEqual((DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL), ('qt', 1.0))
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION - 1}
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.assertTrue(self.background['start'].call_args.kwargs['restart'])
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION}
        self.background['start'].return_value = {'running': True, 'already_running': True}
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.assertFalse(self.background['start'].call_args.kwargs['restart'])
        self.assertIsNone(self.runtime._retry)

    def test_window_sign_in_is_served_by_the_dashboard_and_shares_the_launch_lock_and_closing_flag(self):
        session = self.sign_in(); self.runtime.start()
        registry, signin = self.runtime.server.get_app().registry, self.runtime.signin
        self.assertEqual(signin.start(), {'status': 'running'})
        self.assertEqual(registry.site_signin_status(LOCAL_OWNER, registry.workspace_id), {'status': 'running'})
        self.assertEqual(self.runtime.busy(), 'signin')
        self.assertTrue(signin.take())
        cookies = [{'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': '.tapas.io', 'path': '/', 'expires': None,
                    'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}]
        locked = []
        self.background['start'].side_effect = lambda *args, **kwargs: locked.append(self.runtime._launch.locked()) or {'running': True}
        with patch('comic_archive.window_signin.NetworkGuard') as guard:
            self.assertEqual(signin.finish('11', cookies), {'status': 'connected'})
            guard.assert_called_once_with(self.root / 'Library')
            self.assertEqual(locked, [True])  # The start-up launch and stop() wait on the same lock.
            self.background['stop'].assert_called_once_with(session, wait_seconds=35)
            self.background['start'].assert_called_once_with(session, DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL)
            self.assertEqual([item['name'] for item in load_session(session)['cookies']], ['session'])
            self.assertIsNone(self.runtime.busy())
            self.assertEqual(signin.start(), {'status': 'running'})
            self.runtime.stop(0)
            self.assertEqual(signin.status(), {'status': 'cancelled', 'code': 'signin_incomplete'})
            # A sign-in that only now finds an account must not start a downloader behind the closed app.
            signin.start()
            self.assertEqual(signin.finish('11', cookies), {'status': 'cancelled', 'code': 'signin_incomplete'})
        self.background['start'].assert_called_once()

    def test_draining_or_launching_downloader_defers_then_starts_once_after_it_and_sign_in_have_gone(self):
        self.sign_in(); self.runtime.start()
        self.background['status'].reset_mock()  # Starting looks once, for a batch that closing interrupted.
        signin = self.runtime.server.get_app().registry.site_signin
        draining = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        gone = {'running': False}
        for first in ({'running': True, 'start_deferred': True, 'reason': 'worker_draining'}, LibraryBusy('PRIVATE')):
            self.background['start'].reset_mock()
            self.background['start'].side_effect = [first, {'running': True}]
            # First attempt, then: draining, legacy claim, sign-in running, free, and the retry's own check.
            self.background['status'].side_effect = [draining, draining, {'running': None}, gone, gone, gone]
            with patch.object(signin, 'status', side_effect=[{'status': 'running'}, {'status': 'idle'}]) as signing:
                self.assertEqual(self.runtime.start_worker(), 'deferred')
                self.runtime._retry.join(10)
                self.assertFalse(self.runtime._retry.is_alive())
                self.assertEqual(signing.call_count, 2)
            self.assertEqual(self.background['start'].call_count, 2)
            self.assertEqual(self.background['status'].call_count, 6)
            self.background['status'].reset_mock()

    def test_deferred_start_yields_to_a_sign_in_started_worker_unreadable_state_and_closing(self):
        self.sign_in(); self.runtime.start()
        current = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        self.background['start'].return_value = {'running': True, 'start_deferred': True}
        for later in ({'running': True, 'stop_requested': False}, ArchiveError('PRIVATE')):
            self.background['start'].reset_mock()
            self.background['status'].side_effect = [current, later]
            self.assertEqual(self.runtime.start_worker(), 'deferred')
            self.runtime._retry.join(10)
            self.assertFalse(self.runtime._retry.is_alive())
            self.background['start'].assert_called_once()
        self.background['start'].reset_mock()
        self.background['status'].side_effect = [current, OSError('PRIVATE'), {'running': False}, {'running': False}]
        self.assertEqual(self.runtime.start_worker(), 'deferred')
        self.runtime._retry.join(10)
        self.assertFalse(self.runtime._retry.is_alive())
        self.assertEqual(self.background['start'].call_count, 2)  # retried after the unreadable moment
        self.background['start'].reset_mock()
        self.background['status'].side_effect = None
        self.background['status'].return_value = current
        self.assertEqual(self.runtime.start_worker(), 'deferred')
        self.runtime.stop(0)
        self.runtime._retry.join(10)
        self.assertFalse(self.runtime._retry.is_alive())
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.background['start'].assert_called_once()

    def test_a_deferred_start_ends_when_the_window_switches_to_another_site(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        self.sign_in(); self.runtime.start()
        self.background['start'].return_value = {'running': True, 'start_deferred': True}
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        self.assertEqual(self.runtime.start_worker(), 'deferred')
        retry = self.runtime._retry
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            self.assertIsNone(self.runtime.change_site(example))
        # Tapas's downloader has gone; the other site, not signed in, never gets one from Tapas's wait.
        self.background['status'].return_value = {'running': False}
        retry.join(10)
        self.assertFalse(retry.is_alive())
        self.background['start'].assert_called_once()

    def test_downloader_start_failures_keep_the_dashboard_open_and_log_no_private_text(self):
        log = app_runtime.configure_logging()
        self.sign_in(); url = self.runtime.start()
        for failure in (ArchiveError('PRIVATE_COOKIE'), OSError('/PRIVATE_PATH')):
            self.background['start'].side_effect = failure
            self.assertEqual(self.runtime.start_worker(), 'failed')
        self.background['start'].side_effect = None
        for result in ({'running': False, 'start_failed': True}, {'running': None, 'startup_pending': True}):
            self.background['start'].return_value = result
            self.assertEqual(self.runtime.start_worker(), 'failed')
        self.assertIsNone(self.runtime._retry)
        self.assertEqual(self.fetch(url)[0], 200)
        text = log.read_text()
        self.assertIn('ArchiveError', text); self.assertIn('OSError', text)
        self.assertNotIn('PRIVATE', text)

    def test_busy_names_a_download_worker_task_or_sign_in_and_never_guesses_from_unreadable_state(self):
        self.assertIsNone(self.runtime.busy())
        self.runtime.start()
        registry = self.runtime.server.get_app().registry
        self.assertIsNone(self.runtime.busy())
        with patch.object(registry.site_signin, 'status', return_value={'status': 'running'}):
            self.assertEqual(self.runtime.busy(), 'signin')
        with patch.object(registry, 'account_status', return_value={'status': 'running'}) as account:
            self.assertEqual(self.runtime.busy(), 'download')
            account.return_value = {'status': 'stopped'}
            self.assertIsNone(self.runtime.busy())
        self.background['status'].return_value = {'running': True, 'state': 'busy'}
        self.assertEqual(self.runtime.busy(), 'download')
        self.background['status'].return_value = {'running': True, 'state': 'ready'}
        self.assertIsNone(self.runtime.busy())
        log = app_runtime.configure_logging()
        for failure in (ArchiveError('PRIVATE'), OSError('PRIVATE')):
            self.background['status'].side_effect = failure
            self.assertIsNone(self.runtime.busy())
        self.assertEqual(log.read_text().count('download state is unreadable'), 2)
        self.assertNotIn('PRIVATE', log.read_text())

    def test_library_that_exists_but_cannot_be_used_gets_a_plain_message_and_a_class_only_log_entry(self):
        log = app_runtime.configure_logging()
        for failure in (PermissionError('/PRIVATE'), sqlite3.OperationalError('PRIVATE'), OSError(28, 'PRIVATE')):
            runtime = app_runtime.Runtime(self.root)
            with patch('comic_archive.app_runtime.create_server', side_effect=failure):
                with self.assertRaises(ArchiveError) as caught: runtime.start()
            self.assertEqual(str(caught.exception), app_runtime.UNWRITABLE)
            self.assertIsNone(runtime.thread)
        self.assertNotIn('PRIVATE', log.read_text()); self.assertIn('OperationalError', log.read_text())
        with patch('comic_archive.app_runtime.create_server', side_effect=ArchiveError('Cannot bind the local dashboard. Start it in your normal terminal.')):
            with self.assertRaises(ArchiveError) as caught: self.runtime.start()
        self.assertNotIn('terminal', str(caught.exception))
        with patch('comic_archive.app_runtime.create_server', side_effect=ArchiveError('Hosted registry key is invalid.')):
            with self.assertRaisesRegex(ArchiveError, 'Hosted registry'): self.runtime.start()
        link = self.home / 'link'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ArchiveError) as caught: app_runtime.Runtime(link / 'Comic Archiver').start()
        self.assertIn('link', str(caught.exception)); self.assertIn('symlink', str(caught.exception))

    def test_downloader_that_finished_starting_after_the_window_closed_is_asked_to_stop_at_once(self):
        self.sign_in(); self.runtime.start()
        def late(*args, **kwargs):
            self.runtime._closing.set()  # stop() gave up waiting while this was still starting
            return {'running': True}
        self.background['start'].side_effect = late
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.background['stop'].assert_called_once_with(self.root / '.comic-session', wait_seconds=0)
        self.background['stop'].side_effect = OSError('PRIVATE')
        self.runtime._closing.clear()
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.assertEqual(self.background['stop'].call_count, 2)

    def test_stop_closes_the_dashboard_then_interrupts_the_downloader_without_pausing_the_account_download(self):
        self.sign_in(); url = self.runtime.start()
        self.assertEqual(self.runtime.start_worker(), 'running')
        registry = self.runtime.server.get_app().registry
        seen, session = [], self.root / '.comic-session'
        def drain(session, *, wait_seconds):
            seen.append(('ask', session, self.runtime.server.socket.fileno(), wait_seconds))
            return {'running': True}
        def look(session):
            seen.append(('look', session))
            return {'running': len(seen) < 4}
        self.background['stop'].side_effect = drain; self.background['status'].side_effect = look
        with patch('comic_archive.account_export.request_stop') as pause, patch.object(registry.site_signin, 'close') as close, \
                patch('comic_archive.account_ui.interrupt', side_effect=lambda workspace: seen.append(workspace.library)), \
                patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            pause.assert_not_called(); close.assert_called_once_with(); end.assert_not_called()
        # Asked to leave before its batch is interrupted, so it goes without reading that batch's receipt.
        self.assertEqual(seen, [('ask', session, -1, 0), self.root / 'Library', ('look', session), ('look', session)])
        self.runtime.thread.join(5)
        self.assertFalse(self.runtime.thread.is_alive())
        with self.assertRaises(URLError): self.fetch(url)
        self.assertEqual(self.runtime.stop(), 'stopped_before')
        self.assertEqual(self.background['stop'].call_count, 1)

    def test_a_downloader_that_is_not_running_is_only_asked_once_and_one_that_will_not_go_is_ended(self):
        self.runtime.start()
        with patch('comic_archive.account_ui.interrupt') as interrupt, patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            interrupt.assert_not_called(); end.assert_not_called()
        self.background['stop'].assert_called_once_with(self.root / '.comic-session', wait_seconds=0)
        for ended, outcome in ((True, 'stopped'), (False, 'finishing')):
            other = app_runtime.Runtime(self.home / outcome); self.addCleanup(other.stop, 0)
            other.start()
            alive = [True]
            def end(session, ended=ended, alive=alive):
                alive[0] = not ended
                return ended
            self.background['stop'].reset_mock(); self.background['stop'].return_value = {'running': True}
            self.background['status'].side_effect = lambda session, alive=alive: {'running': alive[0]}
            with patch('comic_archive.account_ui.interrupt', side_effect=OSError('PRIVATE')) as interrupt, \
                    patch('comic_archive.app_runtime.end_downloader', side_effect=end) as ending:
                self.assertEqual(other.stop(.2), outcome)
            interrupt.assert_called_once(); ending.assert_called_once_with(self.home / outcome / '.comic-session')
            self.background['stop'].assert_called_once_with(self.home / outcome / '.comic-session', wait_seconds=0)
            self.background['status'].side_effect = None

    def test_a_downloader_too_busy_to_be_asked_is_asked_again_and_one_that_cannot_be_asked_is_still_ended(self):
        self.runtime.start()
        self.background['stop'].side_effect = [LibraryBusy('PRIVATE'), LibraryBusy('PRIVATE'), {'running': False}]
        with patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            end.assert_not_called()
        self.assertEqual(self.background['stop'].call_count, 3)
        for ended, outcome in ((True, 'stopped'), (False, 'failed')):
            other = app_runtime.Runtime(self.home / outcome); self.addCleanup(other.stop, 0)
            other.start()
            self.background['stop'].side_effect = LibraryBusy('PRIVATE')
            self.background['status'].return_value = {'running': not ended}
            with self.assertLogs(app_runtime.LOG, 'WARNING') as logs, \
                    patch('comic_archive.app_runtime.end_downloader', return_value=ended) as end:
                self.assertEqual(other.stop(.2), outcome)
            end.assert_called_once_with(self.home / outcome / '.comic-session')
            self.assertIn('did not accept the stop request: LibraryBusy', logs.output[0]); self.assertNotIn('PRIVATE', ''.join(logs.output))
            self.background['status'].return_value = {'running': False, 'state': 'stopped'}

    @unittest.skipUnless(Path('/proc/self/cmdline').is_file(), 'needs /proc')
    def test_only_the_running_downloader_of_this_library_is_ended(self):
        session = self.sign_in(); folder = session / 'worker'; folder.mkdir()
        def child(*words):
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', *words],
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            self.addCleanup(process.wait, 5); self.addCleanup(process.kill)
            return process
        mine = child('--session-dir', str(session), 'browser-worker')
        other = child('--session-dir', str(self.home / 'elsewhere/.comic-session'), 'browser-worker')
        def claims(pid):
            (folder / 'status.json').write_text(json.dumps({'pid': pid, 'state': 'busy'}))
        self.assertFalse(app_runtime.end_downloader(session))  # Nothing recorded.
        with patch('comic_archive.app_runtime.background.running', return_value=False):
            claims(mine.pid); self.assertFalse(app_runtime.end_downloader(session))
        with patch('comic_archive.app_runtime.background.running', return_value=True):
            for pid in (other.pid, 1, 'PRIVATE', 2 ** 22 + 7):
                claims(pid); self.assertFalse(app_runtime.end_downloader(session))
            self.assertIsNone(mine.poll()); self.assertIsNone(other.poll())
            claims(mine.pid); self.assertTrue(app_runtime.end_downloader(session))
        self.assertEqual(mine.wait(5), -15)
        self.assertIsNone(other.poll())

    def test_stop_waits_for_a_launching_downloader_only_within_its_budget_and_reports_failures(self):
        self.sign_in(); self.runtime.start()
        self.background['stop'].return_value = self.background['status'].return_value = {'running': True}
        with self.runtime._launch:
            self.assertEqual(self.runtime.stop(.05), 'finishing')
        self.assertEqual(self.background['stop'].call_args.kwargs, {'wait_seconds': 0})
        self.assertTrue(self.runtime._launch.acquire(blocking=False)); self.runtime._launch.release()
        for failure in (ArchiveError('PRIVATE'), OSError('PRIVATE')):
            other = app_runtime.Runtime(self.home / type(failure).__name__); self.addCleanup(other.stop, 0)
            other.start()
            self.background['stop'].side_effect = failure
            self.assertEqual(other.stop(2), 'failed')
            self.assertEqual(other.server.socket.fileno(), -1)
            self.assertTrue(other._launch.acquire(blocking=False)); other._launch.release()
            self.assertEqual(self.background['stop'].call_args.kwargs, {'wait_seconds': 0})


    def test_a_folder_the_system_refuses_to_look_at_is_neither_a_library_nor_present(self):
        unreadable = self.home / 'unreadable'
        with patch.object(Path, 'is_dir', side_effect=PermissionError('PRIVATE')):  # What some Python versions do for a folder under an unreadable parent.
            self.assertFalse(app_runtime.is_library(unreadable))
            self.assertFalse(app_runtime.present(unreadable))

    def test_a_start_sets_the_saved_download_to_wait_for_go_and_a_failure_doing_so_does_not_keep_the_app_from_starting(self):
        with patch('comic_archive.account_ui.reopen', return_value=True) as reopen, self.assertLogs(app_runtime.LOG, 'INFO') as logs:
            self.runtime.start()
        self.assertEqual((reopen.call_args.args[0].library, reopen.call_args.kwargs), (self.root / 'Library', {'idle': True}))
        self.assertIn('waits for Go', ''.join(logs.output))
        # A downloader that outlived the app, or one whose state cannot be told, may be on a task: none is ended for it.
        for number, running in enumerate((True, None)):
            other = app_runtime.Runtime(self.home / f'other-{number}'); self.addCleanup(other.stop, 0)
            self.background['status'].return_value = {'running': running}
            with patch('comic_archive.account_ui.reopen', return_value=False) as reopen:
                other.start()
            self.assertEqual(reopen.call_args.kwargs, {'idle': False})
        failing = app_runtime.Runtime(self.home / 'failing'); self.addCleanup(failing.stop, 0)
        with patch('comic_archive.account_ui.reopen', side_effect=sqlite3.OperationalError('PRIVATE')) as reopen, \
                self.assertLogs(app_runtime.LOG, 'WARNING') as logs:
            url = failing.start()
        reopen.assert_called_once()
        self.assertEqual(self.fetch(url)[0], 200)
        self.assertNotIn('PRIVATE', ''.join(logs.output))

    def test_a_downloader_that_cannot_be_ended_is_reported_as_failed_and_the_launch_lock_is_released(self):
        self.runtime.start()
        self.background['stop'].return_value = self.background['status'].return_value = {'running': True}
        with patch('comic_archive.account_ui.interrupt'), \
                patch('comic_archive.app_runtime.end_downloader', side_effect=OSError('PRIVATE')), \
                self.assertLogs(app_runtime.LOG, 'WARNING') as logs:
            self.assertEqual(self.runtime.stop(.05), 'failed')
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.assertTrue(self.runtime._launch.acquire(blocking=False)); self.runtime._launch.release()

    @unittest.skipUnless(Path('/proc/self/cmdline').is_file(), 'needs /proc')
    def test_a_downloader_in_the_process_group_of_the_app_is_ended_alone(self):
        session = self.sign_in(); folder = session / 'worker'; folder.mkdir()
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--session-dir', str(session), 'browser-worker'],
                                   stdin=subprocess.DEVNULL)  # No session of its own: ending its group would end this process too.
        self.addCleanup(process.wait, 5); self.addCleanup(process.kill)
        self.assertEqual(os.getpgid(process.pid), os.getpgid(0))
        (folder / 'status.json').write_text(json.dumps({'pid': process.pid, 'state': 'busy'}))
        with patch('comic_archive.app_runtime.background.running', return_value=True):
            self.assertTrue(app_runtime.end_downloader(session))
        self.assertEqual(process.wait(5), -15)

    def test_changing_to_an_empty_folder_from_a_library_never_signed_in_creates_no_sign_in(self):
        self.runtime.start()
        empty = self.home / 'empty'
        fresh, problem = app_runtime.move(self.runtime, empty); self.addCleanup(fresh.stop, 0)
        self.assertEqual((problem, fresh.root, app_runtime.saved_root()), (None, empty, empty))
        self.assertFalse((empty / '.comic-session/state.json').exists())
        self.background['start'].assert_not_called()

    def test_state_folder_is_private_per_user_and_ignores_relative_overrides(self):
        self.assertEqual(app_runtime.state_dir(), self.state / 'comic-archiver')
        self.assertEqual(stat.S_IMODE((self.state / 'comic-archiver').stat().st_mode), PRIVATE_FOLDER)
        with patch.dict(os.environ, {STATE_HOME: 'relative/state'}):
            self.assertEqual(app_runtime.state_dir(), self.home / STATE_DEFAULT / 'comic-archiver')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.home / 'AppData/Local')}), patch('comic_archive.app_runtime.WINDOWS', True):
            self.assertEqual(app_runtime.state_dir(), self.home / 'AppData/Local/comic-archiver')
        (self.home / 'blocked').write_text('not a folder')
        with patch.dict(os.environ, {STATE_HOME: str(self.home / 'blocked')}):
            with self.assertRaises(ArchiveError): app_runtime.state_dir()
            self.assertIsNone(app_runtime.configure_logging())
            app_runtime.LOG.error('nowhere to write')

    def test_log_file_is_private_replaced_not_duplicated_and_never_follows_a_symlink(self):
        path = app_runtime.configure_logging()
        self.assertEqual(path, self.state / 'comic-archiver/desktop.log')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), PRIVATE_FILE)
        self.assertEqual(app_runtime.configure_logging(), path)
        handler, = app_runtime.LOG.handlers
        self.assertEqual((handler.maxBytes, handler.backupCount), (256 * 1024, 2))
        app_runtime.LOG.info('written once')
        self.assertEqual(path.read_text().count('written once'), 1)
        self.reset_logging(); path.unlink()
        path.symlink_to(self.home / 'elsewhere')
        self.assertIsNone(app_runtime.configure_logging())
        app_runtime.LOG.error('dropped')
        self.assertFalse((self.home / 'elsewhere').exists())

    def test_failure_descriptions_name_the_class_and_code_location_but_never_the_text(self):
        try:
            raise ArchiveError('PRIVATE_COOKIE https://tapas.io/?token=PRIVATE')
        except ArchiveError as error:
            text = app_runtime.describe(error)
        self.assertRegex(text, r'^ArchiveError at test_app_runtime\.py:[0-9]+$')
        self.assertEqual(app_runtime.describe(ValueError('PRIVATE')), 'ValueError')


if __name__ == '__main__':
    unittest.main()
