"""Cache removal requires a verified archive and successful directory syncs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from comic_archive.purge import evict_episode
from test_archive_budgets import MEMBER, METADATA, PAGE, write_comic


class CacheDurabilityTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.archive = root / 'series' / 'issue.cbz'
        self.archive.parent.mkdir()
        write_comic(self.archive)
        self.archive.with_suffix('.json').write_text(json.dumps(METADATA))
        cache = root / '.cache'
        cache.mkdir()
        self.blob = cache / MEMBER['sha256']
        self.blob.write_bytes(PAGE)
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE archived_pages (hash, path, member, size)')
        self.store = SimpleNamespace(root=root, cache=cache, db=db, provenance=root / '.provenance')
        self.store.provenance.mkdir()

    def test_a_folder_whose_flush_is_refused_keeps_the_cache(self):
        with patch('comic_archive.purge.flush_directory', return_value=False):
            self.assertEqual(evict_episode(self.store, self.archive, METADATA), 0)
        self.assertEqual(self.blob.read_bytes(), PAGE)
        with closing(self.store.db.execute('SELECT path FROM archived_pages')) as rows:
            self.assertEqual(rows.fetchone()[0], 'series/issue.cbz')

    def test_a_saved_episode_frees_its_cached_originals_on_this_system_and_says_how_much(self):
        # The real flush of real folders: Linux, macOS and Windows each go through their own. Only the room is
        # stood in for, which some file systems count differently from one moment to the next.
        with patch('comic_archive.purge.disk_bytes', return_value=4096):
            self.assertEqual(evict_episode(self.store, self.archive, METADATA), 4096)
        self.assertFalse(self.blob.exists())

    def test_every_required_directory_must_report_durable(self):
        for outcomes in ((True, False), (False, True)):
            with self.subTest(outcomes=outcomes), patch('comic_archive.purge.flush_directory', side_effect=outcomes):
                self.assertEqual(evict_episode(self.store, self.archive, METADATA), 0)
                self.assertEqual(self.blob.read_bytes(), PAGE)
        with patch('comic_archive.purge.flush_directory', return_value=True) as sync:
            evict_episode(self.store, self.archive, METADATA)
            self.assertEqual([call.args[0] for call in sync.call_args_list],
                             [self.archive.parent, self.store.provenance])
        self.assertFalse(self.blob.exists())
