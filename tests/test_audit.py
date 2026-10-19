import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.archive import verify
from comic_archive.audit import audit_library
from comic_archive.errors import LibraryBusy
from comic_archive.archive import Store
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'
        with Inventory(self.root) as inventory:
            inventory.bind_account('ACCOUNT_PRIVATE')
            inventory.observe('42', 'Series', [
                {'id': '81', 'title': 'Free', 'order': 1, 'access_state': 'tapas_free'},
                {'id': '82', 'title': 'Unlocked', 'order': 2, 'access_state': 'tapas_unlocked'},
                {'id': '83', 'title': 'Denied', 'order': 3, 'access_state': 'unavailable_or_unverified'}], 'subscribed')
            inventory.record_series_scan('42')
        self.adapter = AccountAdapter()
        self.adapter.identity = 'ACCOUNT_PRIVATE'

    def archive(self, limit=None):
        with patch('builtins.print'), patch('comic_archive.jobs.Store',Store):
            # Partial-audit fixtures deliberately preserve free episode 81;
            # bulk dispatch priority is covered by QueuePriorityTests.
            archive_discovered(self.adapter, self.root, limit=limit, free_only=limit is not None)

    def test_partial_inventory_report_distinguishes_integrity_and_missing_accessible_episodes(self):
        self.archive(limit=1)
        result = audit_library(self.root)
        self.assertEqual(result['verified_inventory_episode_count'], 1)
        self.assertEqual(result['missing_recorded_accessible_episode_count'], 1)
        self.assertTrue(result['archive_integrity_passed'])
        self.assertFalse(result['observed_snapshot_archived'])
        self.assertFalse(result['account_inventory_complete'])
        self.assertFalse(result['account_archive_complete'])
        rows = {row['source_episode_id']: row for row in result['episodes']}
        self.assertEqual(rows['82']['reason'], 'awaiting_archive')
        self.assertEqual(rows['83']['reason'], 'unavailable_or_unverified')
        self.assertFalse(result['remote_authorization_checked'])
        calls = self.adapter.calls.copy()
        audit_library(self.root)
        self.assertEqual(self.adapter.calls, calls)

    def test_report_names_the_site_the_library_saves_from(self):
        self.assertEqual(audit_library(self.root)['source'], 'tapas')
        other = self.root.parent / 'Tappytoon' / 'Library'
        with Inventory(other) as inventory:
            inventory.observe('7', 'Series', [{'id': '70', 'title': 'Free', 'order': 1, 'access_state': 'tappytoon_free'}], 'subscribed')
        self.assertEqual(audit_library(other)['source'], 'tappytoon')

    def test_all_observed_candidates_archived_never_asserts_complete_account_or_changes_state(self):
        self.archive()
        with Inventory(self.root, read_only=True) as inventory:
            before = [tuple(row) for row in inventory.db.execute('SELECT * FROM episodes')]
            settings = [tuple(row) for row in inventory.db.execute('SELECT * FROM settings')]
        result = audit_library(self.root)
        self.assertTrue(result['observed_snapshot_archived'])
        self.assertFalse(result['account_archive_complete'])
        self.assertEqual(result['coverage_blockers'], ['independent_purchase_inventory_unverified'])
        self.assertEqual(result['missing_recorded_accessible_episode_count'], 0)
        self.assertEqual(result['verified_inventory_episode_count'], 2)
        self.assertEqual(result['series'][0]['provenance'], ['episode_check', 'subscribed'])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(before, [tuple(row) for row in inventory.db.execute('SELECT * FROM episodes')])
            self.assertEqual(settings, [tuple(row) for row in inventory.db.execute('SELECT * FROM settings')])
        self.assertNotIn('ACCOUNT_PRIVATE', json.dumps(result))
        self.assertNotIn('account_binding', json.dumps(result))

    def test_fresh_hash_verification_detects_corruption_even_when_index_claims_archived(self):
        self.archive(limit=1)
        path = next(self.root.glob('*/*.cbz'))
        path.write_bytes(b'corrupt')
        result = audit_library(self.root)
        self.assertFalse(result['archive_integrity_passed'])
        self.assertEqual(result['verified_inventory_episode_count'], 0)
        self.assertEqual(result['integrity_problems'], [{'source_episode_id': '81', 'reason': 'archive_integrity_failed'}])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='81'").fetchone()[0], 'archived')

    def test_valid_checksums_do_not_hide_wrong_episode_or_series_identity(self):
        self.archive(limit=1)
        path = next(self.root.glob('*/*.cbz')).with_suffix('.json')
        meta = json.loads(path.read_text())
        meta['source_episode_id'] = '82'
        path.write_text(json.dumps(meta))
        result = audit_library(self.root)
        self.assertEqual(result['integrity_problems'][0]['reason'], 'archive_identity_mismatch')
        meta['source_episode_id'] = '81'
        meta['source_series_id'] = '43'
        path.write_text(json.dumps(meta))
        self.assertEqual(audit_library(self.root)['integrity_problems'][0]['reason'], 'series_identity_mismatch')

    def test_stale_queue_index_can_find_existing_valid_exports_without_mutating_it(self):
        self.archive(limit=1)
        with Inventory(self.root) as inventory:
            inventory.db.execute("UPDATE episodes SET status='queued',archive_path=NULL WHERE id='81'")
            inventory.db.commit()
        result = audit_library(self.root)
        self.assertEqual(result['verified_inventory_episode_count'], 1)
        self.assertEqual(result['inventory_index_problems'], [{'source_episode_id': '81', 'reason': 'inventory_index_out_of_sync'}])
        self.assertFalse(result['observed_snapshot_archived'])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='81'").fetchone()[0], 'queued')

    def test_invalid_identity_in_otherwise_hash_valid_metadata_is_a_reported_integrity_problem(self):
        self.archive(limit=1)
        path = next(self.root.glob('*/*.cbz')).with_suffix('.json')
        meta = json.loads(path.read_text())
        meta['source_episode_id'] = {'cookie': 'PRIVATE'}
        path.write_text(json.dumps(meta))
        result = audit_library(self.root)
        self.assertFalse(result['archive_integrity_passed'])
        self.assertEqual(result['integrity_problems'][0]['reason'], 'archive_metadata_identity_invalid')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_duplicate_and_unknown_exports_remain_visible_without_claiming_account_coverage(self):
        self.archive()
        original = next(self.root.glob('*/*.cbz'))
        duplicate = original.with_name('duplicate.cbz')
        shutil.copy2(original, duplicate)
        shutil.copy2(original.with_suffix('.json'), duplicate.with_suffix('.json'))
        result = audit_library(self.root)
        self.assertTrue(result['archive_integrity_passed'])
        self.assertEqual(len(result['unindexed_archives']), 1)
        self.assertEqual(result['unindexed_archives'][0]['reason'], 'archive_not_reconciled_to_inventory')
        self.assertFalse(result['observed_snapshot_archived'])

    def test_archived_content_survives_later_access_loss_and_holds_have_explicit_reasons(self):
        self.archive(limit=1)
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [
                {'id': '81', 'title': 'Free', 'order': 1, 'access_state': 'unavailable_or_unverified', 'authorization_reason': 'access_denied'},
                {'id': '84', 'title': 'Novel', 'order': 4, 'access_state': 'unavailable_or_unverified', 'authorization_reason': 'novel'},
                {'id': '85', 'title': 'Unknown order', 'order': None, 'access_state': 'tapas_unlocked'}], 'sync')
        result = audit_library(self.root)
        rows = {row['source_episode_id']: row for row in result['episodes']}
        self.assertTrue(rows['81']['archive_verified'])
        self.assertEqual(rows['84']['reason'], 'novel')
        self.assertEqual(rows['85']['reason'], 'ordering_unverified')
        self.assertEqual(result['missing_recorded_accessible_episode_count'], 2)

    def test_no_private_directories_or_private_error_text_enters_the_report(self):
        self.archive(limit=1)
        folder = next(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.'))
        private = self.root / '.private'
        shutil.copytree(folder, private)
        with Inventory(self.root) as inventory:
            inventory.db.execute("UPDATE episodes SET error_code='COOKIE_PRIVATE' WHERE id='82'")
            inventory.db.execute("UPDATE series SET provenance=?", (json.dumps(['subscribed', 'COOKIE_PRIVATE']),))
            inventory.db.commit()
        result = audit_library(self.root)
        self.assertEqual(result['portable_archive_count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_outside_traversal_hidden_and_symlinked_archive_paths_are_explicitly_invalid(self):
        self.archive(limit=1)
        for relative in ('/tmp/PRIVATE.cbz', '../PRIVATE.cbz', '.private/x.cbz', 'folder/\0.cbz'):
            with Inventory(self.root) as inventory:
                inventory.db.execute("UPDATE episodes SET archive_path=? WHERE id='81'", (relative,))
                inventory.db.commit()
            result = audit_library(self.root)
            self.assertEqual(result['integrity_problems'][0]['reason'], 'archive_path_invalid')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_report_rewrites_only_its_own_json_and_is_not_mistaken_for_episode_metadata(self):
        self.archive()
        report = self.root / 'audit.json'
        audit_library(self.root, report)
        audit_library(self.root, report)
        self.assertEqual(verify(self.root), [])
        self.assertEqual(json.loads(report.read_text())['document_type'], 'comic_archive_reconciliation')
        for protected in (next(self.root.glob('*/*.cbz')), next(self.root.glob('*/series.json')),
                          self.root / '.archive-state/inventory.sqlite3'):
            previous = protected.read_bytes()
            with self.assertRaises(ArchiveError): audit_library(self.root, protected)
            self.assertEqual(protected.read_bytes(), previous)

    def test_large_account_report_is_recognized_without_hiding_an_episode_sidecar(self):
        self.archive()
        path=self.root/'audit.json'
        value=audit_library(self.root)
        value['padding']='x'*(17*1024*1024)
        path.write_text(json.dumps(value))
        self.assertEqual(verify(self.root),[])
        value['source_episode_id']='81'
        path.write_text(json.dumps(value))
        self.assertIn('audit.json: missing CBZ or EPUB',verify(self.root))
        with self.assertRaises(ArchiveError):audit_library(self.root,path)
        value.pop('source_episode_id');path.write_text(json.dumps(value))
        audit_library(self.root,path)
        self.assertEqual(verify(self.root),[])

    def test_audit_does_not_create_a_missing_library_and_rejects_an_active_writer(self):
        missing = self.root / 'absent'
        with self.assertRaises(ArchiveError): audit_library(missing)
        self.assertFalse(missing.exists())
        with patch('comic_archive.audit.LibraryLock.__enter__', side_effect=LibraryBusy('Busy')):
            with self.assertRaises(LibraryBusy): audit_library(self.root)

    def set_archive_path(self, value):
        with Inventory(self.root) as inventory:
            inventory.db.execute("UPDATE episodes SET archive_path=? WHERE id='81'", (value,))
            inventory.db.commit()

    def test_recorded_archive_that_is_gone_is_missing_not_silently_dropped(self):
        self.archive(limit=1)
        archive = next(self.root.glob('*/*.cbz'))
        archive.unlink(); archive.with_suffix('.json').unlink()
        for forget_path in (False, True):
            with self.subTest(forget_path=forget_path):
                if forget_path: self.set_archive_path(None)
                result = audit_library(self.root)
                self.assertEqual(result['integrity_problems'], [{'source_episode_id': '81', 'reason': 'archive_missing'}])
                self.assertFalse(result['archive_integrity_passed'])
                self.assertEqual(result['verified_inventory_episode_count'], 0)
                self.assertEqual(result['portable_archive_count'], 0)
                self.assertIn('portable_files_require_repair', result['coverage_blockers'])

    def test_damaged_series_metadata_or_cover_is_reported_per_folder(self):
        self.archive(limit=1)
        path = next(self.root.glob('*/series.json')); good = path.read_text()
        for name, text in (('not json', '{broken'), ('blank title', json.dumps(json.loads(good) | {'title': ''}))):
            with self.subTest(name):
                path.write_text(text)
                result = audit_library(self.root)
                self.assertEqual(result['series_metadata_problems'],
                                 [{'series_folder': path.parent.name, 'reason': 'series_metadata_or_cover_invalid'}])
                self.assertFalse(result['archive_integrity_passed'])
                self.assertIn('portable_files_require_repair', result['coverage_blockers'])
                self.assertEqual(result['verified_inventory_episode_count'], 1)
        path.write_text(good)
        self.assertEqual(audit_library(self.root)['series_metadata_problems'], [])

    def test_missing_episode_metadata_is_an_integrity_failure_not_a_crash(self):
        self.archive(limit=1)
        next(self.root.glob('*/*.cbz')).with_suffix('.json').unlink()
        result = audit_library(self.root)
        self.assertEqual(result['integrity_problems'], [{'source_episode_id': '81', 'reason': 'archive_integrity_failed'}])
        self.assertEqual(result['verified_inventory_episode_count'], 0)

    def test_corrupt_extra_archive_is_listed_as_unindexed_and_as_an_integrity_problem(self):
        self.archive(limit=1)
        original = next(self.root.glob('*/*.cbz'))
        duplicate = original.with_name('duplicate.cbz')
        shutil.copy2(original, duplicate); shutil.copy2(original.with_suffix('.json'), duplicate.with_suffix('.json'))
        duplicate.write_bytes(b'corrupt')
        result = audit_library(self.root)
        relative = duplicate.relative_to(self.root).as_posix()
        self.assertEqual(result['unindexed_archives'], [{'archive_path': relative, 'reason': 'archive_integrity_failed'}])
        self.assertEqual(result['integrity_problems'], [{'archive_path': relative, 'reason': 'archive_integrity_failed'}])
        self.assertEqual(result['verified_inventory_episode_count'], 1)

    def test_unreadable_series_provenance_is_reported_as_empty_not_trusted(self):
        for text in ('not json', json.dumps({'subscribed': True}), json.dumps('subscribed')):
            with self.subTest(text=text):
                with Inventory(self.root) as inventory:
                    inventory.db.execute("UPDATE series SET provenance=?", (text,)); inventory.db.commit()
                self.assertEqual(audit_library(self.root)['series'][0]['provenance'], [])

    def test_symlinked_archive_is_not_followed_as_a_portable_export(self):
        self.archive(limit=1)
        archive = next(self.root.glob('*/*.cbz')); outside = self.root.parent/'outside.cbz'
        archive.rename(outside); archive.symlink_to(outside)
        result = audit_library(self.root)
        self.assertEqual(result['integrity_problems'][0], {'source_episode_id': '81', 'reason': 'archive_path_invalid'})
        self.assertEqual(result['verified_inventory_episode_count'], 0)
        self.assertEqual(result['unindexed_archives'][0]['reason'], 'archive_path_invalid')

    def test_corrupt_inventory_identity_stops_the_audit_instead_of_reporting_on_it(self):
        with Inventory(self.root) as inventory:
            inventory.db.execute("UPDATE episodes SET id='not-a-number' WHERE id='83'"); inventory.db.commit()
        with self.assertRaisesRegex(ArchiveError, 'identities are invalid'): audit_library(self.root)

    def test_report_path_holding_other_json_or_a_directory_is_refused_and_untouched(self):
        self.archive(limit=1)
        other = self.root/'other.json'; other.write_text('[1, 2]')
        with self.assertRaises(ArchiveError): audit_library(self.root, other)
        self.assertEqual(other.read_text(), '[1, 2]')
        folder = self.root/'x.json'; folder.mkdir()
        with self.assertRaises(ArchiveError): audit_library(self.root, folder)
        self.assertTrue(folder.is_dir()); self.assertEqual(list(folder.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
