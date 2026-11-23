import hashlib
import json
import os
import unittest
from unittest.mock import Mock, patch

from comic_archive import extras, extras_backfill
from comic_archive.cancellation import RunCancelled
from comic_archive.errors import AuthenticationError, NetworkUnavailable
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from test_extras import ALL_OFF, MP3
import test_jobs


class BackfillTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp

    def choose(self, **choices):
        with Inventory(self.root) as inventory:
            extras.save(inventory.db, ALL_OFF | choices)

    def queue(self):
        with Inventory(self.root, read_only=True) as inventory:
            return inventory.pending()

    def archives(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for cbz in self.root.rglob('*.cbz') for path in (cbz, cbz.with_suffix('.json'))}

    def start(self, **options):
        with patch('builtins.print'):
            return archive_discovered(self.adapter, self.root, **options)

    def saved(self):
        with patch('builtins.print'):
            archive_discovered(self.adapter, self.root, self.report)
        self.adapter.downloads.clear()
        self.adapter.calls.clear()
        self.adapter.episode_extras = Mock(return_value=extras.found('Original note', {'likes': 7}))

    def extra(self, key='81'):
        return json.loads((self.root / f'.archive-state/extras/tapas/42/{key}.json').read_text())

    def test_later_selection_collects_saved_episodes_in_resumable_batches_without_page_downloads(self):
        self.saved()
        before = self.archives()
        self.choose(notes=True)
        self.assertEqual(len(self.queue()), 2)
        first = self.start(limit=1)
        self.assertEqual(first['archived_episode_ids'], [])
        self.assertEqual(first['extras_episode_ids'], ['81'])
        self.assertEqual(first['downloaded_pages'], 0)
        self.assertEqual(first['status'], 'limited')
        self.assertEqual([row['id'] for row in self.queue()], ['82'])
        second = self.start(limit=1)
        self.assertEqual(second['extras_episode_ids'], ['82'])
        self.assertEqual(second['status'], 'finished')
        self.assertEqual(self.extra()['creator_note'], 'Original note')
        self.assertEqual(self.archives(), before)
        self.assertEqual(self.adapter.downloads, [])
        self.adapter.calls.clear()
        self.assertEqual(self.start()['extras_episode_ids'], [])
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'capture' for call in self.adapter.calls))

    def test_selecting_another_kind_preserves_earlier_extras_and_music_is_not_requested_again(self):
        self.saved()
        self.choose(notes=True)
        self.start()
        self.adapter.episode_extras.return_value = extras.found('Changed note', music=(('https://tapas.io/1.mp3', 'Tune'),))
        self.adapter.retrieve_extra = Mock(return_value=MP3)
        self.choose(music=True)
        self.start()
        self.assertEqual(self.extra()['creator_note'], 'Original note')
        self.assertEqual(len(self.extra()['music']), 1)
        self.assertEqual(self.adapter.retrieve_extra.call_count, 2)
        self.choose(notes=True, music=True)
        self.assertEqual(self.queue(), [])
        self.start()
        self.assertEqual(self.adapter.retrieve_extra.call_count, 2)

    def test_absent_extras_are_checked_once_without_a_permanent_queue(self):
        self.saved()
        self.adapter.episode_extras.return_value = extras.found()
        self.choose(notes=True, music=True, stats=True, comments=True)
        self.start()
        self.assertEqual(self.queue(), [])
        self.assertFalse((self.root / '.archive-state/extras').exists())
        self.start()
        self.assertEqual(self.adapter.episode_extras.call_count, 2)

    def test_pause_and_signin_loss_leave_archives_saved_and_extras_waiting(self):
        for error in (RunCancelled('Pause'), AuthenticationError('Sign in')):
            with self.subTest(error=type(error).__name__):
                self.choose()
                self.saved()
                self.choose(notes=True)
                before = self.archives()
                with patch.object(self.adapter, 'capture_automatic', side_effect=error):
                    if isinstance(error, AuthenticationError):
                        with self.assertRaises(AuthenticationError):
                            self.start()
                    else:
                        self.assertEqual(self.start()['status'], 'cancelled')
                self.assertEqual(len(self.queue()), 2)
                with Inventory(self.root, read_only=True) as inventory:
                    self.assertEqual([row[0] for row in inventory.db.execute('SELECT status FROM episodes')],
                                     ['archived', 'archived'])
                self.assertEqual(self.archives(), before)

    def test_network_pause_retains_work_and_completed_extras(self):
        self.saved()
        self.choose(notes=True)
        self.start(limit=1)
        before = self.extra()
        with patch.object(self.adapter, 'capture_automatic', side_effect=NetworkUnavailable('Offline')):
            self.assertEqual(self.start()['status'], 'paused')
        self.assertEqual([row['id'] for row in self.queue()], ['82'])
        self.assertEqual(self.extra(), before)
        self.start()
        self.assertEqual(self.queue(), [])

    def test_refused_episodes_have_bounded_attempts_without_requeuing_comics(self):
        self.saved()
        before = self.archives()
        self.choose(notes=True)
        self.adapter.denied = {'81', '82'}
        for _ in range(3):
            self.start()
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.archives(), before)
        self.assertEqual(self.adapter.downloads, [])
        self.assertEqual(self.adapter.calls.count(('capture', '81')), 3)
        self.choose(notes=True, stats=True)
        self.assertEqual(len(self.queue()), 2)
        self.adapter.denied.clear()
        self.start()
        self.assertEqual(self.queue(), [])

    def test_missing_old_receipt_table_is_safe_for_read_only_clients(self):
        self.saved()
        self.choose(notes=True)
        with Inventory(self.root) as inventory:
            inventory.db.execute('DROP TABLE IF EXISTS extras_collections')
            inventory.db.commit()
        self.assertEqual(len(self.queue()), 2)
        self.start()
        self.assertEqual(self.queue(), [])

    def test_older_saved_extras_are_adopted_without_reading_the_episode_again(self):
        self.saved()
        self.choose(notes=True, music=True)
        self.adapter.episode_extras.return_value = extras.found('Original note', music=(('https://tapas.io/1.mp3', 'Tune'),))
        self.adapter.retrieve_extra = Mock(return_value=MP3)
        self.start()
        before = self.extra()
        with Inventory(self.root) as inventory:
            inventory.db.execute('DROP TABLE extras_collections')
            inventory.db.commit()
        self.adapter.calls.clear()
        self.adapter.retrieve_extra.reset_mock()
        self.adapter.episode_extras.reset_mock()
        self.start()
        self.assertEqual(self.extra(), before)
        self.assertEqual(self.queue(), [])
        self.adapter.retrieve_extra.assert_not_called()
        self.adapter.episode_extras.assert_not_called()
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'capture' for call in self.adapter.calls))

    def test_controller_schedules_extras_only_work_and_counts_it_as_progress(self):
        from comic_archive import download_control as dc
        self.saved()
        self.choose(notes=True)
        self.assertEqual(dc._inventory(self.root)['pending'], 2)
        result = self.start(limit=1)
        self.assertTrue(dc._valid(result, {'options': {'limit': 1}}))
        self.assertTrue(dc.progressed(result))
        self.assertEqual(dc._inventory(self.root)['pending'], 1)

    def test_newly_archived_episodes_do_not_need_a_second_extras_pass(self):
        self.choose(notes=True)
        self.adapter.episode_extras = Mock(return_value=extras.found('Saved together'))
        with patch('builtins.print'):
            result = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(result['archived_episode_ids'], ['82', '81'])
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.extra()['creator_note'], 'Saved together')

    def test_failed_music_preserves_notes_and_can_be_retried_after_choices_change(self):
        from comic_archive.models import ArchiveError
        self.saved()
        self.choose(notes=True)
        self.start()
        self.adapter.episode_extras.return_value = extras.found(music=(('https://tapas.io/1.mp3', 'Tune'),))
        self.adapter.retrieve_extra = Mock(side_effect=ArchiveError('Unavailable'))
        self.choose(music=True)
        for _ in range(3):
            self.start()
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.extra()['creator_note'], 'Original note')
        self.assertNotIn('music', self.extra())
        self.choose(notes=True, music=True)
        self.adapter.retrieve_extra = Mock(return_value=MP3)
        self.start()
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.extra()['creator_note'], 'Original note')
        self.assertEqual(len(self.extra()['music']), 1)

    def test_unreadable_existing_extras_are_retained_without_overwriting_them(self):
        self.saved()
        self.choose(notes=True)
        self.start()
        path = self.root / '.archive-state/extras/tapas/42/81.json'
        broken = b'{incomplete record'
        path.write_bytes(broken)
        self.choose(stats=True)
        for _ in range(3):
            self.start()
        self.assertEqual(path.read_bytes(), broken)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.adapter.downloads, [])

    def test_missing_legacy_music_is_collected_instead_of_adopted_as_complete(self):
        self.saved()
        self.choose(music=True)
        self.adapter.episode_extras.return_value = extras.found(music=(('https://tapas.io/1.mp3', 'Tune'),))
        self.adapter.retrieve_extra = Mock(return_value=MP3)
        self.start()
        missing = self.root / '.archive-state/extras/tapas/42' / self.extra()['music'][0]['file']
        missing.unlink()
        with Inventory(self.root) as inventory:
            inventory.db.execute('DROP TABLE extras_collections')
            inventory.db.commit()
        self.adapter.retrieve_extra.reset_mock()
        self.start()
        self.assertEqual(missing.read_bytes(), MP3)
        self.assertEqual(self.adapter.retrieve_extra.call_count, 1)
        self.assertEqual(self.queue(), [])

    def legacy_tracks(self, contents):
        folder = extras.folder(self.root, 'tapas', '42')
        folder.mkdir(parents=True, exist_ok=True)
        tracks = []
        for index, data in enumerate(contents, 1):
            name = f'81.music-{index}.mp3'
            (folder / name).write_bytes(data)
            tracks.append({'file': name, 'sha256': hashlib.sha256(data).hexdigest()})
        record = {'schema_version': 1, 'site': 'tapas', 'series': '42', 'episode': '81',
                  'creator_note': 'Original note', 'music': tracks}
        (folder / '81.json').write_text(json.dumps(record), encoding='utf-8')
        return folder, tracks

    def test_legacy_music_refuses_duplicate_file_identity_and_track_limit_before_hashing(self):
        folder, tracks = self.legacy_tracks((MP3, MP3))
        with patch.object(extras, 'MAX_TRACKS', 1), patch('comic_archive.extras_backfill.hashlib.sha256') as digest:
            self.assertFalse(extras_backfill._legacy_music(folder, '81', tracks, Mock()))
            digest.assert_not_called()
        same_file = folder / tracks[1]['file']
        same_file.unlink()
        os.link(folder / tracks[0]['file'], same_file)
        with Inventory(self.root) as inventory, patch('comic_archive.extras_backfill.hashlib.sha256') as digest:
            extras_backfill.adopt_existing(inventory, 'tapas', {'id': '81', 'series_id': '42'}, ['music', 'notes'], Mock())
            digest.assert_not_called()
            self.assertEqual(extras_backfill.records(inventory.db, '81'), {('81', 'notes'): (1, 0)})

    def test_legacy_music_checks_bytes_and_cancellation_between_chunks(self):
        folder, tracks = self.legacy_tracks((b'a' * (128 * 1024 + 1),))
        with patch.object(extras, 'MUSIC_BYTES', 65536), patch('comic_archive.extras_backfill.hashlib.sha256') as digest:
            self.assertFalse(extras_backfill._legacy_music(folder, '81', tracks, Mock()))
            digest.assert_not_called()
        check = Mock(side_effect=[None, None, RunCancelled('held')])
        with Inventory(self.root) as inventory:
            row = {'id': '81', 'series_id': '42'}
            with self.assertRaises(RunCancelled):
                extras_backfill.adopt_existing(inventory, 'tapas', row, ['music', 'notes'], check)
            self.assertEqual(extras_backfill.records(inventory.db, '81'), {})
            extras_backfill.adopt_existing(inventory, 'tapas', row, ['music', 'notes'], Mock())
            self.assertEqual(extras_backfill.records(inventory.db, '81'), {('81', 'music'): (1, 0), ('81', 'notes'): (1, 0)})

    def test_later_extras_are_collected_before_an_unfinished_comic_backlog(self):
        self.saved()
        before = self.archives()
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [
                {'id': '81', 'title': 'E81', 'order': 1, 'access_state': 'tapas_free'},
                {'id': '82', 'title': 'E82', 'order': 2, 'access_state': 'tapas_unlocked'},
                {'id': '84', 'title': 'New comic', 'order': 4, 'access_state': 'tapas_unlocked'},
            ], 'fixture')
        self.choose(notes=True)
        result = self.start(limit=1)
        self.assertEqual(result['extras_episode_ids'], ['81'])
        self.assertEqual(result['archived_episode_ids'], [])
        self.assertEqual(self.adapter.downloads, [])
        self.assertEqual(self.archives(), before)
        self.assertEqual({row['id'] for row in self.queue()}, {'82', '84'})
