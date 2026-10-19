import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import cloud, shared
from comic_archive.cloud_auth import SignIn, validate
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold
from comic_archive.cloud_errors import CloudTransferError


class CloudSignInTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name)/'Library'; self.root.mkdir()
        self.signin = SignIn(self.root); self.addCleanup(self.signin.close)
        self.credentials = {'key_id': 'k'*25, 'application_key': 'a'*31}
        self.section = {'type': 'b2', 'account': 'PRIVATE_KEY_ID', 'key': 'PRIVATE_KEY', 'archive_bucket': 'synthetic-bucket'}

    def finish(self):
        self.signin._thread.join(timeout=30)
        self.assertFalse(self.signin._thread.is_alive())
        return self.signin.status()

    def test_a_backblaze_sign_in_for_a_site_that_shares_its_storage_keeps_the_connection_with_the_shared_settings(self):
        store = self.root.parent/'.comic-shared'
        shared.link(self.root, store)
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=self.section), \
                patch('comic_archive.cloud.configure_target') as configure:
            self.signin.start('b2', self.credentials)
            self.assertEqual(self.finish()['status'], 'connected')
        config = configure.call_args.kwargs['config']
        self.assertEqual((config.parent, config.stat().st_mode&0o777), (store/'cloud', PRIVATE_FILE))
        self.assertIn('PRIVATE_KEY', config.read_text())
        self.assertFalse(list((self.root/'.archive-state/cloud').glob('connection-*')))
        self.assertFalse(list(config.parent.glob('signin-*')))

    def test_a_new_sign_in_deletes_the_connection_file_it_replaces_unless_a_test_cleanup_needs_it(self):
        def sign_in():
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                    patch('comic_archive.cloud.Transfer'):
                self.signin.start('b2', self.credentials)
                self.assertEqual(self.finish()['status'], 'connected')
            return Path(cloud.settings(self.root)['targets']['b2']['config'])
        named = self.root.parent/'named.conf'; named.write_text('[b2]\ntype = b2\n'); named.chmod(0o600)
        with patch('comic_archive.cloud.Transfer'):
            cloud.configure_target(self.root, 'b2', config=named)
        first = sign_in()
        self.assertTrue(named.is_file())  # A file named on the command line stays.
        second = sign_in()
        self.assertFalse(first.exists()); self.assertTrue(second.is_file())
        target = cloud.settings(self.root)['targets']['b2']
        receipt = self.root/'.archive-state/cloud/tests'/(str(uuid.uuid4()) + '.json')
        cloud._write(receipt, {'provider': 'b2', 'state': 'cleanup_pending',
                               'connection': {k: target[k] for k in ('remote', 'config', 'target_id')}})
        third = sign_in()
        self.assertTrue(second.is_file()); self.assertTrue(third.is_file())
        cloud._write(receipt, json.loads(receipt.read_text()) | {'state': 'cleaned'})
        sign_in()
        self.assertFalse(third.exists())

    def test_duplicate_signin_is_rejected_and_shutdown_cancels_owned_work(self):
        entered = threading.Event()
        def connection(root, credentials, cancel):
            entered.set(); cancel.wait(3)
            raise ArchiveError('Cancelled')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', side_effect=connection):
            self.signin.start('b2', self.credentials); self.assertTrue(entered.wait(2))
            with self.assertRaises(ArchiveError): self.signin.start('b2', self.credentials)
            self.signin.close()
            self.assertEqual(self.finish()['status'], 'cancelled')

    def test_cancel_during_the_last_step_discards_the_new_connection(self):
        def connection(root, credentials, cancel):
            cancel.set()
            return dict(self.section)
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', side_effect=connection), \
                patch('comic_archive.cloud.configure_target') as configure:
            self.signin.start('b2', self.credentials)
            status = self.finish()
        self.assertEqual((status['status'], status['code']), ('cancelled', 'signin_cancelled'))
        configure.assert_not_called()
        folder = self.root/'.archive-state/cloud'
        self.assertFalse(list(folder.glob('connection-*'))); self.assertFalse(list(folder.glob('signin-*')))

    def test_rejected_connection_leaves_no_config_behind_and_reports_a_fixed_code(self):
        folder = self.root/'.archive-state/cloud'
        for error, code in ((ArchiveError('PRIVATE_DETAIL'), 'signin_failed'), (NetworkPolicyHold('PRIVATE_DETAIL'), 'signin_network_held'),
                            (CloudTransferError('cloud_rate_limited'), 'cloud_rate_limited')):
            with self.subTest(code=code), patch('comic_archive.cloud_tools.ensure'), \
                    patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                    patch('comic_archive.cloud.configure_target', side_effect=error):
                self.signin.start('b2', self.credentials)
                status = self.finish()
                self.assertEqual((status['status'], status['code']), ('error', code))
                self.assertNotIn('PRIVATE', json.dumps(status))
                self.assertFalse(list(folder.glob('connection-*'))); self.assertFalse(list(folder.glob('signin-*')))

    def test_auth_payloads_are_fixed_and_bounded(self):
        # Google Drive, Dropbox and Proton Drive were offered by earlier versions; only Backblaze B2 signs in now.
        for provider, credentials in [('google', {}), ('dropbox', {}), ('proton', {'username':'u', 'password':'p'}),
                                      ([], {}), ('ftp', {}), ('b2', []), ('b2', {}),
                                      ('b2', self.credentials | {'password':'PRIVATE'})]:
            with self.assertRaises(ArchiveError): validate(provider, credentials)
        validate('b2', self.credentials)
