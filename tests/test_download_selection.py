"""User-selected scope survives reruns without deleting or restoring excluded books."""
import json
from pathlib import Path
import tempfile
import unittest

from comic_archive import download_selection as selection, sites
from comic_archive.audit import check_completion
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


def choices(*enabled):
    return {key: key in enabled for key in selection.TYPES}


def episode(key, access, order=1, **extra):
    return dict(id=key, title='Episode', order=order, access_state=access, **extra)


class DownloadSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    def test_defaults_preserve_every_type_and_saved_choices_are_strict_and_durable(self):
        with Inventory(self.root) as inventory:
            self.assertEqual(selection.read(inventory.db), dict.fromkeys(selection.TYPES, True))
            selection.save(inventory.db, choices('bought'))
            for invalid in ({}, choices(), {'free': True}, choices('free') | {'bought': 1}, None):
                with self.subTest(invalid=invalid), self.assertRaises(ArchiveError):
                    selection.save(inventory.db, invalid)
                self.assertEqual(selection.read(inventory.db), choices('bought'))
            with self.assertRaises(ValueError):
                selection.clause(inventory.db, 'e; DELETE FROM episodes')
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(selection.read(inventory.db), choices('bought'))

    def test_paid_choice_includes_free_companions_for_every_provider_but_not_unrelated_free_series(self):
        for site in sites.SITES:
            with self.subTest(site=site.key), Inventory(self.root / site.key) as inventory:
                inventory.observe('42', 'Purchased series',
                                  [episode('81', site.free), episode('82', site.unlocked, 2)], 'fixture')
                inventory.observe('43', 'Unrelated free series', [episode('91', site.free)], 'fixture')
                selection.save(inventory.db, choices('bought'))
                self.assertEqual({row['id'] for row in inventory.pending()}, {'81', '82'})
                self.assertEqual(inventory.status()['excluded_pending_episode_count'], 1)
                self.assertEqual(inventory.episode_type_counts()['free']['waiting'], 1)
                # Permanent purchase evidence survives a later public-free label.
                inventory.record_access('82', site.free, expires_at=None)
                self.assertEqual({row['id'] for row in inventory.pending()}, {'81', '82'})

    def test_limited_and_unknown_types_are_separate_and_neither_implies_a_purchase(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Temporary unlocks', [
                episode('81', 'tapas_free'), episode('82', 'tapas_unlocked', 2, access_expires_at='2020-01-01T00:00:00Z'),
                episode('83', 'tapas_limited', 3), episode('84', 'tapas_readable', 4),
                episode('85', 'tapas_readable', 5, access_expires_at='2099-01-01T00:00:00Z')], 'fixture')
            selection.save(inventory.db, choices('limited'))
            self.assertEqual({row['id'] for row in inventory.pending()}, {'82', '83', '85'})
            self.assertEqual(selection.access_kind('tapas_readable', '2099-01-01T00:00:00Z'), 'limited')
            self.assertEqual(inventory.episode_type_counts()['limited']['waiting'], 3)
            selection.save(inventory.db, choices('bought'))
            self.assertEqual(inventory.pending(), [])
            selection.save(inventory.db, choices('unclassified'))
            self.assertEqual([row['id'] for row in inventory.pending()], ['84'])

    def test_corrupt_preferences_fail_closed_instead_of_restoring_the_all_types_default(self):
        with Inventory(self.root) as inventory:
            inventory.db.execute('INSERT INTO settings VALUES (?,?)', (selection.KEY, '{broken'))
            inventory.db.commit()
            with self.assertRaises(ArchiveError):
                inventory.pending()
            selection.save(inventory.db, choices('free'))
            self.assertEqual(selection.read(inventory.db), choices('free'))

    def test_inconclusive_bulk_hints_keep_types_and_rental_expiry_until_a_reader_reclassifies_them(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81', 'tapas_free'),
                episode('82', 'tapas_unlocked', 2, access_expires_at='2099-01-01T00:00:00Z')], 'fixture')
            inventory.observe('42', 'Series', [episode('81', 'tapas_readable', access_expires_at=None),
                episode('82', 'tapas_readable', 2, access_expires_at=None)], 'sync')
            selection.save(inventory.db, choices('limited'))
            self.assertEqual([row['id'] for row in inventory.pending()], ['82'])
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM download_paid_series').fetchone()[0], 0)
            selection.save(inventory.db, choices('free'))
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
            inventory.record_access('81', 'tapas_unlocked', reader_confirmed=True)
            self.assertEqual(inventory.pending(), [])

    def test_deleted_unwanted_free_episode_stays_deleted_across_subsequent_runs_until_reselected(self):
        adapter = AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Free series', [episode('81', 'tapas_free')], 'fixture')
        self.assertEqual(archive_discovered(adapter, self.root)['archived_episode_ids'], ['81'])
        with Inventory(self.root) as inventory:
            path = self.root / inventory.db.execute("SELECT archive_path FROM episodes WHERE id='81'").fetchone()[0]
            original = path.read_bytes()
            selection.save(inventory.db, choices('bought'))
        self.assertEqual(path.read_bytes(), original)  # Changing choices never deletes existing books.
        path.unlink()  # The user's own deletion, not an application action.
        adapter.calls.clear()
        adapter.downloads.clear()
        for _ in range(2):
            result = archive_discovered(adapter, self.root)
            self.assertEqual(result['archived_episode_ids'], [])
            self.assertEqual(result['pending_episode_count'], 0)
            self.assertFalse(path.exists())
        self.assertNotIn(('capture', '81'), adapter.calls)
        self.assertEqual(adapter.downloads, [])
        self.assertTrue(check_completion(self.root)['completion_check_passed'])
        with Inventory(self.root) as inventory:
            selection.save(inventory.db, choices('free'))
        self.assertEqual(archive_discovered(adapter, self.root)['archived_episode_ids'], ['81'])
        self.assertTrue(path.is_file())

    def test_reader_reclassification_is_applied_before_images_are_requested(self):
        adapter = AccountAdapter()
        adapter.access['81'] = 'tapas_unlocked'
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81', 'tapas_readable')], 'fixture')
            selection.save(inventory.db, choices('unclassified'))
        result = archive_discovered(adapter, self.root)
        self.assertEqual(result['excluded_episode_ids'], ['81'])
        self.assertEqual(result['archived_episode_ids'], [])
        self.assertEqual(adapter.downloads, [])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.pending(), [])
            self.assertEqual(json.loads(inventory.db.execute('SELECT summary FROM runs').fetchone()[0])['excluded_episode_ids'], ['81'])
