import json
import os
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

    def test_how_the_sign_in_stands_can_be_asked_while_the_connection_is_being_checked_and_saved(self):
        inside, release, answers = threading.Event(), threading.Event(), []

        def configure(*args, **options):
            inside.set()
            release.wait(30)
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=self.section), \
                patch('comic_archive.cloud.configure_target', side_effect=configure):
            self.signin.start('b2', self.credentials)
            self.assertTrue(inside.wait(30))
            asking = threading.Thread(target=lambda: answers.append(self.signin.status()), daemon=True)
            asking.start(); asking.join(10)
            release.set()
            self.assertEqual([answer['status'] for answer in answers], ['running'])
            self.assertEqual(self.finish()['status'], 'connected')

    def test_a_new_sign_in_clears_the_key_an_earlier_one_left_when_it_was_ended_before_it_finished(self):
        import time
        from comic_archive import cloud_auth
        folder = self.root/'.archive-state/cloud'; folder.mkdir(parents=True)
        stale, recent, outside = folder/'signin-stale', folder/'signin-recent', self.root.parent/'outside'
        for work in (stale, recent, outside):
            work.mkdir(); (work/'rclone.conf').write_text('[b2]\nkey = PRIVATE_KEY\n')
        if os.name != 'nt':  # Windows refuses a private folder with a link in it before any sign-in starts.
            (folder/'signin-link').symlink_to(outside, target_is_directory=True)
        old = time.time() - cloud_auth.STALE_SECONDS - 60
        os.utime(stale, (old, old)); os.utime(outside, (old, old))
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                patch('comic_archive.cloud.Transfer'):
            self.signin.start('b2', self.credentials)
            self.assertEqual(self.finish()['status'], 'connected')
        self.assertFalse(stale.exists())
        # One that may still be going on, and anything a link leads to, is left.
        self.assertTrue((recent/'rclone.conf').is_file()); self.assertTrue((outside/'rclone.conf').is_file())

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

    def sign_in(self):
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                patch('comic_archive.cloud.Transfer'):
            self.signin.start('b2', self.credentials)
            self.assertEqual(self.finish()['status'], 'connected')
        return Path(cloud.settings(self.root)['targets']['b2']['config'])

    def pending_test(self, target):
        """A cloud test that still has to delete its files with the connection it was run with."""
        test_id = str(uuid.uuid4())
        cloud._write(self.root/'.archive-state/cloud/tests'/(test_id + '.json'), {
            'schema_version': 1, 'provider': 'b2', 'state': 'cleanup_pending', 'files': [], 'verified_episodes': 0,
            'prefix': f"Comic-Archive/{cloud.settings(self.root)['library_id']}/tests/{test_id}",
            'connection': {k: target[k] for k in ('remote', 'config', 'target_id')}})
        return test_id

    def test_a_replaced_key_file_a_test_cleanup_needed_is_deleted_once_the_test_is_cleaned_up(self):
        first = self.sign_in()
        waiting = self.pending_test(cloud.settings(self.root)['targets']['b2'])
        second = self.sign_in()
        self.assertTrue(first.is_file())  # The test still has to clean up with it.
        current = self.pending_test(cloud.settings(self.root)['targets']['b2'])
        with patch('comic_archive.cloud.Transfer'):
            self.assertEqual(cloud.cleanup_test(self.root, waiting), {'test_files_deleted': True})
            self.assertFalse(first.exists())
            self.assertEqual(cloud.cleanup_test(self.root, current), {'test_files_deleted': True})
        self.assertTrue(second.is_file())  # The connection in use keeps its file.

    def test_a_new_sign_in_deletes_the_key_files_nothing_names_any_more_and_keeps_every_one_still_named(self):
        import time
        from comic_archive import cloud_auth
        first = self.sign_in(); folder = first.parent
        unused, recent, waiting, earlier = (folder/('connection-' + uuid.uuid4().hex + '.conf') for _ in range(4))
        for path in (unused, recent, waiting, earlier):
            path.write_text('[b2]\nkey = PRIVATE_KEY\n'); path.chmod(0o600)
        target = {'remote': 'b2', 'enabled': False, 'tested': False, 'target_id': str(uuid.uuid4())}
        self.pending_test(target | {'config': str(waiting)})
        record = folder/'targets.json'; saved = json.loads(record.read_text())
        # A provider an earlier version offered: its connection stays in the record, and so does its file.
        saved['targets']['google'] = target | {'remote': 'google', 'config': str(earlier)}
        cloud._write(record, saved)
        old = time.time() - cloud_auth.STALE_SECONDS - 60
        for path in (unused, waiting, earlier):
            os.utime(path, (old, old))
        second = self.sign_in()
        self.assertFalse(unused.exists())  # Left by a sign-in that was ended at the wrong moment.
        # One that may be a sign-in still going on, one a test still needs, and one the record names all stay.
        self.assertEqual([path.is_file() for path in (recent, waiting, earlier)], [True, True, True])
        self.assertFalse(first.exists()); self.assertTrue(second.is_file())
        self.assertEqual(Path(json.loads(record.read_text())['targets']['google']['config']), earlier)

    def test_a_new_sign_in_with_shared_storage_deletes_the_shared_file_it_replaces_unless_another_site_needs_it(self):
        store = self.root.parent/'.comic-shared'
        shared.link(self.root, store)
        other = self.root.parent/'Webtoon/Library'; other.mkdir(parents=True)
        shared.link(other, store)

        def sign_in():
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                    patch('comic_archive.cloud.Transfer'):
                self.signin.start('b2', self.credentials)
                self.assertEqual(self.finish()['status'], 'connected')
            return Path(cloud.settings(self.root)['targets']['b2']['config'])
        first = sign_in()
        self.assertEqual(first.parent, store/'cloud')
        second = sign_in()
        self.assertFalse(first.exists()); self.assertTrue(second.is_file())
        self.assertEqual(cloud.settings(other)['targets']['b2']['config'], str(second))
        target = cloud.settings(other)['targets']['b2']
        receipt = other/'.archive-state/cloud/tests'/(str(uuid.uuid4()) + '.json')
        cloud._write(receipt, {'provider': 'b2', 'state': 'cleanup_pending',
                               'connection': {k: target[k] for k in ('remote', 'config', 'target_id')}})
        third = sign_in()
        self.assertTrue(second.is_file())  # The other site's test still has to clean up with it.
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

    def test_a_sign_in_that_fails_after_its_connection_was_saved_keeps_the_key_file_the_settings_name(self):
        def sign_in(**failing):
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                    patch('comic_archive.cloud.Transfer'), patch('comic_archive.cloud.status', **failing):
                self.signin.start('b2', self.credentials)
                status = self.finish()
            return status, Path(cloud.settings(self.root)['targets']['b2']['config'])
        _, first = sign_in()
        # The settings are saved, then reading how things stand fails: the connection is the new one all the same.
        status, second = sign_in(side_effect=ArchiveError('PRIVATE_DETAIL'))
        self.assertNotEqual(first, second)
        self.assertIn('PRIVATE_KEY', second.read_text())
        self.assertEqual(status['status'], 'connected')
        self.assertFalse(first.exists())  # The file it replaced is deleted as after any other sign-in.
        self.assertEqual(list((self.root/'.archive-state/cloud').glob('connection-*')), [second])

    def test_a_sign_in_whose_saved_key_could_not_be_noted_on_this_computer_says_so_and_keeps_the_key(self):
        def sign_in(trust, trusted=True):
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', return_value=dict(self.section)), \
                    patch('comic_archive.cloud.Transfer'), patch('comic_archive.cloud_runtime.trust', side_effect=trust) as noted, \
                    patch('comic_archive.cloud_runtime.trusted', return_value=trusted):
                self.signin.start('b2', self.credentials)
                status = self.finish()
            return status, Path(cloud.settings(self.root)['targets']['b2']['config']), noted.call_count
        # The key is saved, then noting the connection as set up here fails, and fails again: never "connected".
        status, first, tries = sign_in(ArchiveError('PRIVATE_DETAIL'))
        self.assertEqual((status['status'], status['code'], tries), ('error', 'signin_unfinished', 2))
        self.assertNotIn('PRIVATE', json.dumps(status))
        self.assertIn('PRIVATE_KEY', first.read_text())  # The file the settings name stays.
        # Noted at the second try, the sign-in is whole.
        status, second, tries = sign_in([ArchiveError('PRIVATE_DETAIL'), None])
        self.assertEqual((status['status'], tries), ('connected', 2))
        self.assertFalse(first.exists())
        # A second try that went through without the connection being known here is not whole either.
        status, third, _ = sign_in([ArchiveError('PRIVATE_DETAIL'), None], trusted=False)
        self.assertEqual((status['status'], status['code']), ('error', 'signin_unfinished'))
        self.assertEqual(list((self.root/'.archive-state/cloud').glob('connection-*')), [third])

    def test_auth_payloads_are_fixed_and_bounded(self):
        # Google Drive, Dropbox and Proton Drive were offered by earlier versions; only Backblaze B2 signs in now.
        for provider, credentials in [('google', {}), ('dropbox', {}), ('proton', {'username':'u', 'password':'p'}),
                                      ([], {}), ('ftp', {}), ('b2', []), ('b2', {}),
                                      ('b2', self.credentials | {'password':'PRIVATE'})]:
            with self.assertRaises(ArchiveError): validate(provider, credentials)
        validate('b2', self.credentials)
