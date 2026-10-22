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
