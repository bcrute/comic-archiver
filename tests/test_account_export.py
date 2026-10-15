import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import account_export, batches, completion
from comic_archive.archive import atomic_json
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.errors import LibraryBusy


class AccountExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.library = self.project / 'Library'
        self.session = self.project / '.comic-session'
        self.root = self.session / 'worker'
        (self.root / 'requests').mkdir(parents=True)
        (self.root / 'responses').mkdir()
        with Inventory(self.library) as inv:
            inv.observe('42', 'Series', [], 'subscribed')

    def tick(self):
        return account_export.worker_tick(self.session, self.library)

    def test_fresh_account_status_is_read_only_but_import_still_requires_a_worker(self):
        project = self.project / 'fresh'
        project.mkdir()
        session, library = project / '.comic-session', project / 'Library'
        self.assertEqual(account_export.status(session, library),
                         {'configured': False, 'account_inventory_complete': False})
        self.assertEqual(list(project.iterdir()), [])
        with self.assertRaises(ArchiveError):
            account_export.configure(session, library)
        self.assertEqual(list(project.iterdir()), [])
        session.symlink_to(self.session, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            account_export.status(session, library)

    def request(self):
        for _ in range(8):
            self.tick()
            files = list((self.root / 'requests').glob('*.json'))
            if files:
                self.assertEqual(len(files), 1)
                return files[0], json.loads(files[0].read_text())
        self.fail('Expected one durable task')

    def finish(self, path, result, error=None):
        response = {'task_id': path.stem, 'status': 'ok', 'result': result}
        if error:
            response.update(status='error', error_code=error)
        atomic_json(self.root / 'responses' / path.name, response)
        path.unlink()
        self.tick()

    def lists(self):
        for category in account_export.CATEGORIES:
            path, req = self.request()
            self.assertEqual(req['operation'], 'sync')
            self.assertEqual(req['args']['categories'], [category])
            self.assertTrue(req['args']['scan_only'])
            with Inventory(self.library) as inv:
                inv.scan_candidates()
            self.finish(path, {'status': 'finished', 'refresh': {'reading_lists': [
                {'category': category, 'complete': category != 'recent',
                 'boundary_observed': category != 'recent', 'observed_series_count': 1}]}})

    def test_empty_queue_still_discovers_scans_and_audits_before_finishing(self):
        account_export.configure(self.session, self.library)
        self.lists()
        path, req = self.request()
        self.assertEqual(req['args']['refresh_lists'], False)
        self.assertTrue(req['args']['scan_only'])
        with Inventory(self.library) as inv:
            inv.record_series_scan('42')
            inv.finish_scan_cycle()
        self.finish(path, {'status': 'finished'})
        path, req = self.request()
        self.assertEqual(req, {'operation': 'audit_account', 'args': {}})
        self.finish(path, {'archive_integrity_passed': True,
                          'missing_recorded_accessible_episode_count': 0,
                          'inventory_index_problem_count': 0, 'unindexed_archive_count': 0})
        value = account_export.status(self.session, self.library)
        self.assertEqual(value['status'], 'finished')
        self.assertFalse(value['account_inventory_complete'])
        self.assertIn('independent_purchase_inventory_unverified', value['coverage_blockers'])

    def test_unconfigured_worker_in_an_unrelated_session_remains_available(self):
        self.assertIsNone(account_export.worker_tick(self.project / 'unrelated-session', self.library))

    def test_queue_publication_reuses_reserved_identity_after_restart(self):
        account_export.configure(self.session, self.library)
        self.tick()  # Reserve without publication.
        state = account_export._load(self.root)
        identity = state['pending']['task_id']
        path, request = self.request()
        self.assertEqual(path.stem, identity)
        for _ in range(3):
            self.tick()
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [path])
        self.assertEqual(json.loads(path.read_text()), request)
        self.assertEqual((self.root / 'account-export.json').stat().st_mode & 0o777, PRIVATE_FILE)

    def test_source_refusal_holds_exact_receipt_and_submits_no_successor(self):
        account_export.configure(self.session, self.library)
        path, _ = self.request()
        self.finish(path, {'status': 'paused', 'pause_reason': 'source_refused'}, error='source_refused')
        for _ in range(3):
            self.tick()
        value = account_export.status(self.session, self.library)
        self.assertEqual(value['status'], 'held')
        self.assertEqual(value['pending_task_id'], path.stem)
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_scan_without_progress_holds_instead_of_looping(self):
        account_export.configure(self.session, self.library)
        self.lists()
        path, _ = self.request()
        self.finish(path, {'status': 'limited'})
        self.assertEqual(account_export.status(self.session, self.library)['code'], 'scan_no_progress')
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_newly_discovered_access_is_saved_before_the_next_scan(self):
        account_export.configure(self.session, self.library)
        self.lists()
        with Inventory(self.library) as inv:
            inv.observe('42', 'Series', [{'id': '81', 'title': 'Chapter', 'order': 1,
                         'access_state': 'tapas_free'}], 'sync')
        with patch('comic_archive.completion.continue_discovered') as start:
            self.tick()
            start.assert_called_once()
        self.assertEqual(account_export.status(self.session, self.library)['phase'], 'series')
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])
        with patch('comic_archive.completion.status', return_value={'status': 'running'}), \
                patch('comic_archive.completion.continue_discovered') as start:
            self.tick()
            start.assert_not_called()
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_fast_account_scans_are_small_metadata_only_jobs(self):
        account_export.configure(self.session, self.library, window_budgets={'speed': 'fast'})
        self.lists()
        _, request = self.request()
        self.assertEqual(request['args']['metadata_speed'], 'fast')
        self.assertEqual(request['args']['scan_limit'], 10)
        self.assertTrue(request['args']['scan_only'])

    def test_missing_accessible_content_fails_final_audit_even_if_queue_empty(self):
        account_export.configure(self.session, self.library)
        value = account_export._load(self.root)
        value['phase'] = 'verify'
        account_export._save(self.root, value)
        path, _ = self.request()
        self.finish(path, {'archive_integrity_passed': True,
                          'missing_recorded_accessible_episode_count': 9,
                          'inventory_index_problem_count': 0, 'unindexed_archive_count': 0})
        self.assertEqual(account_export.status(self.session, self.library)['code'], 'archive_incomplete')

    def test_explicit_resume_replaces_failed_task_but_retains_its_receipt(self):
        account_export.configure(self.session, self.library)
        path, request = self.request()
        self.finish(path, {'status': 'paused', 'pause_reason': 'login_required'}, error='login_required')
        receipt = self.root / 'responses' / path.name
        original = receipt.read_bytes()
        account_export.configure(self.session, self.library, resume=True)
        replacement, replay = self.request()
        self.assertNotEqual(replacement.stem, path.stem)
        self.assertEqual(replay, request)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(account_export.status(self.session, self.library)['last_failed_task_id'], path.stem)

    def test_stop_during_interleaved_extraction_stops_completion_as_well(self):
        account_export.configure(self.session, self.library)
        state = account_export._load(self.root)
        state['phase'] = 'series'
        account_export._save(self.root, state)
        with Inventory(self.library) as inv:
            inv.observe('42', 'Series', [{'id': '81', 'title': 'Chapter', 'order': 1,
                         'access_state': 'tapas_free'}], 'sync')
        completion.configure(self.session, self.library, until_drained=True)
        for _ in range(3):
            completion.tick(self.session, self.library)
        account_export.request_stop(self.session, self.library)
        self.assertEqual(completion.status(self.session, self.library)['status'], 'stopped')
        self.assertTrue(batches._load(self.root)['stop_requested'])

    def test_stop_of_finished_export_does_not_create_an_unresumable_running_state(self):
        account_export.configure(self.session, self.library)
        state = account_export._load(self.root)
        state.update(status='finished', phase='finished')
        account_export._save(self.root, state)
        self.assertEqual(account_export.request_stop(self.session, self.library)['status'], 'finished')
        with self.assertRaises(ArchiveError):
            account_export.configure(self.session, self.library, resume=True)

    def test_resume_does_not_duplicate_an_unresolved_or_successful_pending_task(self):
        account_export.configure(self.session, self.library)
        path, request = self.request()
        account_export.request_stop(self.session, self.library)
        account_export.configure(self.session, self.library, resume=True)
        self.assertEqual(self.request()[0], path)
        self.assertEqual(json.loads(path.read_text()), request)
        account_export.request_stop(self.session, self.library)
        self.finish(path, {'status': 'finished', 'refresh': {'reading_lists': [
            {'category': 'subscribed', 'complete': True, 'boundary_observed': True, 'observed_series_count': 1}]}})
        account_export.configure(self.session, self.library, resume=True)
        self.tick()
        self.assertEqual(account_export._load(self.root)['category_index'], 1)
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_resume_requires_resolution_of_a_held_extraction_plan(self):
        account_export.configure(self.session, self.library)
        account_export.request_stop(self.session, self.library)
        with patch('comic_archive.completion.status', return_value={'status': 'paused'}):
            with self.assertRaises(ArchiveError):
                account_export.configure(self.session, self.library, resume=True)
        self.assertEqual(account_export.status(self.session, self.library)['status'], 'stopped')

    def test_invalid_task_result_holds_without_crashing_the_worker(self):
        for result in ([], None, 'PRIVATE_SOURCE_DETAILS'):
            with self.subTest(result_type=type(result).__name__):
                account_export.configure(self.session, self.library)
                path, _ = self.request()
                self.finish(path, result)
                value = account_export.status(self.session, self.library)
                self.assertEqual(value['status'], 'held')
                self.assertEqual(value['code'], 'task_result_invalid')
                self.assertNotIn('PRIVATE', json.dumps(value))
                (self.root / 'account-export.json').unlink()

    def test_audit_counters_must_be_real_integers_before_claiming_completion(self):
        fields = ('missing_recorded_accessible_episode_count', 'inventory_index_problem_count', 'unindexed_archive_count')
        for field in fields:
            for invalid in (False, 0.0, None):
                with self.subTest(field=field, invalid_type=type(invalid).__name__):
                    account_export.configure(self.session, self.library)
                    value = account_export._load(self.root)
                    value['phase'] = 'verify'
                    account_export._save(self.root, value)
                    path, _ = self.request()
                    self.finish(path, {'archive_integrity_passed': True, **dict.fromkeys(fields, 0), field: invalid})
                    self.assertEqual(account_export.status(self.session, self.library)['status'], 'held')
                    (self.root / 'account-export.json').unlink()

    def test_damaged_checkpoint_keeps_worker_available_and_preserves_evidence(self):
        account_export.configure(self.session, self.library)
        path, _ = self.request()
        checkpoint = self.root / 'account-export.json'
        checkpoint.write_text('{invalid JSON')
        self.assertIsNone(self.tick())
        self.assertEqual(checkpoint.read_text(), '{invalid JSON')
        self.assertTrue(path.is_file())
        with self.assertRaises(ArchiveError):
            account_export.status(self.session, self.library)

    def test_invalid_list_receipts_do_not_advance_or_crash(self):
        valid = {'category': 'subscribed', 'complete': True, 'boundary_observed': True, 'observed_series_count': 1}
        for checks in (None, [None], [valid, valid], [valid | {'observed_series_count': False}]):
            with self.subTest(checks=checks):
                account_export.configure(self.session, self.library)
                path, _ = self.request()
                self.finish(path, {'status': 'finished', 'refresh': {'reading_lists': checks}})
                self.assertEqual(account_export.status(self.session, self.library)['code'], 'list_checkpoint_incomplete')
                self.assertEqual(account_export._load(self.root)['category_index'], 0)
                (self.root / 'account-export.json').unlink()

    def test_untrusted_failure_details_are_not_published_in_status(self):
        account_export.configure(self.session, self.library)
        path, _ = self.request()
        self.finish(path, {'status': 'paused', 'pause_reason': 'PRIVATE_SOURCE_DETAILS'}, error='PRIVATE_ERROR')
        value = account_export.status(self.session, self.library)
        self.assertEqual(value['code'], 'task_failed')
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_boolean_checkpoint_version_is_rejected(self):
        account_export.configure(self.session, self.library)
        value = account_export._load(self.root)
        value['schema_version'] = True
        account_export._save(self.root, value)
        with self.assertRaises(ArchiveError):
            account_export.status(self.session, self.library)

    def test_existing_backlog_must_drain_before_discovery_and_holds_propagate(self):
        with patch('comic_archive.completion.status', return_value={'status': 'running'}):
            account_export.configure(self.session, self.library)
            self.assertEqual(self.tick()['phase'], 'existing_backlog')
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])
        with patch('comic_archive.completion.status', return_value={'status': 'paused', 'code': 'login_required'}):
            self.assertEqual(self.tick()['code'], 'login_required')
        with patch('comic_archive.completion.status', return_value={'status': 'known_backlog_drained'}):
            account_export.configure(self.session, self.library, resume=True)
            self.assertEqual(self.tick()['phase'], 'lists')
        self.assertEqual(self.request()[1]['args']['categories'], ['subscribed'])

    def test_held_extraction_propagates_in_both_discovery_and_archive_phases(self):
        for phase in ('series', 'archive'):
            for status in ('paused', 'stopped'):
                with self.subTest(phase=phase, status=status):
                    account_export.configure(self.session, self.library)
                    state = account_export._load(self.root)
                    state['phase'] = phase
                    account_export._save(self.root, state)
                    with patch('comic_archive.completion.status', return_value={'status': status, 'code': 'source_refused'}):
                        self.assertEqual(self.tick()['code'], 'source_refused')
                    self.assertEqual(list((self.root / 'requests').glob('*.json')), [])
                    (self.root / 'account-export.json').unlink()

    def test_archive_phase_waits_for_or_starts_extraction_before_audit(self):
        account_export.configure(self.session, self.library)
        state = account_export._load(self.root)
        state['phase'] = 'archive'
        account_export._save(self.root, state)
        with Inventory(self.library) as inv:
            inv.observe('42', 'Series', [{'id': '81', 'title': 'Chapter', 'order': 1, 'access_state': 'tapas_free'}], 'sync')
        with patch('comic_archive.completion.status', return_value={'status': 'running'}), patch('comic_archive.completion.continue_discovered') as start:
            self.assertEqual(self.tick()['phase'], 'archive')
            start.assert_not_called()
        with patch('comic_archive.completion.continue_discovered') as start:
            self.tick()
            start.assert_called_once()
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_unpublished_reservation_survives_stop_and_explicit_resume(self):
        account_export.configure(self.session, self.library)
        self.tick()
        identity = account_export.status(self.session, self.library)['pending_task_id']
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])
        account_export.request_stop(self.session, self.library)
        account_export.configure(self.session, self.library, resume=True)
        self.assertEqual(self.request()[0].stem, identity)

    def test_new_export_and_stop_require_valid_existing_state(self):
        with self.assertRaises(ArchiveError):
            account_export.request_stop(self.session, self.library)
        for status in ('paused', 'stopped'):
            with patch('comic_archive.completion.status', return_value={'status': status}):
                with self.assertRaises(ArchiveError):
                    account_export.configure(self.session, self.library)
        account_export.configure(self.session, self.library)
        with self.assertRaises(ArchiveError):
            account_export.configure(self.session, self.library)

    def test_invalid_durable_checkpoint_cannot_publish_or_replace_work(self):
        account_export.configure(self.session, self.library)
        self.tick()
        original = account_export._load(self.root)
        variants = [original | {'audit': []}, original | {'last_failed_task_id': '../outside'},
                    original | {'window_budgets': original['window_budgets'] | {'archive_only': False}},
                    original | {'pending': original['pending'] | {'before_remaining': False}},
                    original | {'list_checks': [{'category': 'subscribed', 'complete': True}]},
                    {k: v for k, v in original.items() if k != 'phase'}]
        for state in variants:
            with self.subTest(fields=list(state)):
                account_export._save(self.root, state)
                before = (self.root / 'account-export.json').read_bytes()
                self.assertIsNone(self.tick())
                self.assertEqual((self.root / 'account-export.json').read_bytes(), before)
                self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_temporary_controller_failure_holds_safely_and_lock_contention_waits(self):
        account_export.configure(self.session, self.library)
        with patch('comic_archive.account_export._advance', side_effect=LibraryBusy('busy')):
            self.assertIsNone(self.tick())
        self.assertEqual(account_export.status(self.session, self.library)['status'], 'running')
        with patch('comic_archive.account_export._advance', side_effect=OSError('PRIVATE')):
            self.assertIsNone(self.tick())
        state = account_export.status(self.session, self.library)
        self.assertEqual(state['code'], 'controller_failed')
        self.assertNotIn('PRIVATE', json.dumps(state))
        self.assertEqual(list((self.root / 'requests').glob('*.json')), [])

    def test_discovery_renewal_preserves_prior_usage_and_recovery_limits(self):
        completion.configure(self.session, self.library, until_drained=True)
        for _ in range(3):
            completion.tick(self.session, self.library)
        window = batches._load(self.root)
        # Resolve the fixture request as a successfully drained backlog.
        window.update(status='known_backlog_drained', pending=None,
                      downloaded_pages=12, downloaded_bytes=42,
                      network_recovery_attempts=2, source_recovery_attempts=1)
        batches._save(self.root, window)
        completion.tick(self.session, self.library)
        before = completion.status(self.session, self.library)
        result = completion.continue_discovered(self.session, self.library)
        self.assertEqual(result['downloaded_pages'], before['downloaded_pages'])
        self.assertEqual(result['network_recovery_attempts'], 2)
        self.assertEqual(result['source_recovery_attempts'], 1)
        self.assertTrue(result['until_drained'])
        self.assertEqual(result['status'], 'running')
        with self.assertRaises(ArchiveError):
            completion.continue_discovered(self.session, self.library)


    def test_worker_in_a_project_without_an_export_does_nothing(self):
        self.assertIsNone(self.tick())
        self.assertFalse((self.root / 'account-export.json').exists())
        self.assertEqual(list((self.root / 'requests').iterdir()), [])

    def test_scan_that_made_progress_keeps_going_with_a_new_job_and_a_new_baseline(self):
        account_export.configure(self.session, self.library)
        self.lists()
        with Inventory(self.library) as inv:
            inv.observe('43', 'Other', [], 'subscribed')
        path, _ = self.request()
        self.assertEqual(account_export._load(self.root)['pending']['before_remaining'], 2)
        with Inventory(self.library) as inv:
            inv.record_series_scan('42')
        self.finish(path, {'status': 'limited'})
        value = account_export.status(self.session, self.library)
        self.assertEqual((value['status'], value['phase'], value['code'], value['pending_task_id']),
                         ('running', 'series', None, None))
        again, _ = self.request()
        self.assertNotEqual(again.stem, path.stem)
        self.assertEqual(account_export._load(self.root)['pending']['before_remaining'], 1)
        self.assertEqual(account_export.status(self.session, self.library)['status'], 'running')


if __name__ == '__main__':
    unittest.main()
