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
        self.assertFalse((self.session / 'worker/account-export.json').exists())

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
        self.assertEqual(Path.cwd(), next_root)
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

    def test_switch_checks_the_previous_worker_and_keeps_the_previous_workspace_on_refusal(self):
        next_root = self.root / 'Webtoon'
        with patch('comic_archive.worker_host.status',
                   side_effect=lambda session: {'running': session == self.session}):
            answer = self.call('prepare_workspace', root=str(next_root),
                               base_root=str(self.root), site_key='webtoon')
        self.assertFalse(answer['ok'])
        self.assertEqual(Path.cwd(), self.root)
        self.assertFalse(next_root.exists())

    def test_stale_network_settings_cannot_retarget_the_current_sites_policy(self):
        (self.root / 'net.json').write_text('{"connection":"wifi","metered":false}')
        next_root = self.root / 'Webtoon'
        self.assertTrue(self.call('prepare_workspace', root=str(next_root),
            base_root=str(self.root), site_key='webtoon')['ok'])
        (next_root / 'net.json').write_text('{"connection":"cellular","metered":true}')
        self.assertTrue(self.call('settings', root=str(next_root))['ok'])
        self.assertFalse(self.call('settings', root=str(self.root))['ok'])
        self.assertEqual(network_android.probe(), {'connection': 'cellular', 'metered': True})

    def test_wrong_folder_site_and_invalid_attempt_shapes_are_refused_before_network(self):
        wrong = self.call('prepare_workspace', root=str(self.root / 'Tapas'),
                          base_root=str(self.root), site_key='webtoon')
        self.assertFalse(wrong['ok'])
        for token in (True, 0, -1, '1', 2**63):
            with self.subTest(token=token):
                self.assertFalse(self.attempt(token=token)['ok'])
        self.assertFalse(self.attempt(timeout_ms=900_001)['ok'])
        self.assertFalse(android_session.is_signed_in(self.session))
