"""Real bridge/session/workspace effects around delayed verification and native commit.

Only provider verification is replaced. No live account, cookie deletion, download,
or B2 operation is performed; private files and shared settings are real fixtures.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from comic_archive import android_session, network_android, shared, sites
from test_app_bridge import app_bridge


VERIFIED = {'signed_in': True, 'verified': True, 'reason': None}


class NativeSignInAttemptTests(unittest.TestCase):
    def setUp(self):
        self.before = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.clean)
        self.root = Path(self.tmp.name)
        self.session = self.root / '.comic-session'
        app_bridge._workspace.update(root=None, base=None, site_key='tapas')
        for kept in (app_bridge._open, app_bridge._site_seams, app_bridge._cloud_controllers, app_bridge._catalog_registries): kept.clear()
        app_bridge._signin_attempts.clear()
        app_bridge.set_cookie_seams(lambda url: 'sid=FIXTURE_SESSION')
        self.assertTrue(self.call('prepare_workspace', root=str(self.root))['ok'])

    def clean(self):
        network_android.configure(None)
        os.chdir(self.before)
        app_bridge._workspace.update(root=None, base=None, site_key='tapas')
        app_bridge._signin_attempts.clear()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': operation, 'args': args})))

    def attempt(self, active=True, token=1, **extra):
        return self.call('signin_attempt', session_root=str(self.session), site_key='tapas',
                         attempt=token, active=active, **extra)

    def probe(self, token=1):
        return self.call('signin_probe', session_root=str(self.session), site_key='tapas', attempt=token)

    def commit(self, token=1):
        return self.call('signin_commit', session_root=str(self.session), site_key='tapas', attempt=token)

    def test_verification_prepares_but_only_current_native_commit_writes_a_record(self):
        self.assertTrue(self.attempt()['ok'])
        with patch('comic_archive.android_adapter.signed_in_probe', return_value=VERIFIED):
            self.assertEqual(self.probe()['result'], VERIFIED)
        self.assertFalse(android_session.is_signed_in(self.session))
        self.assertEqual(self.commit()['result'], VERIFIED)
        self.assertEqual(android_session.signed_in_site(self.session), sites.DEFAULT)
        self.assertEqual(json.loads((self.session / 'state.json').read_text()), {'cookies': [], 'origins': []})
        self.assertFalse(self.commit()['ok'], 'The same prepared answer cannot be replayed')
        self.assertFalse((self.session / 'worker/download.sqlite3').exists())

    def test_cancelling_after_verification_before_native_commit_keeps_the_record_absent(self):
        self.assertTrue(self.attempt()['ok'])
        with patch('comic_archive.android_adapter.signed_in_probe', return_value=VERIFIED):
            self.probe()
        self.assertTrue(self.attempt(active=False)['ok'])
        self.assertFalse(self.commit()['ok'])
        self.assertFalse(android_session.is_signed_in(self.session))

    def delayed_verification(self, change):
        entered, release = threading.Event(), threading.Event()

        def verify(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('The fixture verification was not released')
            return VERIFIED

        self.assertTrue(self.attempt()['ok'])
        with ThreadPoolExecutor(max_workers=2) as executor, \
                patch('comic_archive.android_adapter.signed_in_probe', side_effect=verify):
            future = executor.submit(self.probe)
            try:
                self.assertTrue(entered.wait(2))
                # The invalidation must finish while the provider answer is still held.
                changed = executor.submit(change).result(timeout=2)
                self.assertTrue(changed['ok'], changed)
            finally:
                release.set()
            answer = future.result(timeout=5)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['result']['reason'], 'attempt_ended')
        self.assertFalse(self.commit()['ok'])
        self.assertFalse(android_session.is_signed_in(self.session))

    def test_a_cancelled_blocking_probe_cannot_write_after_its_network_answer_arrives(self):
        self.delayed_verification(lambda: self.attempt(active=False))

    def test_signout_invalidates_a_blocking_probe_before_forgetting_the_record(self):
        self.delayed_verification(lambda: self.call('signout_record', session_root=str(self.session)))

    def test_switching_workspace_invalidates_old_probe_and_preserves_originals_and_shared_setup(self):
        original = self.root / 'Library/kept.cbz'
        original.write_bytes(b'fixture-original-archive')
        next_root = sites.folder(self.root, sites.named('webtoon'))
        self.delayed_verification(lambda: self.call('prepare_workspace', root=str(next_root),
            base_root=str(self.root), site_key='webtoon'))
        self.assertEqual(original.read_bytes(), b'fixture-original-archive')
        self.assertEqual(Path.cwd(), self.before)
        self.assertEqual(sites.of_session(next_root / '.comic-session'), sites.named('webtoon'))
        self.assertEqual(shared.folder(next_root / 'Library'), shared.folder(self.root / 'Library'))
        self.assertFalse(android_session.is_signed_in(next_root / '.comic-session'))

    def test_a_verified_answer_that_expires_before_native_commit_cannot_be_saved(self):
        self.assertTrue(self.attempt(timeout_ms=1_000)['ok'])
        with patch('comic_archive.android_adapter.signed_in_probe', return_value=VERIFIED):
            self.assertTrue(self.probe()['result']['signed_in'])
        later = time.monotonic() + 2
        with patch.object(app_bridge.time, 'monotonic', return_value=later):
            self.assertFalse(self.commit()['ok'])
        self.assertFalse(android_session.is_signed_in(self.session))

    def test_native_attempt_cannot_be_downgraded_to_an_untagged_write_or_another_site(self):
        self.assertTrue(self.attempt()['ok'])
        with patch('comic_archive.android_adapter.signed_in_probe') as probe, \
                patch('comic_archive.android_readers.signed_in_probe') as other_probe:
            legacy = self.call('signin_probe', session_root=str(self.session))
            other = self.call('signin_probe', session_root=str(self.session), attempt=1, site_key='webtoon')
        self.assertFalse(legacy['result']['signed_in'])
        self.assertFalse(other['result']['signed_in'])
        probe.assert_not_called()
        other_probe.assert_not_called()
        self.assertFalse(android_session.is_signed_in(self.session))

    def test_an_old_native_attempt_cannot_cancel_or_commit_a_replacement(self):
        self.assertTrue(self.attempt()['ok'])
        self.assertTrue(self.attempt(token=2)['ok'])
        self.assertTrue(self.attempt(active=False)['ok'])
        with patch('comic_archive.android_adapter.signed_in_probe', return_value=VERIFIED):
            self.assertEqual(self.probe(token=2)['result'], VERIFIED)
        self.assertFalse(self.commit()['ok'])
        self.assertTrue(self.commit(token=2)['ok'])

    def test_another_site_opens_while_one_saves_and_showing_a_saving_site_never_holds_it(self):
        next_root = self.root / 'Webtoon'
        with patch('comic_archive.worker_host.status',
                   side_effect=lambda session: {'running': session == self.session}), \
                patch('comic_archive.account_ui.reopen', return_value=False) as reopen, \
                patch.object(app_bridge, '_close_browser') as close:
            answer = self.call('prepare_workspace', root=str(next_root),
                               base_root=str(self.root), site_key='webtoon')
            self.assertTrue(answer['ok'], answer)
            self.assertEqual(reopen.call_args.args[0].root, next_root)
            reopen.reset_mock()
            close.reset_mock()
            # Back to the site that is saving: shown again, with its download left as it is.
            back = self.call('prepare_workspace', root=str(self.root))
            self.assertEqual(back['result'], {'root': str(self.root), 'library': str(self.root / 'Library'),
                'session': str(self.session), 'site_key': 'tapas', 'held': False})
            reopen.assert_not_called()
            close.assert_not_called()
            self.assertEqual(app_bridge._workspace['root'], self.root)
            self.assertEqual(set(app_bridge._open), {self.root, next_root})
            # A saving folder this app did not open itself is still refused.
            app_bridge._open.clear()
            self.assertFalse(self.call('prepare_workspace', root=str(self.root))['ok'])
            reopen.assert_not_called()
        self.assertEqual(Path.cwd(), self.before)

    def test_every_opened_site_keeps_its_own_settings_and_connection_report(self):
        (self.root / 'net.json').write_text('{"connection":"wifi","metered":false}')
        next_root = self.root / 'Webtoon'
        self.assertTrue(self.call('prepare_workspace', root=str(next_root),
            base_root=str(self.root), site_key='webtoon')['ok'])
        (next_root / 'net.json').write_text('{"connection":"cellular","metered":true}')
        shown = self.call('settings', root=str(next_root))['result']
        other = self.call('settings', root=str(self.root))['result']
        self.assertEqual((shown['connection'], shown['allowed']), ('cellular', False))
        self.assertEqual((other['connection'], other['allowed']), ('wifi', True))
        # A folder this app never opened is still refused.
        self.assertFalse(self.call('settings', root=str(self.root / 'Tappytoon'))['ok'])

    def test_each_site_starts_with_its_own_sign_in_store_and_only_its_own_download_guards_it(self):
        next_root = self.root / 'Webtoon'
        self.assertTrue(self.call('prepare_workspace', root=str(next_root),
            base_root=str(self.root), site_key='webtoon')['ok'])
        first, second, later = (lambda url: 'a=1'), (lambda url: 'b=2'), (lambda url: 'c=3')
        self.addCleanup(app_bridge._seams.update, site_key=None)
        app_bridge.set_cookie_seams(first, None, 'tapas')
        app_bridge.set_cookie_seams(second, None, 'webtoon')
        started = []
        def start(session, library, source, sink, status_path, **options):
            started.append((session, source))
            return {'running': True}
        def roots(root):
            return {'session_root': str(root / '.comic-session'), 'library_root': str(root / 'Library'),
                    'network_status_path': str(root / 'net.json')}
        with patch('comic_archive.worker_host.start_in_process', side_effect=start):
            self.assertTrue(self.call('worker_start', **roots(self.root))['ok'])
            self.assertTrue(self.call('worker_start', **roots(next_root))['ok'])
        self.assertEqual(started, [(self.session, first), (next_root / '.comic-session', second)])
        with patch('comic_archive.worker_host.status',
                   side_effect=lambda session: {'running': session == self.session}):
            app_bridge.set_cookie_seams(later, None, 'webtoon')
            # Showing the saving site again registers its own store once more, which changes nothing.
            app_bridge.set_cookie_seams(first, None, 'tapas')
            with self.assertRaises(app_bridge._BridgeError):
                app_bridge.set_cookie_seams(later, None, 'tapas')
            with self.assertRaises(app_bridge._BridgeError):
                app_bridge.deactivate_cookie_seams()
        self.assertIs(app_bridge._cookie_seams('webtoon')[0], later)
        self.assertIs(app_bridge._cookie_seams('tapas')[0], first)
        # A store registered for one site never answers for another.
        app_bridge._site_seams.pop('tapas')
        app_bridge._seams.update(site_key='webtoon')
        with self.assertRaises(app_bridge._BridgeError):
            app_bridge._cookie_seams('tapas')

    def test_pausing_or_stopping_another_site_leaves_the_first_sites_browser_alone(self):
        next_root = self.root / 'Webtoon'
        self.assertTrue(self.call('prepare_workspace', root=str(next_root),
            base_root=str(self.root), site_key='webtoon')['ok'])
        with patch('comic_archive.account_ui.pause') as pause, \
                patch('comic_archive.worker_host.request_stop', return_value={'running': False}), \
                patch.object(app_bridge, '_close_browser') as close:
            self.assertTrue(self.call('pause', root=str(next_root))['ok'])
            self.assertIsNone(pause.call_args.kwargs['on_stop_requested'])
            self.assertTrue(self.call('worker_stop', session_root=str(next_root / '.comic-session'))['ok'])
            close.assert_not_called()
            # The first site is not the one showing, and is still worked on.
            self.assertTrue(self.call('pause', root=str(self.root))['ok'])
            self.assertIs(pause.call_args.kwargs['on_stop_requested'], app_bridge._interrupt_browser)
            self.assertTrue(self.call('worker_stop', session_root=str(self.session))['ok'])
            close.assert_called_once_with()

    def test_wrong_folder_site_and_invalid_attempt_shapes_are_refused_before_network(self):
        wrong = self.call('prepare_workspace', root=str(self.root / 'Tapas'),
                          base_root=str(self.root), site_key='webtoon')
        self.assertFalse(wrong['ok'])
        for token in (True, 0, -1, '1', 2**63):
            with self.subTest(token=token):
                self.assertFalse(self.attempt(token=token)['ok'])
        self.assertFalse(self.attempt(timeout_ms=900_001)['ok'])
        self.assertFalse(android_session.is_signed_in(self.session))
