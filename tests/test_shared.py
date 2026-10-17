import json
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import cloud, network, shared
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter
from test_cloud import FakeTransfer


class SharedSettingsTests(unittest.TestCase):
    """Two sites' libraries in one library folder, as the app lays them out: Tapas's, and another site's inside it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.top = Path(temporary.name)
        self.tapas, self.other = self.top / 'Library', self.top / 'Other/Library'
        self.store = self.top / '.comic-shared'
        FakeTransfer.files = {}; FakeTransfer.fail_provider = None; FakeTransfer.fail_delete = False; FakeTransfer.fail_upload = False
        transfer = patch.object(cloud, 'Transfer', FakeTransfer); transfer.start(); self.addCleanup(transfer.stop)

    def config(self, folder, name='connection-first.conf'):
        """A private connection file, as the app's own B2 sign-in leaves one."""
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name; path.write_text('[b2]\ntype = b2\narchive_bucket = synthetic-bucket\n'); path.chmod(0o600)
        return path

    @staticmethod
    def own(library):
        return library / '.archive-state/cloud'

    def publish(self, library, key='81'):
        series = Series('42', 'Synthetic', source='tapas')
        episode = Episode(key, '42', 'Chapter ' + key, int(key), (Page(1, 'https://cdn.test/a', False),), 'tapas_free')
        with closing(PublicationStore(library)) as store:
            store.archive(Manifest(series, (episode,)), MemoryAdapter(), progress=lambda _: None)

    def ready(self, library, config):
        cloud.configure_target(library, 'b2', config=config)
        cloud.smoke_test(library, 'b2', episodes=1)
        cloud.enable(library, 'b2', True)

    def b2(self, library):
        return next(t for t in cloud.status(library)['targets'] if t['provider'] == 'b2')

    def test_backblaze_set_up_on_one_site_serves_every_site_each_in_a_folder_of_its_own(self):
        for library in (self.tapas, self.other):
            shared.link(library, self.store)
        self.publish(self.tapas)
        config = self.config(self.store / 'cloud')
        self.ready(self.tapas, config)
        self.publish(self.tapas, '82')
        # The other site has no episodes to test with, and needs none: the shared setup is checked already.
        self.assertEqual({k: self.b2(self.other)[k] for k in ('connected', 'tested', 'enabled')},
                         {'connected': True, 'tested': True, 'enabled': True})
        self.assertEqual(cloud.status(self.other)['storage'], 'shared')
        self.publish(self.other, '91')
        ids = [cloud.settings(library)['library_id'] for library in (self.tapas, self.other)]
        self.assertNotEqual(ids[0], ids[1])
        folders = {key.split('/')[1] for _, key in FakeTransfer.files if '/tests/' not in key}
        self.assertEqual(folders, set(ids))
        # One record of the connection, kept with the shared settings; each library keeps only its name in the cloud.
        for library in (self.tapas, self.other):
            own = json.loads((library / '.archive-state/cloud/targets.json').read_text())
            self.assertEqual(own['targets'], {})
        kept = json.loads((self.store / 'cloud/targets.json').read_text())
        self.assertEqual((set(kept['targets']), kept['targets']['b2']['config']), ({'b2'}, str(config)))
        self.assertEqual(stat.S_IMODE((self.store / 'cloud/targets.json').stat().st_mode), PRIVATE_FILE)

    def test_a_new_sign_in_keeps_its_connection_file_with_the_shared_settings_unless_the_library_keeps_its_own(self):
        shared.link(self.tapas, self.store)
        self.assertEqual(cloud.connection_folder(self.tapas), self.store / 'cloud')
        shared.choose_cloud(self.tapas, 'separate')
        self.assertEqual(cloud.connection_folder(self.tapas), self.tapas / '.archive-state/cloud')
        self.assertEqual(cloud.connection_folder(self.other), self.other / '.archive-state/cloud')

    def test_a_site_set_up_before_sharing_gives_its_setup_to_every_site_and_its_uploads_stay_done(self):
        self.publish(self.other)
        first = self.config(self.own(self.other))
        self.ready(self.other, first)
        self.publish(self.other, '82')
        uploaded = dict(FakeTransfer.files)
        self.assertTrue(any('/tests/' not in key for _, key in uploaded))
        target_id = cloud.settings(self.other)['targets']['b2']['target_id']
        shared.link(self.other, self.store)
        shared.link(self.tapas, self.store)
        moved = Path(cloud.settings(self.other)['targets']['b2']['config'])
        self.assertEqual((moved.parent, moved.read_text(), first.exists()), (self.store / 'cloud', '[b2]\ntype = b2\narchive_bucket = synthetic-bucket\n', False))
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), PRIVATE_FILE)
        self.assertEqual(json.loads((self.own(self.other) / 'targets.json').read_text())['targets'], {})
        for library in (self.tapas, self.other):
            self.assertEqual((cloud.status(library)['storage'], cloud.settings(library)['targets']['b2']['target_id']), ('shared', target_id))
            self.assertTrue(self.b2(library)['enabled'])
        # What was uploaded before is still known to be uploaded: nothing goes again.
        self.assertEqual(cloud.upload_pending(self.other)['uploaded_episodes'], 0)
        self.assertEqual(FakeTransfer.files, uploaded)

    def test_a_site_opened_before_but_never_set_up_uses_the_shared_setup_and_keeps_its_folder_name(self):
        shared.link(self.tapas, self.store)
        self.publish(self.tapas)
        self.ready(self.tapas, self.config(self.store / 'cloud'))
        name = str(uuid.uuid4())
        self.own(self.other).mkdir(parents=True)
        (self.own(self.other) / 'targets.json').write_text(json.dumps({'schema_version': 1, 'library_id': name, 'targets': {}}))
        shared.link(self.other, self.store)
        self.assertEqual((shared.cloud_mode(self.other), cloud.settings(self.other)['library_id']), ('shared', name))
        self.assertTrue(self.b2(self.other)['enabled'])

    def test_a_site_with_its_own_setup_keeps_it_when_the_shared_one_is_another(self):
        self.publish(self.tapas); self.publish(self.other)
        self.ready(self.tapas, self.config(self.own(self.tapas)))
        mine = self.config(self.own(self.other), 'connection-mine.conf')
        self.ready(self.other, mine)
        shared.link(self.tapas, self.store)
        shared.link(self.other, self.store)
        self.assertEqual((cloud.status(self.tapas)['storage'], cloud.status(self.other)['storage']), ('shared', 'separate'))
        self.assertEqual(cloud.settings(self.other)['targets']['b2']['config'], str(mine))
        self.assertNotEqual(cloud.settings(self.other)['targets']['b2']['target_id'],
                            cloud.settings(self.tapas)['targets']['b2']['target_id'])

    def test_a_link_cut_short_finishes_with_the_connections_it_had_already_moved(self):
        self.publish(self.other)
        first = self.config(self.own(self.other))
        self.ready(self.other, first)
        real = cloud._write
        def stop_before_the_library(path, value):
            if path == self.own(self.other) / 'targets.json':
                raise OSError('stopped')
            real(path, value)
        with patch('comic_archive.cloud._write', side_effect=stop_before_the_library):
            with self.assertRaises(OSError):
                shared.link(self.other, self.store)
        # The connection reached the shared settings, but the library still had it, and was not marked as linked.
        self.assertEqual((shared.cloud_mode(self.other), first.exists()), (None, True))
        shared.link(self.other, self.store)
        self.assertEqual(cloud.status(self.other)['storage'], 'shared')
        self.assertEqual(len(list((self.store / 'cloud').glob('*.conf'))), 1)
        self.assertFalse(first.exists())
        self.assertTrue(self.b2(self.other)['tested'])

    def test_a_test_still_to_be_cleaned_up_keeps_the_connection_file_it_names(self):
        self.publish(self.other)
        first = self.config(self.own(self.other))
        self.ready(self.other, first)
        FakeTransfer.fail_delete = True
        with self.assertRaises(ArchiveError):
            cloud.smoke_test(self.other, 'b2', episodes=1)
        FakeTransfer.fail_delete = False
        (self.other / '.archive-state/cloud/tests/older.json').write_text(json.dumps({'state': 'cleanup_pending'}))
        shared.link(self.other, self.store)
        self.assertTrue(first.exists())
        [waiting] = cloud.status(self.other)['pending_test_cleanup']
        self.assertEqual(cloud.cleanup_test(self.other, waiting['test_id']), {'test_files_deleted': True})

    def test_a_connection_file_named_on_the_command_line_stays_where_it_is(self):
        self.publish(self.other)
        named = self.top / 'my-rclone.conf'; named.write_text('[b2]\ntype = b2\narchive_bucket = synthetic-bucket\n'); named.chmod(0o600)
        self.ready(self.other, named)
        shared.link(self.other, self.store)
        self.assertEqual((cloud.settings(self.other)['targets']['b2']['config'], named.exists()),
                         (str(named), True))

    def test_a_site_can_keep_its_own_backblaze_setup_and_go_back_to_the_shared_one_with_nothing_lost(self):
        for library in (self.tapas, self.other):
            shared.link(library, self.store)
        self.publish(self.tapas); self.publish(self.other)
        self.ready(self.tapas, self.config(self.store / 'cloud'))
        shared.choose_cloud(self.other, 'separate')
        self.assertEqual((cloud.status(self.other)['storage'], self.b2(self.other)['connected']), ('separate', False))
        self.ready(self.other, self.config(self.own(self.other), 'connection-mine.conf'))
        self.assertTrue(self.b2(self.tapas)['tested'])
        self.assertNotEqual(cloud.settings(self.other)['targets'], cloud.settings(self.tapas)['targets'])
        shared.choose_cloud(self.other, 'shared')
        self.assertEqual(cloud.settings(self.other)['targets'], cloud.settings(self.tapas)['targets'])
        shared.choose_cloud(self.other, 'separate')
        self.assertTrue(self.b2(self.other)['tested'])
        with self.assertRaises(ArchiveError):
            shared.choose_cloud(self.other, 'both')
        with self.assertRaises(ArchiveError):
            shared.choose_cloud(self.top / 'Unlinked/Library', 'shared')

    def test_a_linked_library_that_lost_its_cloud_record_gets_a_new_name_when_set_up_again(self):
        shared.link(self.tapas, self.store)
        (self.tapas / '.archive-state/cloud/targets.json').unlink()
        self.assertIsNone(cloud.settings(self.tapas))
        cloud.configure_target(self.tapas, 'b2', config=self.config(self.store / 'cloud'))
        own = json.loads((self.tapas / '.archive-state/cloud/targets.json').read_text())
        self.assertEqual(own['targets'], {})
        self.assertEqual(set(cloud.settings(self.tapas)['targets']), {'b2'})

    def test_a_library_that_is_not_linked_keeps_its_own_settings(self):
        self.assertIsNone(cloud.status(self.tapas)['storage'])
        self.assertIsNone(shared.folder(self.tapas))
        self.assertEqual(network.policy(self.tapas), {'schema_version': 1, 'wifi_only': True})
        network.configure(self.tapas, False)
        self.assertTrue((self.tapas / '.archive-state/network-policy.json').is_file())

    def test_the_connection_rule_is_one_for_every_site_and_a_site_brings_its_own_choice_when_first_linked(self):
        network.configure(self.other, False)
        network.configure(self.tapas, True)
        shared.link(self.other, self.store)
        shared.link(self.tapas, self.store)
        # The first site linked brought its choice; the second found one already there.
        self.assertFalse(network.policy(self.tapas)['wifi_only'])
        network.configure(self.tapas, True)
        self.assertTrue(network.policy(self.other)['wifi_only'])
        self.assertEqual(stat.S_IMODE((self.store / 'network-policy.json').stat().st_mode), PRIVATE_FILE)
        with patch('comic_archive.network.probe', return_value={'connection': 'cellular', 'metered': True}):
            with self.assertRaises(network.NetworkPolicyHold):
                network.NetworkGuard(self.other).check()
            network.configure(self.tapas, False)
            network.NetworkGuard(self.other).check()

    def test_a_library_folder_moved_by_hand_uses_the_shared_settings_beside_it_now(self):
        shared.link(self.tapas, self.store)
        shared.choose_cloud(self.tapas, 'separate')
        elsewhere = self.top / 'Moved/.comic-shared'
        shared.link(self.tapas, elsewhere)
        self.assertEqual((shared.folder(self.tapas), shared.cloud_mode(self.tapas)), (elsewhere, 'separate'))
        shared.link(self.tapas, elsewhere)
        self.assertEqual(shared.folder(self.tapas), elsewhere)

    def test_unreadable_or_symlinked_shared_settings_are_refused_and_nothing_is_guessed(self):
        shared.link(self.tapas, self.store)
        link = self.tapas / shared.LINK
        value = json.loads(link.read_text())
        self.assertEqual(stat.S_IMODE(link.stat().st_mode), PRIVATE_FILE)
        for broken in ('not json', json.dumps([]), json.dumps(value | {'cloud': 'both'}), json.dumps(value | {'folder': 'relative'}),
                       json.dumps(value | {'schema_version': 2}), json.dumps(value | {'schema_version': True}),
                       json.dumps(value | {'folder': 3}), json.dumps(value | {'extra': 1}), json.dumps(value) + ' ' * 4096):
            link.write_text(broken)
            with self.assertRaises(ArchiveError):
                shared.folder(self.tapas)
            with self.assertRaises(ArchiveError):
                network.policy(self.tapas)
        link.unlink(); link.symlink_to(self.top / 'elsewhere.json')
        with self.assertRaises(ArchiveError):
            shared.cloud_folder(self.tapas)

    def test_shared_cloud_settings_of_an_unexpected_shape_are_refused(self):
        shared.link(self.tapas, self.store)
        (self.store / 'cloud').mkdir()
        target = {'remote': 'b2', 'config': '/private/connection.conf', 'enabled': False, 'tested': False,
                  'target_id': '00000000-0000-4000-8000-000000000000'}
        for broken in ({'targets': {}}, {'schema_version': 2, 'targets': {}}, {'schema_version': True, 'targets': {}},
                       {'schema_version': 1, 'targets': {'ftp': target}}, {'schema_version': 1, 'targets': {'b2': target | {'tested': 1}}},
                       {'schema_version': 1, 'targets': []}):
            (self.store / 'cloud/targets.json').write_text(json.dumps(broken))
            with self.assertRaises(ArchiveError):
                cloud.settings(self.tapas)
        (self.store / 'cloud/targets.json').write_text(json.dumps({'schema_version': 1, 'targets': {'b2': target}}))
        self.assertEqual(cloud.settings(self.tapas)['targets'], {'b2': target})


if __name__ == '__main__':
    unittest.main()
