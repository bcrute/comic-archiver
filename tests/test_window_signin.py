import json
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive.account_ui import DESKTOP_REQUEST_INTERVAL
from comic_archive.hosted import Workspace
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold
from comic_archive.session import load_session, store_session
from comic_archive.window_signin import WindowSignIn, storage_state

RUNNING = {'status': 'running'}
CANCELLED = {'status': 'cancelled', 'code': 'signin_incomplete'}
EXPIRED = {'status': 'error', 'code': 'signin_incomplete'}
FAILED = {'status': 'error', 'code': 'signin_failed'}


def cookie(name='session', value='PRIVATE_COOKIE', domain='.tapas.io', path='/', expires=None, **more):
    return {'name': name, 'value': value, 'domain': domain, 'path': path, 'expires': expires,
            'httpOnly': True, 'secure': True, 'sameSite': 'Lax'} | more


class StorageStateTests(unittest.TestCase):
    def test_session_expiring_and_http_only_cookies_take_the_shape_the_downloader_reads(self):
        state = storage_state([cookie(), cookie('remember', 'PRIVATE_TOKEN', 'tapas.io', '/account', 1790893649, httpOnly=False, secure=False)],
                              [['theme', 'dark'], ('reader', '{"zoom":1}')])
        self.assertEqual(state, {
            'cookies': [{'name': 'remember', 'value': 'PRIVATE_TOKEN', 'domain': 'tapas.io', 'path': '/account', 'expires': 1790893649,
                         'httpOnly': False, 'secure': False, 'sameSite': 'Lax'},
                        {'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': '.tapas.io', 'path': '/', 'expires': -1,
                         'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}],
            'origins': [{'origin': 'https://tapas.io', 'localStorage': [{'name': 'reader', 'value': '{"zoom":1}'},
                                                                         {'name': 'theme', 'value': 'dark'}]}]})
        self.assertEqual(json.loads(json.dumps(state)), state)

    def test_same_site_follows_the_cookie_and_an_unset_or_unknown_value_is_lax(self):
        for given, saved in [('Strict', 'Strict'), ('Lax', 'Lax'), ('None_', 'None'), ('None', 'None'), ('Default', 'Lax'),
                             (None, 'Lax'), ('strict', 'Lax'), (2, 'Lax')]:
            self.assertEqual(storage_state([cookie(sameSite=given)])['cookies'][0]['sameSite'], saved)

    def test_only_tapas_own_cookies_are_kept_in_one_stable_order(self):
        mixed = [cookie('b'), cookie('a', path='/z'), cookie('a', path='/b'), cookie('oauth', domain='.google.com'),
                 cookie('cdn', domain='cdn.tapas.io'), cookie('m', domain='.m.tapas.io'), cookie('fake', domain='tapas.io.example.com'),
                 cookie('nottapas', domain='nottapas.io'), cookie('host', domain='tapas.io'), cookie('b', value='NEWER')]
        expected = [('a', '.tapas.io', '/b'), ('a', '.tapas.io', '/z'), ('b', '.tapas.io', '/'), ('host', 'tapas.io', '/')]
        for order in (mixed, mixed[::-1]):
            state = storage_state(order)
            self.assertEqual([(item['name'], item['domain'], item['path']) for item in state['cookies']], expected)
            self.assertEqual(state['origins'], [])
        self.assertEqual(storage_state(mixed)['cookies'][2]['value'], 'NEWER')  # The later record replaces the earlier one.
        self.assertEqual(storage_state([cookie(path='')])['cookies'][0]['path'], '/')

    def test_expiry_is_unix_seconds_or_a_session_cookie_and_never_a_guess(self):
        for given, saved in [(None, -1), (-1, -1), (0, -1), (1790893649, 1790893649), (1790893649.5, 1790893649.5),
                             (1790893649525, 1790893649.525)]:
            self.assertEqual(storage_state([cookie(expires=given)])['cookies'][0]['expires'], saved)
        for bad in ('PRIVATE', True, float('nan'), -20, 10 ** 18):
            with self.assertRaises(ArchiveError) as caught:
                storage_state([cookie(expires=bad)])
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_malformed_records_and_a_sign_in_without_tapas_cookies_are_refused_without_echoing_them(self):
        for bad in ([('session', 'PRIVATE_COOKIE', '.tapas.io', '/')], [{'name': 'session', 'value': 'PRIVATE_COOKIE'}],
                    [cookie(value=b'PRIVATE_COOKIE')], [cookie(domain=None)], [None], [], [cookie(domain='.google.com')]):
            with self.assertRaises(ArchiveError) as caught:
                storage_state(bad)
            self.assertNotIn('PRIVATE', str(caught.exception))
        state = storage_state([cookie()], [['only-a-name'], ['name', 7], None, 'ab', ['kept', 'value'], ['kept', 'latest']])
        self.assertEqual(state['origins'], [{'origin': 'https://tapas.io', 'localStorage': [{'name': 'kept', 'value': 'latest'}]}])


class WindowSignInTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.workspace = Workspace('a' * 32, Path(temporary.name).resolve())
        self.session, self.state = self.workspace.session, self.workspace.session / 'state.json'
        self.session.mkdir(); self.state.write_text('OLD_PRIVATE_SESSION')
        with Inventory(self.workspace.library): pass
        self.guard = Mock()
        self.stop, self.start = Mock(return_value={'running': False}), Mock(return_value={'running': True})
        self.clock = Mock(return_value=1000.0)
        for name, value in [('NetworkGuard', self.guard), ('background.stop', self.stop), ('background.start', self.start),
                            ('time', SimpleNamespace(monotonic=self.clock))]:
            wrapper = patch('comic_archive.window_signin.' + name, value); wrapper.start(); self.addCleanup(wrapper.stop)
        self.launch, self.closing = threading.Lock(), threading.Event()
        self.signin = WindowSignIn(self.workspace, self.launch, self.closing)
        self.cookies = [cookie(), cookie('oauth', 'OTHER_PRIVATE', '.google.com')]

    def finish(self, key='11'):
        return self.signin.finish(key, self.cookies, [['theme', 'dark']])

    def untouched(self):
        self.assertEqual(self.state.read_text(), 'OLD_PRIVATE_SESSION')
        self.start.assert_not_called()

    def test_start_records_one_request_for_the_window_and_a_second_click_adds_none(self):
        self.assertEqual((self.signin.status(), self.signin.browser), ({'status': 'idle'}, 'qt'))
        self.assertFalse(self.signin.take())
        self.assertEqual(self.signin.start(), RUNNING)
        self.assertEqual(self.signin.start(), RUNNING)
        self.assertTrue(self.signin.take())
        self.assertFalse(self.signin.take())
        self.assertEqual(self.signin.start(), RUNNING)
        self.assertFalse(self.signin.take())
        self.assertEqual(self.signin.status(), RUNNING)
        self.guard.assert_not_called(); self.stop.assert_not_called(); self.untouched()
        default = WindowSignIn(self.workspace)
        self.assertIsNot(default._launch, self.launch); self.assertFalse(default._closing.is_set())

    def test_finish_checks_the_account_stops_the_old_downloader_saves_privately_then_starts_the_slow_one(self):
        order = []
        self.stop.side_effect = lambda *args, **kwargs: order.append(('stop', self.state.read_text(), self.launch.locked())) or {'running': False}
        self.start.side_effect = lambda *args, **kwargs: order.append(('start', load_session(self.session), self.launch.locked())) or {'running': True}
        self.signin.start(); self.signin.take()
        with self.assertLogs('comic_archive.desktop', 'INFO') as logs:
            self.assertEqual(self.finish(), {'status': 'connected'})
        self.assertEqual(self.signin.status(), {'status': 'connected'})
        saved = {'cookies': [{'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': '.tapas.io', 'path': '/', 'expires': -1,
                              'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}],
                 'origins': [{'origin': 'https://tapas.io', 'localStorage': [{'name': 'theme', 'value': 'dark'}]}]}
        self.assertEqual(order, [('stop', 'OLD_PRIVATE_SESSION', True), ('start', saved, True)])
        self.assertFalse(self.launch.locked())
        self.guard.assert_called_once_with(self.workspace.library); self.guard.return_value.check.assert_called_once_with()
        self.stop.assert_called_once_with(self.session, wait_seconds=35)
        self.start.assert_called_once_with(self.session, 'qt', DESKTOP_REQUEST_INTERVAL)
        # What the downloader will load: Tapas only, private, and nothing left half-written.
        self.assertEqual(load_session(self.session), saved)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), PRIVATE_FILE)
        self.assertEqual(stat.S_IMODE(self.session.stat().st_mode), PRIVATE_FOLDER)
        self.assertEqual(sorted(path.name for path in self.session.iterdir()), ['site', 'state.json'])
        self.assertEqual((self.session / 'site').read_text(), 'tapas')
        self.assertNotIn('PRIVATE', ''.join(logs.output)); self.assertIn('sign-in ended: connected', logs.output[-1])
        # A finished sign-in can be followed by a new one.
        self.assertEqual(self.signin.start(), RUNNING); self.assertTrue(self.signin.take())

    def test_another_account_keeps_the_saved_session_and_the_running_downloader(self):
        with Inventory(self.workspace.library) as inventory: inventory.bind_account('22')
        self.signin.start()
        self.assertEqual(self.finish(), {'status': 'error', 'code': 'account_mismatch'})
        self.assertEqual(self.signin.status(), {'status': 'error', 'code': 'account_mismatch'})
        self.stop.assert_not_called(); self.untouched()
        self.signin.start()
        self.assertEqual(self.finish(key=None), {'status': 'error', 'code': 'account_mismatch'})
        self.stop.assert_not_called(); self.untouched()

    def test_network_hold_stops_a_sign_in_before_the_page_opens_and_before_anything_is_saved(self):
        self.guard.return_value.check.side_effect = NetworkPolicyHold('PRIVATE')
        self.signin.start(); self.signin.take()
        self.signin.admit()
        self.assertEqual(self.signin.status(), {'status': 'error', 'code': 'network_held'})
        self.signin.start()
        self.assertEqual(self.finish(), {'status': 'error', 'code': 'network_held'})
        self.stop.assert_not_called(); self.untouched()
        self.guard.return_value.check.side_effect = None
        self.signin.start()
        self.signin.admit()
        self.assertEqual(self.signin.status(), RUNNING)
        self.guard.return_value.check.side_effect = ArchiveError('PRIVATE_PATH settings are invalid')
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.signin.admit()
        self.assertEqual(self.signin.status(), FAILED)
        self.assertNotIn('PRIVATE', ''.join(logs.output)); self.assertIn('ArchiveError', logs.output[0])

    def test_previous_downloader_still_stopping_fails_without_saving_or_starting(self):
        self.stop.return_value = {'running': True, 'stop_wait_expired': True}
        self.signin.start()
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.assertEqual(self.finish(), FAILED)
        self.untouched()
        self.assertIn('ArchiveError', logs.output[0])
        self.stop.side_effect = OSError('/PRIVATE_PATH')
        self.signin.start()
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.assertEqual(self.finish(), FAILED)
        self.untouched()
        self.assertNotIn('PRIVATE', ''.join(logs.output)); self.assertIn('OSError', logs.output[0])

    def test_downloader_that_is_deferred_or_does_not_start_is_a_failed_sign_in_with_no_private_text(self):
        for result in ({'running': True, 'start_deferred': True, 'reason': 'worker_draining'}, {'running': False, 'start_failed': True},
                       {'running': None, 'startup_pending': True}, ArchiveError('PRIVATE_COOKIE'), RuntimeError('PRIVATE_COOKIE')):
            self.start.side_effect = [result]
            self.signin.start()
            with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
                self.assertEqual(self.finish(), FAILED)
            self.assertEqual(self.signin.status(), FAILED)
            self.assertNotIn('PRIVATE', ''.join(logs.output))
            self.assertFalse(self.launch.locked())
        self.assertEqual(self.start.call_count, 5)

    def test_unusable_cookies_fail_before_the_old_downloader_is_touched(self):
        for cookies in ([], [cookie(domain='.google.com')], [cookie(expires='PRIVATE')], [None]):
            self.cookies = cookies
            self.signin.start()
            with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
                self.assertEqual(self.finish(), FAILED)
            self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.stop.assert_not_called(); self.untouched()

    def test_cancel_before_an_account_is_found_ends_at_once_and_a_late_finish_saves_nothing(self):
        self.signin.cancel()  # Nothing to cancel yet.
        self.assertEqual(self.signin.status(), {'status': 'idle'})
        self.signin.start()
        self.signin.cancel()
        self.assertEqual(self.signin.status(), CANCELLED)
        self.assertFalse(self.signin.take())
        self.assertEqual(self.finish(), CANCELLED)
        self.signin.admit()
        self.assertEqual(self.signin.status(), CANCELLED)
        self.guard.assert_called_once(); self.stop.assert_not_called(); self.untouched()
        self.signin.start(); self.signin.take()
        self.signin.close()
        self.assertEqual(self.signin.status(), CANCELLED)

    def test_cancel_after_an_account_is_found_stops_before_saving_but_never_undoes_a_saved_sign_in(self):
        def cancelled_while_stopping(*args, **kwargs):
            self.signin.close()
            # Still running to the dashboard, and no second sign-in can begin, until this one has decided.
            self.assertEqual((self.signin.status(), self.signin.start()), (RUNNING, RUNNING))
            return {'running': False}
        self.stop.side_effect = cancelled_while_stopping
        self.signin.start(); self.signin.take()
        self.assertEqual(self.finish(), CANCELLED)
        self.assertEqual(self.signin.status(), CANCELLED)
        self.assertFalse(self.signin.take())
        self.untouched()
        self.stop.side_effect = None
        self.start.side_effect = lambda *args, **kwargs: self.signin.cancel() or {'running': True}
        self.signin.start()
        self.assertEqual(self.finish(), {'status': 'connected'})
        self.assertEqual(load_session(self.session)['cookies'][0]['name'], 'session')

    def test_sign_in_left_open_for_fifteen_minutes_expires_but_never_while_it_is_being_saved(self):
        self.signin.start()
        self.clock.return_value = 1000.0 + 899
        self.assertEqual(self.signin.status(), RUNNING)
        self.clock.return_value = 1000.0 + 900
        self.assertFalse(self.signin.take())
        self.assertEqual(self.signin.status(), EXPIRED)
        self.assertEqual(self.finish(), EXPIRED)
        self.signin.fail('signin_failed'); self.signin.cancel()
        self.assertEqual(self.signin.status(), EXPIRED)
        self.stop.assert_not_called(); self.untouched()
        self.assertEqual(self.signin.start(), RUNNING)  # A new attempt gets a new fifteen minutes.
        self.clock.return_value += 899
        self.assertTrue(self.signin.take())

        def slow(*args, **kwargs):
            self.clock.return_value += 5000
            self.assertEqual(self.signin.status(), RUNNING)
            return {'running': False}
        self.stop.side_effect = slow
        self.assertEqual(self.finish(), {'status': 'connected'})

    def test_window_reports_a_failure_with_a_known_code_only_while_a_sign_in_is_open(self):
        self.signin.fail('signin_failed')
        self.assertEqual(self.signin.status(), {'status': 'idle'})
        self.signin.start()
        with self.assertRaises(ArchiveError):
            self.signin.fail('PRIVATE_DETAIL')
        self.assertEqual(self.signin.status(), RUNNING)
        self.signin.fail('signin_failed')
        self.assertEqual(self.signin.status(), FAILED)
        self.assertFalse(self.signin.take())
        self.assertEqual(self.finish(), FAILED)
        self.untouched()

    def test_app_that_is_closing_saves_nothing_and_never_leaves_a_new_downloader_running(self):
        self.closing.set()
        self.signin.start()
        self.assertEqual(self.finish(), CANCELLED)
        self.stop.assert_not_called(); self.untouched()
        self.closing.clear()
        self.stop.side_effect = lambda *args, **kwargs: self.closing.set() or {'running': False}
        self.signin.start()
        self.assertEqual(self.finish(), CANCELLED)
        self.untouched()
        # The window closed while the new downloader was starting: it is asked to stop at once.
        self.closing.clear(); self.stop.reset_mock(side_effect=True)
        self.stop.return_value = {'running': False}
        self.start.side_effect = lambda *args, **kwargs: self.closing.set() or {'running': True}
        self.signin.start()
        self.assertEqual(self.finish(), CANCELLED)
        self.assertEqual([call.kwargs for call in self.stop.call_args_list], [{'wait_seconds': 35}, {'wait_seconds': 0}])
        self.closing.clear()
        self.stop.side_effect = [{'running': False}, OSError('/PRIVATE_PATH')]
        self.signin.start()
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.assertEqual(self.finish(), CANCELLED)
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.assertFalse(self.launch.locked())

    def test_finish_waits_for_the_runtimes_own_downloader_launch(self):
        self.signin.start()
        results = []
        with self.launch:
            worker = threading.Thread(target=lambda: results.append(self.finish()), daemon=True)
            worker.start()
            worker.join(.2)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.signin.status(), RUNNING)
            self.stop.assert_not_called(); self.untouched()
        worker.join(10)
        self.assertEqual(results, [{'status': 'connected'}])


class StoreSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_converted_window_cookies_round_trip_through_the_private_session_file(self):
        folder = self.root / 'new' / '.comic-session'
        state = storage_state([cookie(expires=1790893649), cookie('visit', 'PRIVATE_VISIT')], [['theme', 'dark']])
        store_session(json.loads(json.dumps(state)), folder)
        self.assertEqual(load_session(folder), state)
        self.assertEqual(stat.S_IMODE((folder / 'state.json').stat().st_mode), PRIVATE_FILE)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), PRIVATE_FOLDER)
        self.assertFalse((folder / 'state.tmp').exists())
        # Saving again replaces the file whole, and foreign entries never reach it.
        (folder / 'state.json').chmod(0o644)
        store_session({'cookies': state['cookies'][:1] + [cookie('oauth', domain='.google.com', expires=-1)],
                       'origins': [{'origin': 'https://accounts.google.com', 'localStorage': []}]}, folder)
        self.assertEqual(load_session(folder), {'cookies': state['cookies'][:1], 'origins': []})
        self.assertEqual(stat.S_IMODE((folder / 'state.json').stat().st_mode), PRIVATE_FILE)

    def test_symlinked_session_folder_or_files_are_refused_and_nothing_is_written_through_them(self):
        state = storage_state([cookie()])
        real = self.root / 'real'; real.mkdir()
        (self.root / 'link').symlink_to(real, target_is_directory=True)
        elsewhere = self.root / 'elsewhere.json'; elsewhere.write_text('UNTOUCHED')
        with self.assertRaises(ArchiveError):
            store_session(dict(state), self.root / 'link')
        with self.assertRaises(ArchiveError):
            store_session(dict(state), self.root / 'link' / '.comic-session')
        for name in ('state.json', 'state.tmp'):
            folder = self.root / name.replace('.', '-'); folder.mkdir()
            (folder / name).symlink_to(elsewhere)
            with self.assertRaises(ArchiveError):
                store_session(dict(state), folder)
        self.assertEqual(elsewhere.read_text(), 'UNTOUCHED')
        self.assertEqual(list(real.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
