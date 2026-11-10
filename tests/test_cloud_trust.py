import json
import os
from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import home_environment
from comic_archive import app_runtime, cloud, cloud_runtime, cloud_trust
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter
from test_cloud import FakeTransfer


class CloudTrustTests(unittest.TestCase):
    """A library folder can bring a connection to someone else's bucket; this computer uses only its own."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / 'state'
        FakeTransfer.files = {}; FakeTransfer.fail_provider = None; FakeTransfer.fail_delete = FakeTransfer.fail_upload = False
        for wrapper in [patch.dict(os.environ, home_environment(self.home, self.state)),
                        patch.object(cloud, 'Transfer', FakeTransfer),
                        # As the desktop app and its downloader run; each test says when it is another computer.
                        patch.object(cloud_runtime, '_checked', True), patch.object(cloud_runtime, '_android', False)]:
            wrapper.start(); self.addCleanup(wrapper.stop)
        self.series = Series('42', 'Synthetic', source='tapas')
        self.record = self.state / 'comic-archiver' / cloud_trust.RECORD

    def library(self, top, key='THEIR_PRIVATE_KEY'):
        """A library folder whose B2 connection was set up, tested and turned on on another computer."""
        root = top / 'Library'
        config = root / '.archive-state/cloud/connection-1.conf'
        config.parent.mkdir(parents=True)
        config.write_text(f'[b2]\ntype=b2\naccount=KEY_ID\nkey={key}\narchive_bucket=their-bucket\n'); config.chmod(0o600)
        self.publish(root, '81')
        with patch.object(cloud_runtime, '_checked', False):
            cloud.configure_target(root, 'b2', config=config)
            cloud.smoke_test(root, 'b2', episodes=1)
            cloud.enable(root, 'b2', True)
        return root

    def publish(self, root, key):
        episode = Episode(key, '42', 'Chapter ' + key, int(key), (Page(1, 'https://cdn.test/a', False),), 'tapas_free')
        with closing(PublicationStore(root)) as store, patch.object(cloud, 'direct_target', return_value=None):
            folder = store.archive(Manifest(self.series, (episode,)), MemoryAdapter(), progress=lambda _: None)
            return store.episode_path(self.series, episode, folder)

    def b2(self, root):
        return cloud.status(root)['targets'][0]

    def test_a_connection_a_library_folder_brought_takes_no_episode_until_the_person_says_so(self):
        root = self.library(self.home / 'Downloads/Comic Archiver')
        self.assertEqual({key: self.b2(root)[key] for key in ('enabled', 'tested', 'confirmed', 'bucket')},
                         {'enabled': True, 'tested': True, 'confirmed': False, 'bucket': 'their-bucket'})
        FakeTransfer.files = {}
        # Saved here: none goes straight to B2, none is queued, and nothing waiting is sent.
        self.assertIsNone(cloud.direct_target(root))
        archive = self.publish(root, '82')
        self.assertTrue(archive.is_file())
        cloud.queue_episode(root, archive)
        self.assertEqual(list((root / '.archive-state/cloud/outbox/b2').glob('*.json')), [])
        with patch.object(cloud_runtime, '_checked', False):
            cloud.queue_episode(root, archive)  # As the other computer left it.
        self.assertEqual(cloud.upload_pending(root)['uploaded_episodes'], 0)
        self.assertEqual(FakeTransfer.files, {})
        # Yes: from now on, as if it had been set up here.
        self.assertTrue(cloud.confirm(root, 'b2', True)['targets'][0]['confirmed'])
        self.assertEqual(cloud.direct_target(root)['remote'], 'b2')
        self.assertEqual(cloud.upload_pending(root)['uploaded_episodes'], 1)
        self.assertNotIn('THEIR_PRIVATE_KEY', self.record.read_text())
        # No: new episodes are saved here, and the person is not asked again.
        other = self.library(self.home / 'Shared/Comic Archiver', 'OTHER_PRIVATE_KEY')
        self.assertFalse(self.b2(other)['confirmed'])
        target = cloud.confirm(other, 'b2', False)['targets'][0]
        self.assertEqual((target['enabled'], target['confirmed']), (False, True))
        self.assertIsNone(cloud.direct_target(other))
        # Turned on again by the person: their own choice.
        self.assertTrue(cloud.enable(other, 'b2', True)['targets'][0]['enabled'])
        self.assertEqual(cloud.direct_target(other)['remote'], 'b2')
        third = self.library(self.home / 'Third/Comic Archiver', 'THIRD_PRIVATE_KEY')
        with patch.object(cloud_runtime, '_checked', False):
            cloud.enable(third, 'b2', False)
        self.assertTrue(cloud.enable(third, 'b2', True)['targets'][0]['confirmed'])

    def test_a_connection_set_up_or_tested_here_is_used_and_a_copy_of_its_folder_keeps_it(self):
        root = self.home / 'Comic Archiver/Library'
        config = self.home / 'rclone.conf'; config.write_text('[b2]\ntype=b2\nkey=OWN_PRIVATE_KEY\n'); config.chmod(0o600)
        copied = self.home / 'copied.conf'
        cloud_runtime.note_connections()  # Opened before, with none.
        self.publish(root, '81')
        cloud.configure_target(root, 'b2', config=config)
        self.assertTrue(self.b2(root)['confirmed'])
        cloud.smoke_test(root, 'b2', episodes=1); cloud.enable(root, 'b2', True)
        self.assertEqual(cloud.direct_target(root)['remote'], 'b2')
        # The same connection wherever its file is; a changed file, or another name in it, is another connection.
        target = cloud.settings(root)['targets']['b2']
        digest = cloud_trust.fingerprint(target)
        shutil.copyfile(config, copied); copied.chmod(0o600)
        self.assertEqual(cloud_trust.fingerprint(target | {'config': str(copied)}), digest)
        self.assertTrue(cloud_trust.trusted(target | {'config': str(copied)}))
        self.assertNotEqual(cloud_trust.fingerprint(target | {'remote': 'other'}), digest)
        config.write_text('[b2]\ntype=b2\nkey=REPLACED_PRIVATE_KEY\n')
        self.assertFalse(self.b2(root)['confirmed'])
        self.assertIsNone(cloud.direct_target(root))
        for unreadable in (target | {'config': str(self.home / 'missing.conf')}, {}):
            self.assertIsNone(cloud_trust.fingerprint(unreadable))
            self.assertFalse(cloud_trust.trusted(unreadable))
        # A connection file gone, as when a library folder moved: nothing goes to it, and nobody is asked about it.
        config.rename(self.home / 'away.conf')
        self.assertEqual((self.b2(root)['enabled'], self.b2(root)['confirmed']), (True, True))
        self.assertIsNone(cloud.direct_target(root))
        (self.home / 'away.conf').rename(config)
        # A tested connection is the person's own as well.
        cloud.smoke_test(root, 'b2', episodes=1)
        self.assertTrue(self.b2(root)['confirmed'])
        self.assertNotIn('PRIVATE', self.record.read_text())

    def test_connections_in_use_before_this_version_are_kept_and_none_a_folder_brings_later(self):
        own, chosen = self.home / 'Comic Archiver', self.home / 'drive/Comic Archiver'
        roots = [self.library(own), self.library(chosen, 'CHOSEN_PRIVATE_KEY'),
                 self.library(chosen / 'Webtoon', 'WEBTOON_PRIVATE_KEY')]
        app_runtime.remember_root(chosen)
        app_runtime.remember_library(own)
        before = self.library(self.home / 'unopened/Comic Archiver', 'UNOPENED_PRIVATE_KEY')
        self.assertFalse(self.record.exists())
        cloud_runtime.note_connections()  # The first start of this version.
        self.assertEqual(len(json.loads(self.record.read_text())['connections']), 3)
        for root in roots:
            self.assertTrue(self.b2(root)['confirmed'])
        self.assertFalse(self.b2(before)['confirmed'])
        # Noted from now on: a folder first opened later is asked about.
        later = self.library(self.home / 'later/Comic Archiver', 'LATER_PRIVATE_KEY')
        app_runtime.remember_library(later.parent)
        cloud_runtime.note_connections()
        self.assertFalse(self.b2(later)['confirmed'])
        # A record that cannot be read knows nothing: the person is asked again, never the reverse.
        self.record.write_text('not json')
        self.assertFalse(self.b2(roots[0])['confirmed'])
        self.record.write_text(json.dumps({'connections': ['A' * 64, 7]}))
        self.assertFalse(self.b2(roots[0])['confirmed'])

    def test_a_library_put_in_the_standard_folder_before_the_app_first_ran_is_asked_about(self):
        brought = self.library(self.home / 'Comic Archiver')
        self.assertEqual(app_runtime.default_root(), self.home / 'Comic Archiver')
        self.assertFalse(self.record.exists())
        cloud_runtime.note_connections()  # The first start: nothing was opened here before.
        self.assertEqual(json.loads(self.record.read_text())['connections'], [])
        self.assertFalse(self.b2(brought)['confirmed'])
        app_runtime.remember_library(self.home / 'Comic Archiver')
        cloud_runtime.note_connections()
        self.assertFalse(self.b2(brought)['confirmed'])

    def test_android_and_the_command_line_tools_use_every_connection_as_before(self):
        root = self.library(self.home / 'Downloads/Comic Archiver')
        with patch.object(cloud_runtime, '_android', True):
            self.assertTrue(self.b2(root)['confirmed'])
            cloud_runtime.note_connections()
            cloud.confirm(root, 'b2', True)
        self.assertFalse(self.record.exists())
        with patch.object(cloud_runtime, '_checked', False):
            self.assertEqual(cloud.direct_target(root)['remote'], 'b2')
        self.assertIsNone(cloud.direct_target(root))
        for answer in ('yes', None):
            with self.assertRaises(cloud.ArchiveError): cloud.confirm(root, 'b2', answer)
        with self.assertRaises(cloud.ArchiveError): cloud.confirm(root, 'dropbox', True)

    def test_the_desktop_app_and_its_downloader_turn_the_check_on(self):
        from comic_archive import cli, desktop_app
        with patch.object(cloud_runtime, '_checked', False):
            with patch('comic_archive.cli.parser'), patch('comic_archive.cli.run_beside_engine', return_value=0), \
                    self.assertRaises(SystemExit) as ended:
                cli.main()
            self.assertEqual((ended.exception.code, cloud_runtime._checked), (0, True))
        with patch.object(cloud_runtime, '_checked', False), patch('comic_archive.desktop_app.qt', side_effect=ImportError), \
                patch('comic_archive.desktop_app.toolkit_missing', Mock(return_value=3)):
            self.assertEqual(desktop_app.main([]), 3)
            self.assertTrue(cloud_runtime._checked)


if __name__ == '__main__':
    unittest.main()
