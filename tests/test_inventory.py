import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from comic_archive.errors import AuthenticationError, LibraryBusy, MetadataError
from comic_archive.inventory import MAX_FAILED_ATTEMPTS, Inventory
from comic_archive.locking import LibraryLock
from comic_archive.models import ArchiveError


def lock_is_free(root):
    """Another thread of this process can take the writer lock only if it was released."""
    def take():
        with LibraryLock(root):
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(take).result()
        except LibraryBusy:
            return False


def episode(key, order=1, access='tapas_free', title='E'):
    return {'id': key, 'title': title, 'order': order, 'access_state': access}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = self.work / 'Library'

    def test_symlinked_library_or_state_folder_is_refused_without_writing_through_it(self):
        real = self.work / 'real'
        real.mkdir()
        linked_library = self.work / 'linked-library'
        linked_library.symlink_to(real, target_is_directory=True)
        linked_state = self.work / 'linked-state'
        linked_state.mkdir()
        (linked_state / '.archive-state').symlink_to(real, target_is_directory=True)
        for root in (linked_library, linked_state):
            for read_only in (False, True):
                with self.subTest(root=root.name, read_only=read_only), self.assertRaises(ArchiveError):
                    Inventory(root, read_only=read_only)
        self.assertEqual(list(real.iterdir()), [])

    def test_read_only_open_needs_an_existing_inventory_and_creates_nothing(self):
        with self.assertRaises(ArchiveError):
            Inventory(self.root, read_only=True)
        self.assertFalse(self.root.exists())

    def test_unreadable_inventory_file_is_left_alone_and_does_not_keep_the_library_locked(self):
        state = self.root / '.archive-state'
        state.mkdir(parents=True)
        path = state / 'inventory.sqlite3'
        path.write_bytes(b'this is not a database ' * 100)
        damaged = path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            Inventory(self.root)
        self.assertEqual(path.read_bytes(), damaged)
        self.assertTrue(lock_is_free(self.root))
        path.unlink()
        path.mkdir()
        with self.assertRaises(sqlite3.OperationalError):
            Inventory(self.root)
        self.assertTrue(path.is_dir())
        self.assertTrue(lock_is_free(self.root))
        path.rmdir()
        with Inventory(self.root) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'], {})

    def test_account_binding_needs_a_verified_identity_and_stores_nothing_without_one(self):
        with Inventory(self.root) as inventory:
            for identity in (None, '', 11, b'11'):
                with self.subTest(identity=identity):
                    with self.assertRaises(AuthenticationError):
                        inventory.bind_account(identity)
                    with self.assertRaises(AuthenticationError):
                        inventory.account_binding_state(identity)
            self.assertEqual(inventory.account_binding_state('11'), 'unbound')
            inventory.bind_account('11')
            self.assertEqual(inventory.account_binding_state('11'), 'matches')
            with self.assertRaises(AuthenticationError):
                inventory.account_binding_state('12')

    def test_damaged_account_binding_is_reported_and_kept_rather_than_replaced(self):
        with Inventory(self.root) as inventory:
            inventory.bind_account('11')
            salt, binding = (inventory.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                             for key in ('account_salt', 'account_binding'))
            for label, damaged in (('short salt', {'account_salt': 'ab' * 16}),
                                   ('salt is not hex', {'account_salt': 'zz' * 32}),
                                   ('binding is not a digest', {'account_binding': 'PRIVATE'})):
                values = {'account_salt': salt, 'account_binding': binding} | damaged
                for key, value in values.items():
                    inventory.db.execute('UPDATE settings SET value=? WHERE key=?', (value, key))
                inventory.db.commit()
                with self.subTest(label):
                    with self.assertRaises(MetadataError) as raised:
                        inventory.account_binding_state('11')
                    self.assertNotIn('PRIVATE', str(raised.exception))
                    stored = dict(inventory.db.execute(
                        "SELECT key,value FROM settings WHERE key IN ('account_salt','account_binding')"))
                    self.assertEqual(stored, values)

    def test_invalid_series_or_episode_metadata_is_refused_and_leaves_no_partial_rows(self):
        with Inventory(self.root) as inventory:
            for series_id, title in (('abc', 'S'), ('0', 'S'), ('', 'S'), (42, 'S'), ('٤٢', 'S'),
                                     ('1' * 21, 'S'), ('42', None)):
                with self.subTest(series_id=series_id, title=title), self.assertRaises(MetadataError):
                    inventory.observe(series_id, title, [], 'fixture')
            bad_items = ({'id': 'abc'}, {'id': '0'}, {'id': 81}, {'title': None},
                         {'access_state': 'reader_confirmed'}, {'access_state': None},
                         {'order': 0}, {'order': True}, {'order': '1'}, {'order': -3})
            for change in bad_items:
                with self.subTest(change=change), self.assertRaises(MetadataError):
                    inventory.observe('42', 'S', [episode('81'), episode('82', 2) | change], 'fixture')
            with self.subTest('repeated id'), self.assertRaises(MetadataError):
                inventory.observe('42', 'S', [episode('81'), episode('81', 2)], 'fixture')
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM series').fetchone()[0], 0)
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM episodes').fetchone()[0], 0)
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)

    def test_an_episode_cannot_move_to_another_series_and_the_refusal_changes_nothing(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'First', [episode('81')], 'fixture')
            with self.assertRaises(MetadataError):
                inventory.observe('43', 'Second', [episode('82', 2), episode('81', 1, 'tapas_unlocked')], 'fixture')
            self.assertEqual([tuple(r) for r in inventory.db.execute('SELECT id,series_id,access_state FROM episodes')],
                             [('81', '42', 'tapas_free')])
            self.assertEqual([r['id'] for r in inventory.watched_series()], ['42'])


    def write_report(self, value, name='report.json'):
        path = self.work / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_discovery_report_must_be_a_plain_bounded_file_of_the_expected_schema(self):
        series = {'source_series_id': '42', 'title': 'S', 'checked_at': '2026-10-02T00:00:00Z',
                  'accessible_episodes': [episode('81')]}
        good = {'schema_version': 1, 'source': 'tapas', 'series': [series]}
        real = self.write_report(good)
        link = self.work / 'link.json'
        link.symlink_to(real)
        huge = self.work / 'huge.json'
        with huge.open('wb') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        bad = {'broken json': self.write_report('{broken', 'a.json'),
               'not an object': self.write_report([], 'b.json'),
               'another source': self.write_report(good | {'source': 'other'}, 'c.json'),
               'another schema': self.write_report(good | {'schema_version': 2}, 'd.json'),
               'no series list': self.write_report({'schema_version': 1, 'source': 'tapas'}, 'e.json'),
               'series without a check time': self.write_report(
                   good | {'series': [{k: v for k, v in series.items() if k != 'checked_at'}]}, 'f.json'),
               'series list is not a list': self.write_report(good | {'series': 7}, 'g.json')}
        with Inventory(self.root) as inventory:
            for label, path in ({'symlink': link, 'too large': huge} | bad).items():
                with self.subTest(label), self.assertRaises(MetadataError):
                    inventory.ingest_report(path)
            self.assertEqual(inventory.watched_series(), [])
            inventory.ingest_report(real)
            self.assertEqual([r['id'] for r in inventory.pending()], ['81'])


    def test_scan_gaps_ignore_rows_with_unknown_codes_and_old_inventories_without_the_table(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [], 'fixture')
            inventory.observe('43', 'T', [], 'fixture')
            inventory.record_scan_gap('43', 2, 3)
            inventory.db.execute("INSERT INTO series_scan_gaps VALUES ('42','2026-10-01T00:00:00+00:00','PRIVATE_CODE',NULL,NULL)")
            inventory.db.commit()
            gaps = inventory.scan_gaps()
            self.assertEqual([g['source_series_id'] for g in gaps], ['43'])
            self.assertNotIn('PRIVATE', json.dumps(gaps))
        with Inventory(self.root) as inventory:
            inventory.db.execute('DROP TABLE series_scan_gaps')
            inventory.db.execute('DROP TABLE episode_expirations')
            inventory.observe('44', 'U', [episode('90')], 'fixture')
            inventory.db.commit()
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.scan_gaps(), [])
            self.assertEqual([row['id'] for row in inventory.pending()], ['90'])


    def test_an_episode_a_job_keeps_failing_on_goes_last_then_is_held_until_its_access_changes(self):
        listed = [episode('81', 1), episode('82', 2), episode('83', 3)]
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', listed, 'subscribed')
            inventory.mark('81', 'failed', 'job_failed', attempted=True)
            self.assertEqual([row['id'] for row in inventory.pending()], ['82', '83', '81'])
            inventory.observe('42', 'S', listed, 'subscribed')  # An unchanged refresh keeps it last.
            row = inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(tuple(row), ('failed', 'job_failed'))
            self.assertEqual([row['id'] for row in inventory.pending()], ['82', '83', '81'])
            for _ in range(MAX_FAILED_ATTEMPTS - 1):
                inventory.mark('81', 'failed', 'job_failed', attempted=True)
            self.assertEqual([row['id'] for row in inventory.pending()], ['82', '83'])
            inventory.observe('42', 'S', [episode('81', 1, 'tapas_unlocked')] + listed[1:], 'subscribed')
            self.assertEqual([row['id'] for row in inventory.pending()], ['81', '82', '83'])

if __name__ == '__main__':
    unittest.main()
