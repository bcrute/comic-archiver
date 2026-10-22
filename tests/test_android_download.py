from pathlib import Path
import tempfile
import unittest

from comic_archive import account_export, account_ui, android_download
from comic_archive.archive import atomic_json
from comic_archive.models import ArchiveError
from comic_archive.workspace import Workspace


class AndroidGoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace('default', Path(tmp.name))
        self.root = self.workspace.session / 'worker'
        (self.root / 'requests').mkdir(parents=True)
        (self.root / 'responses').mkdir()
        self.workspace.library.mkdir()

    def old_plan(self, *, queued=False):
        account_export.configure(self.workspace.session, self.workspace.library)
        account_export.worker_tick(self.workspace.session, self.workspace.library)
        account_export.worker_tick(self.workspace.session, self.workspace.library)
        value = account_export._load(self.root)
        value.update(status='stopped', category_index=2, list_checks=[
            {'category': c, 'complete': False, 'boundary_observed': False, 'observed_series_count': 0}
            for c in ['subscribed', 'updated']])
        task = value['pending']['task_id']
        if not queued:
            (self.root / 'requests' / (task + '.json')).unlink()
            atomic_json(self.root / 'responses' / (task + '.json'),
                        {'task_id': task, 'status': 'error', 'error_code': 'job_failed', 'result': {'status': 'paused'}})
        account_export._save(self.root, value)
        return task

    def test_go_uses_embedded_reader_pacing_instead_of_second_desktop_browser(self):
        android_download.start(self.workspace)
        self.assertEqual(account_ui.speed(self.workspace), 'medium')
        self.assertEqual(account_export._load(self.root)['window_budgets']['speed'], 'normal')

    def test_old_empty_mobile_scans_are_read_again_without_deleting_failed_receipt(self):
        task = self.old_plan()
        path = self.root / 'responses' / (task + '.json')
        receipt = path.read_bytes()
        android_download.start(self.workspace)
        value = account_export._load(self.root)
        self.assertEqual(value['category_index'], 0)
        self.assertEqual(value['list_checks'], [])
        self.assertIsNone(value['pending'])
        self.assertEqual(path.read_bytes(), receipt)
        self.assertEqual(value['status'], 'running')

    def test_upgrade_does_not_discard_a_live_pending_task(self):
        self.old_plan(queued=True)
        before = (self.root / 'account-export.json').read_bytes()
        with self.assertRaises(ArchiveError):
            android_download.start(self.workspace)
        self.assertEqual((self.root / 'account-export.json').read_bytes(), before)

    def test_later_go_resumes_checkpoint_without_rewinding_again(self):
        self.old_plan()
        android_download.start(self.workspace)
        value = account_export._load(self.root)
        value.update(status='stopped', category_index=1, list_checks=[
            {'category': 'subscribed', 'complete': False, 'boundary_observed': False, 'observed_series_count': 0}])
        account_export._save(self.root, value)
        android_download.start(self.workspace)
        self.assertEqual(account_export._load(self.root)['category_index'], 1)


class AndroidStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace('default', Path(tmp.name))
        root = self.workspace.session / 'worker'
        (root / 'requests').mkdir(parents=True)
        (root / 'responses').mkdir()
        self.workspace.library.mkdir()
        account_export.configure(self.workspace.session, self.workspace.library)
        self.progress_path = self.workspace.library / '.archive-state/progress.json'

    def seed(self):
        from comic_archive.inventory import Inventory
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('1001', 'Fixture Series', [
                {'id': '9001', 'title': 'First', 'order': 1, 'access_state': 'tapas_free'},
                {'id': '9002', 'title': 'Second', 'order': 2, 'access_state': 'tapas_free'}], 'subscribed')
            inventory.mark('9001', 'archived')
            self.run = inventory.start_run()
        self.write_progress()

    def write_progress(self, **values):
        atomic_json(self.progress_path, {'run_id': self.run, 'episode_id': '9002',
                    'status': 'running', 'completed': 1, 'downloaded_pages': 10,
                    'downloaded_bytes': 1048576, 'message': 'PRIVATE', 'url': 'PRIVATE', **values})

    def snapshot(self, *, busy=True):
        from unittest.mock import patch
        with patch('comic_archive.worker_host.status', return_value={
                'running': True, 'state': 'busy' if busy else 'ready'}):
            return android_download.status(self.workspace)

    def test_fresh_status_is_read_only_and_reports_missing_observations_as_missing(self):
        result = self.snapshot()
        self.assertEqual(result['counts'], {})
        self.assertEqual(result['progress'], {})
        self.assertFalse((self.workspace.library / '.archive-state/inventory.sqlite3').exists())

    def test_real_inventory_and_current_progress_are_projected_without_private_fields(self):
        self.seed()
        result = self.snapshot()
        self.assertEqual(result['counts'], {'saved_episodes': 1, 'observed_series': 1})
        self.assertEqual(result['progress']['phase'], 'archive')
        self.assertEqual(result['progress']['downloaded_pages'], 10)
        self.assertEqual(result['progress']['downloaded_bytes'], 1048576)
        self.assertGreaterEqual(result['progress']['age_seconds'], 0)
        for private in ['run_id', 'episode_id', 'source_slug', 'message', 'url']:
            self.assertNotIn(private, result['progress'])
        self.assertNotIn('PRIVATE', str(result))

    def test_new_progress_reaches_the_next_snapshot(self):
        self.seed()
        self.assertEqual(self.snapshot()['progress']['downloaded_pages'], 10)
        self.write_progress(downloaded_pages=14, downloaded_bytes=2097152)
        self.assertEqual(self.snapshot()['progress']['downloaded_pages'], 14)
        self.assertEqual(self.snapshot()['progress']['downloaded_bytes'], 2097152)

    def test_old_run_idle_worker_and_cancelled_progress_do_not_claim_live_activity(self):
        self.seed()
        self.assertEqual(self.snapshot(busy=False)['progress'], {})
        self.write_progress(run_id='previous-run')
        self.assertEqual(self.snapshot()['progress'], {})
        self.write_progress(status='cancelled')
        self.assertEqual(self.snapshot()['progress'], {})
        self.assertEqual(self.snapshot()['counts']['saved_episodes'], 1)

    def test_paused_plan_does_not_reuse_a_running_progress_file(self):
        self.seed()
        account_ui.pause(self.workspace)
        result = self.snapshot()
        self.assertEqual(result['account']['status'], 'stopped')
        self.assertEqual(result['progress'], {})

    def test_discovery_counts_are_live_and_invalid_counters_are_dropped(self):
        self.seed()
        self.write_progress(phase='account_list', reading_list_page_count=7,
                            observed_library_series_count=35, downloaded_pages=-1,
                            downloaded_bytes='PRIVATE', completed=True)
        result = self.snapshot()['progress']
        self.assertEqual(result['phase'], 'account_list')
        self.assertEqual(result['reading_list_page_count'], 7)
        self.assertEqual(result['observed_library_series_count'], 35)
        for invalid in ['downloaded_pages', 'downloaded_bytes', 'completed']:
            self.assertNotIn(invalid, result)
