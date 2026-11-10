"""The episode indicator reads recorded state without touching acquisitions or archives."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.episode_index import page
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError


class EpisodeIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    def seed(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [
                {'id': str(80 + number), 'title': f'Episode {number}', 'order': 5 if number == 3 else number,
                 'access_state': 'tapas_free' if number == 1 else 'unavailable_or_unverified'}
                for number in range(1, 4)], 'fixture')
            inventory.mark('81', 'archived')
            inventory.mark('82', 'downloading')
            inventory.observe('43', 'Unenumerated series', [], 'fixture')

    def test_uninitialized_library_stays_uninitialized(self):
        self.assertEqual(page(self.root), {'initialized': False, 'items': [], 'next_offset': None})
        self.assertFalse(self.root.exists())

    def test_records_include_saved_pending_unavailable_and_unenumerated_series(self):
        self.seed()
        with patch('comic_archive.archive.episode_archives', side_effect=AssertionError('No archive scan')), \
                patch('comic_archive.transport.Transport.get', side_effect=AssertionError('No provider request')):
            listing = page(self.root)
            self.assertEqual(listing['items'], [
                {'id': '42', 'title': 'Series', 'episodes': 3, 'saved': 1, 'formats': [],
                 'access_kinds': ['free', 'unclassified']},
                {'id': '43', 'title': 'Unenumerated series', 'episodes': 0, 'saved': 0, 'formats': [], 'access_kinds': []}])
            episodes = page(self.root, '42')['items']
        self.assertEqual([row['status'] for row in episodes], ['archived', 'downloading', 'unavailable'])
        self.assertEqual([row['episode_number'] for row in episodes], [1, 2, 5])
        self.assertTrue(all('archive_path' not in row and 'source_url' not in row for row in episodes))
        # Opening an Inventory writer would reset this downloading row. This view must not.
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='82'").fetchone()[0], 'downloading')
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 3)

    def test_same_title_distinct_ids_keep_recorded_formats_and_access_separate(self):
        self.seed()
        with Inventory(self.root) as inventory:
            inventory.observe('44', 'Series', [
                {'id': '90', 'title': 'Chapter', 'order': 1, 'access_state': 'tapas_unlocked',
                 'access_expires_at': '2000-01-01T00:00:00+00:00'}], 'fixture')
            inventory.db.execute("UPDATE episodes SET archive_path='private/comic.CBZ' WHERE id='81'")
            inventory.db.execute("UPDATE episodes SET archive_path='private/novel.epub' WHERE id='90'")
            inventory.db.commit()
        # These paths intentionally do not exist; the view must use recorded metadata only.
        rows = [row for row in page(self.root)['items'] if row['title'] == 'Series']
        self.assertEqual([row['id'] for row in rows], ['42', '44'])
        self.assertEqual([row['formats'] for row in rows], [['CBZ'], ['EPUB']])
        self.assertEqual(rows[1]['access_kinds'], ['limited'])
        chapter = page(self.root, '44')['items'][0]
        self.assertEqual((chapter['format'], chapter['access_kind']), ('EPUB', 'limited'))
        self.assertNotIn('archive_path', chapter)

    def test_pagination_is_bounded_and_does_not_invent_or_duplicate_episodes(self):
        self.seed()
        first = page(self.root, '42', limit=2)
        second = page(self.root, '42', offset=first['next_offset'], limit=2)
        self.assertEqual([row['id'] for row in first['items'] + second['items']], ['81', '82', '83'])
        self.assertIsNone(second['next_offset'])
        first_series = page(self.root, limit=1)
        self.assertEqual(first_series['next_offset'], 1)
        self.assertEqual(page(self.root, offset=1, limit=1)['items'][0]['id'], '43')

    def test_unknown_series_and_invalid_page_arguments_cannot_query_arbitrary_content(self):
        self.seed()
        for options in ({'series_id': '../secret'}, {'series_id': '99'}, {'offset': True},
                        {'offset': -1}, {'limit': 201}, {'limit': 0}, {'series_id': 42}):
            with self.subTest(options=options), self.assertRaises(ArchiveError):
                page(self.root, **options)

    def test_symlinked_inventory_is_refused(self):
        self.seed()
        database = self.root / '.archive-state' / 'inventory.sqlite3'
        target = database.with_name('retained.sqlite3')
        database.rename(target)
        database.symlink_to(target)
        with self.assertRaises(ArchiveError):
            page(self.root)
