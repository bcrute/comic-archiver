from contextlib import closing
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

    def test_completion_retry_keeps_other_checkpoints_and_attempts_each_gap_once(self):
        with Inventory(self.root) as inventory:
            for key in ('42', '43'):
                inventory.observe(key, 'Series', [], 'fixture')
                inventory.record_series_scan(key)
            inventory.record_scan_gap('42', 2, 3)
            before = [tuple(row) for row in inventory.db.execute('SELECT * FROM series_scans')]
            self.assertEqual(inventory.prepare_completion_retry(), (1, 0))
            self.assertEqual([row['id'] for row in inventory.scan_candidates()], ['42'])
            self.assertEqual(inventory.status()['scan_remaining_series_count'], 1)
        with Inventory(self.root) as inventory:
            self.assertEqual([row['id'] for row in inventory.scan_candidates()], ['42'])
            inventory.record_scan_gap('42', 2, 3)
            self.assertEqual(inventory.scan_candidates(), [])
            self.assertTrue(inventory.finish_scan_cycle())
            self.assertEqual(inventory.status()['scan_remaining_series_count'], 0)
            self.assertEqual([tuple(row) for row in inventory.db.execute('SELECT * FROM series_scans')], before)
            self.assertEqual(len(inventory.scan_gaps()), 1)
            self.assertIsNone(inventory.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone())

    def test_completion_retry_requeues_only_incomplete_readers_and_never_saved_files(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81'), episode('82'), episode('83')], 'fixture')
            inventory.mark('81', 'unsupported', 'novel_manifest_incomplete')
            inventory.mark('82', 'unsupported', 'scheduled')
            path = self.root / 'Series/003.cbz'
            path.parent.mkdir()
            path.write_bytes(b'saved archive fixture')
            inventory.db.execute("UPDATE episodes SET status='archived',archive_path='Series/003.cbz',"
                                 "file_signature='saved' WHERE id='83'")
            inventory.db.commit()
            self.assertEqual(inventory.prepare_completion_retry(), (0, 1))
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
            row = inventory.db.execute("SELECT status,archive_path,file_signature FROM episodes WHERE id='83'").fetchone()
            self.assertEqual(tuple(row), ('archived', 'Series/003.cbz', 'saved'))
            self.assertEqual(path.read_bytes(), b'saved archive fixture')

    def test_completion_retry_gives_episodes_set_aside_after_failures_one_more_cycle(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81'), episode('82', 2), episode('83', 3),
                                               episode('84', 4, 'unavailable_or_unverified')], 'fixture')
            for key, times in (('81', MAX_FAILED_ATTEMPTS), ('82', MAX_FAILED_ATTEMPTS - 1), ('84', MAX_FAILED_ATTEMPTS)):
                for _ in range(times):
                    inventory.mark(key, 'failed', 'job_failed', attempted=True)
            inventory.mark('83', 'failed', 'source_refused')
            inventory.db.execute("UPDATE episodes SET attempts=? WHERE id='83'", (MAX_FAILED_ATTEMPTS,))
            self.assertNotIn('81', [row['id'] for row in inventory.pending()])
            self.assertEqual(inventory.prepare_completion_retry(), (0, 1))
            rows = {row['id']: (row['status'], row['error_code'], row['attempts'])
                    for row in inventory.db.execute('SELECT * FROM episodes')}
            # Only the one that had used its tries; one still being tried, a refusal and one without access stay.
            self.assertEqual(rows, {'81': ('queued', None, 0), '82': ('failed', 'job_failed', MAX_FAILED_ATTEMPTS - 1),
                                    '83': ('failed', 'source_refused', MAX_FAILED_ATTEMPTS),
                                    '84': ('failed', 'job_failed', MAX_FAILED_ATTEMPTS)})
            self.assertEqual([row['id'] for row in inventory.pending()][0], '81')
            self.assertEqual(inventory.prepare_completion_retry(), (0, 0))

    def test_unchecked_series_precede_old_refusals_without_resetting_scan_or_reader_checkpoints(self):
        with Inventory(self.root) as inventory:
            for key in ('1', '2', '3', '90', '91', '92'):
                inventory.observe(key, 'Series ' + key, [], 'fixture')
            inventory.observe('1', 'Old refusals', [episode('81', access='unavailable_or_unverified')], 'fixture')
            for key in ('1', '2'):
                inventory.record_series_scan(key)
            inventory.db.execute("UPDATE series_scans SET observed_at='2000-01-01T00:00:00+00:00'")
            inventory.scan_candidates()
            cycle = inventory.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone()[0]
            inventory.record_series_scan('3')
            inventory.record_series_unavailable('92', 404)
            inventory.record_reader_denial('81')
            episodes = [tuple(r) for r in inventory.db.execute('SELECT * FROM episodes')]
            denials = [tuple(r) for r in inventory.db.execute('SELECT * FROM reader_denials')]
            self.assertEqual([r['id'] for r in inventory.scan_candidates(2)], ['90', '91'])
            self.assertEqual([r['id'] for r in inventory.scan_candidates()], ['90', '91', '1', '2'])
            inventory.record_series_scan('90')
        with Inventory(self.root) as inventory:
            self.assertEqual([r['id'] for r in inventory.scan_candidates()], ['91', '1', '2'])
            self.assertEqual(inventory.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone()[0], cycle)
            self.assertEqual([tuple(r) for r in inventory.db.execute('SELECT * FROM episodes')], episodes)
            self.assertEqual([tuple(r) for r in inventory.db.execute('SELECT * FROM reader_denials')], denials)
            self.assertEqual([r['source_series_id'] for r in inventory.scan_gaps()], ['92'])

    def test_reader_denial_survives_refresh_and_restart_until_next_round_or_changed_hint(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81')], 'fixture')
            inventory.scan_candidates()
            inventory.record_reader_denial('81')
            inventory.record_access('81', 'unavailable_or_unverified')
            inventory.mark('81', 'unavailable', 'access_denied')
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81')], 'fixture')
            self.assertEqual(inventory.pending(), [])
            inventory.observe('42', 'Series', [episode('81', access='tapas_unlocked')], 'fixture')
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
            inventory.record_reader_denial('81')
            inventory.record_access('81', 'unavailable_or_unverified')
            inventory.observe('42', 'Series', [episode('81', access='tapas_unlocked')], 'fixture')
            self.assertEqual(inventory.pending(), [])
            inventory.db.execute("UPDATE settings SET value='2099-01-01T00:00:00+00:00' WHERE key='scan_cycle_started'")
            inventory.observe('42', 'Series', [episode('81', access='tapas_unlocked')], 'fixture')
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])

    def test_negative_bulk_hint_preserves_existing_candidate_but_does_not_queue_whole_catalogue(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81')], 'fixture')
            inventory.observe('42', 'Series', [episode('81', access='unavailable_or_unverified'),
                                               episode('82', order=2, access='unavailable_or_unverified')], 'fixture')
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])

    def test_episode_types_separate_saved_pending_and_unavailable_without_opening_files(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81'), episode('82', access='tapas_unlocked'),
                episode('83', access='tapas_limited'), episode('84', access='tapas_readable'),
                episode('85', access='unavailable_or_unverified'), episode('86', access='tapas_unlocked') |
                {'access_expires_at': '2000-01-01T00:00:00+00:00'}], 'fixture')
            inventory.mark('81', 'archived')
            inventory.mark('82', 'archived')
            inventory.mark('83', 'archived')
            inventory.mark('84', 'failed', 'job_failed')
            self.assertEqual(inventory.episode_type_counts(), {
                'free': {'saved': 1, 'waiting': 0}, 'bought': {'saved': 1, 'waiting': 0},
                'limited': {'saved': 1, 'waiting': 1}, 'unclassified': {'saved': 0, 'waiting': 1}})
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.status()['episode_types']['limited'], {'saved': 1, 'waiting': 1})

    def test_confirmed_reader_success_clears_previous_denial(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [episode('81')], 'fixture')
            inventory.record_reader_denial('81')
            inventory.record_access('81', 'tapas_free', reader_confirmed=True)
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM reader_denials WHERE episode_id='81'").fetchone())
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])

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


    def test_tappytoon_correction_preserves_saved_partial_attempted_and_historically_owned_work(self):
        with Inventory(self.root) as inventory:
            rows = [episode(str(key), key - 80, 'tappytoon_readable') for key in range(81, 91)]
            rows[5]['access_state'] = 'tappytoon_free'
            rows[8]['access_state'] = 'webtoon_readable'
            inventory.observe('42', 'Series', rows, 'sync')
            inventory.mark('82', 'queued', attempted=True)
            inventory.mark('83', 'archived')
            inventory.mark('84', 'failed', 'job_failed', attempted=True)
            inventory.mark('85', 'downloading', attempted=True)
            inventory.db.execute("UPDATE episodes SET archive_path='Series/partial.cbz' WHERE id='87'")
            inventory.db.execute("INSERT INTO observations VALUES ('88','2026-10-01T00:00:00+00:00','tappytoon_unlocked')")
            inventory.mark('90', 'unavailable', 'access_denied', attempted=True)
            partial = self.root / 'Series' / 'partial.cbz'
            partial.parent.mkdir()
            partial.write_bytes(b'existing partial work')
            before = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
            inventory.enable_tappytoon_entitlement_discovery()
            after = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
            self.assertEqual((after['81']['status'], after['81']['access_state'], after['81']['attempts']),
                             ('unavailable', 'unavailable_or_unverified', 0))
            self.assertIsNone(after['81']['error_code'])
            self.assertEqual({key: value for key, value in after.items() if key != '81'},
                             {key: value for key, value in before.items() if key != '81'})
            self.assertEqual(partial.read_bytes(), b'existing partial work')
            # A newly bought chapter is discoverable again; direct reader confirmations remain valid.
            inventory.observe('42', 'Series', [episode('81', 1, 'tappytoon_unlocked'),
                episode('91', 11, 'tappytoon_readable')], 'episode_check')
        with Inventory(self.root) as inventory:
            inventory.enable_tappytoon_entitlement_discovery()
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='81'").fetchone()[0], 'queued')
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='91'").fetchone()[0], 'queued')

    def test_header_fallback_upgrade_requeues_only_old_missing_metadata_once(self):
        with Inventory(self.root) as inventory:
            for key in ('42', '43', '44'):
                inventory.observe(key, 'Series', [], 'fixture')
                inventory.record_series_scan(key)
            inventory.record_series_unavailable('42', 404)
            inventory.record_series_unavailable('43', 410)
            inventory.record_scan_gap('44', 1, 2)
            inventory.observe('42', 'Series', [episode('81'), episode('82', 2)], 'fixture')
            inventory.mark('81', 'failed', 'image_invalid', attempted=True)
            inventory.mark('82', 'archived')
            before = [tuple(row) for row in inventory.db.execute('SELECT * FROM episodes')]
            inventory.enable_series_header_fallback()
            self.assertEqual([row[0] for row in inventory.db.execute('SELECT series_id FROM series_scans')], ['44'])
            self.assertEqual([row['source_series_id'] for row in inventory.scan_gaps()], ['44'])
            self.assertEqual([tuple(row) for row in inventory.db.execute('SELECT * FROM episodes')], before)
            inventory.record_series_scan('42')
            inventory.record_series_unavailable('42', 404)
        with Inventory(self.root) as inventory:
            inventory.enable_series_header_fallback()
            self.assertEqual({row['source_series_id'] for row in inventory.scan_gaps()}, {'42', '44'})
            self.assertEqual({row[0] for row in inventory.db.execute('SELECT series_id FROM series_scans')}, {'42', '44'})

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

    def test_product_flag_recovery_is_once_and_only_resets_exhausted_accessible_webtoon_failures(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [episode('81', access='webtoon_free'),
                episode('82', access='webtoon_unlocked'), episode('83', access='tapas_free'),
                episode('84', access='webtoon_free')], 'subscribed')
            for key in ('81', '82', '83'):
                for _ in range(MAX_FAILED_ATTEMPTS):
                    inventory.mark(key, 'failed', 'job_failed', attempted=True)
            inventory.mark('84', 'failed', 'job_failed', attempted=True)
            before = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
            inventory.db.execute("DELETE FROM settings WHERE key='webtoon_product_flag_recheck'")
            inventory.db.commit()
        with Inventory(self.root, read_only=True) as inventory:
            self.assertIn('81', [row['id'] for row in inventory.pending()])
            self.assertIn('82', [row['id'] for row in inventory.pending()])
            self.assertNotIn('83', [row['id'] for row in inventory.pending()])
            self.assertEqual(dict(inventory.db.execute("SELECT * FROM episodes WHERE id='81'").fetchone()), before['81'])
        with Inventory(self.root) as inventory:
            after = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
            for key in ('81', '82'):
                self.assertEqual(after.pop(key), before.pop(key) | {'status': 'queued', 'error_code': None, 'attempts': 0})
            self.assertEqual(after, before)
            for _ in range(MAX_FAILED_ATTEMPTS):
                inventory.mark('81', 'failed', 'job_failed', attempted=True)
        with Inventory(self.root) as inventory:
            self.assertNotIn('81', [row['id'] for row in inventory.pending()])
            self.assertEqual(inventory.db.execute("SELECT attempts FROM episodes WHERE id='81'").fetchone()[0], MAX_FAILED_ATTEMPTS)

    def test_product_flag_recovery_rolls_back_if_the_checkpoint_cannot_be_saved(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [episode('81', access='webtoon_free')], 'subscribed')
            for _ in range(MAX_FAILED_ATTEMPTS):
                inventory.mark('81', 'failed', 'job_failed', attempted=True)
            inventory.db.execute("DELETE FROM settings WHERE key='webtoon_product_flag_recheck'")
            inventory.db.execute("CREATE TRIGGER fail_product_upgrade BEFORE INSERT ON settings "
                "WHEN NEW.key='webtoon_product_flag_recheck' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
            inventory.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            Inventory(self.root)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(tuple(inventory.db.execute("SELECT status,error_code,attempts FROM episodes WHERE id='81'").fetchone()),
                             ('failed', 'job_failed', MAX_FAILED_ATTEMPTS))
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM settings WHERE key='webtoon_product_flag_recheck'").fetchone())

    def test_image_structure_upgrade_requeues_only_exhausted_accessible_failures_once(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [episode(str(key), key - 80, access) for key, access in (
                (81, 'webtoon_free'), (82, 'tapas_free'), (83, 'webtoon_free'),
                (84, 'webtoon_free'), (85, 'webtoon_free'), (86, 'webtoon_free'),
                (87, 'unavailable_or_unverified'), (88, 'tappytoon_free'), (89, 'tapas_unlocked'))], 'subscribed')
            for key, status, code, attempts in (
                    ('81', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS),
                    ('82', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS),
                    ('83', 'unavailable', 'access_denied', MAX_FAILED_ATTEMPTS),
                    ('84', 'archived', None, MAX_FAILED_ATTEMPTS),
                    ('85', 'queued', 'asset_read_unavailable', MAX_FAILED_ATTEMPTS),
                    ('86', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS - 1),
                    ('87', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS),
                    ('88', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS),
                    ('89', 'failed', 'job_failed', MAX_FAILED_ATTEMPTS)):
                inventory.db.execute('UPDATE episodes SET status=?,error_code=?,attempts=? WHERE id=?',
                                     (status, code, attempts, key))
            inventory.db.execute("DELETE FROM settings WHERE key='image_structure_recheck'")
            inventory.db.commit()
            before = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
        with Inventory(self.root, read_only=True) as inventory:
            self.assertIn('81', [row['id'] for row in inventory.pending()])
            self.assertIn('82', [row['id'] for row in inventory.pending()])
            self.assertNotIn('87', [row['id'] for row in inventory.pending()])
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM settings WHERE key='image_structure_recheck'").fetchone())
            self.assertEqual(dict(inventory.db.execute("SELECT * FROM episodes WHERE id='81'").fetchone()), before['81'])
        with Inventory(self.root) as inventory:
            after = {row['id']: dict(row) for row in inventory.db.execute('SELECT * FROM episodes')}
            for key in ('81', '82', '88', '89'):
                self.assertEqual(after.pop(key), before.pop(key) | {'status': 'queued', 'error_code': None, 'attempts': 0})
            self.assertEqual(after, before)
            self.assertIn('81', [row['id'] for row in inventory.pending()])
            for _ in range(MAX_FAILED_ATTEMPTS):
                inventory.mark('81', 'failed', 'job_failed', attempted=True)
        with Inventory(self.root) as inventory:
            self.assertNotIn('81', [row['id'] for row in inventory.pending()])
            self.assertEqual(tuple(inventory.db.execute("SELECT status,error_code,attempts FROM episodes WHERE id='81'").fetchone()),
                             ('failed', 'job_failed', MAX_FAILED_ATTEMPTS))

    def test_starts_counted_by_earlier_versions_are_forgotten_once_and_failures_kept(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [episode(str(key), key - 80, access) for key, access in (
                (81, 'tapas_free'), (82, 'tapas_free'), (83, 'tapas_free'), (84, 'tapas_free'), (85, 'tappytoon_readable'))],
                'subscribed')
            for key, status, code, attempts in (('81', 'queued', 'interrupted', 5), ('82', 'failed', 'job_failed', 2),
                                                ('83', 'archived', None, 4), ('84', 'failed', 'source_refused', 3),
                                                ('85', 'queued', None, 1)):
                inventory.db.execute('UPDATE episodes SET status=?,error_code=?,attempts=? WHERE id=?', (status, code, attempts, key))
            inventory.db.execute("DELETE FROM settings WHERE key IN ('attempts_count_failures','tappytoon_entitlement_discovery_enabled')")
            inventory.db.commit()
        def counts():
            return dict(inventory.db.execute('SELECT id,attempts FROM episodes'))
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(counts(), {'81': 5, '82': 2, '83': 4, '84': 3, '85': 1})
        with Inventory(self.root) as inventory:
            # A tried Tappytoon seed keeps its count until the correction that reads it has run.
            self.assertEqual(counts(), {'81': 0, '82': 2, '83': 0, '84': 0, '85': 1})
            inventory.db.execute('UPDATE episodes SET attempts=7')
            inventory.db.commit()
        with Inventory(self.root) as inventory:
            self.assertEqual(set(counts().values()), {7})

    def test_image_structure_upgrade_rolls_back_the_requeue_if_its_marker_cannot_be_saved(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'S', [episode('81', access='webtoon_free')], 'subscribed')
            for _ in range(MAX_FAILED_ATTEMPTS):
                inventory.mark('81', 'failed', 'job_failed', attempted=True)
            inventory.db.execute("DELETE FROM settings WHERE key='image_structure_recheck'")
            inventory.db.execute("CREATE TRIGGER fail_upgrade BEFORE INSERT ON settings "
                                 "WHEN NEW.key='image_structure_recheck' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
            inventory.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            Inventory(self.root)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(tuple(inventory.db.execute("SELECT status,error_code,attempts FROM episodes WHERE id='81'").fetchone()),
                             ('failed', 'job_failed', MAX_FAILED_ATTEMPTS))
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM settings WHERE key='image_structure_recheck'").fetchone())
        with closing(sqlite3.connect(self.root / '.archive-state/inventory.sqlite3')) as db:
            with db:
                db.execute('DROP TRIGGER fail_upgrade')
        with Inventory(self.root) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])

if __name__ == '__main__':
    unittest.main()
