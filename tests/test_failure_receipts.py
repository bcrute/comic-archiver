import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import batches
from comic_archive.archive import atomic_json, verify
from comic_archive.errors import AuthenticationError
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.run_receipts import FailureReceipt, failure_result
from comic_archive.worker import run_worker
from test_jobs import AccountAdapter


class FailureReceiptTests(unittest.TestCase):
    @patch('builtins.print')
    def check_failure(self, failure, output):
        class Adapter(AccountAdapter):
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def capture_automatic(self, url):
                if url.endswith('/82'):
                    raise failure
                return super().capture_automatic(url)
        adapter = Adapter(); adapter.access = {'81': 'tapas_free', '82': 'tapas_free'}
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary); session = project / '.comic-session'; library = project / 'Library'
            root = session / 'worker'
            for name in ('requests', 'responses'): (root / name).mkdir(parents=True)
            atomic_json(root / 'status.json', {'state': 'ready'})
            with Inventory(library) as inventory:
                inventory.observe('42', 'Series', [
                    {'id': key, 'title': 'Episode', 'order': int(key) - 80, 'access_state': 'tapas_free'}
                    for key in ('81', '82')], 'fixture')
            batches._start(root, {'archive_only': True, 'refresh_lists': False, 'max_pages': 5,
                                 'batch_pages': 4, 'max_bytes': 1048576}, False)
            task = batches.step(session, library)['pending_task_id']
            batches.step(session, library)
            def archive(adapter, output, report, **options):
                return archive_discovered(adapter, library, **options)
            with patch('comic_archive.worker.TapasAdapter', return_value=adapter), \
                    patch('comic_archive.jobs.archive_discovered', side_effect=archive), \
                    patch('comic_archive.completion.worker_tick'), \
                    patch('comic_archive.worker.time.sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): run_worker(session)
            receipt = json.loads((root / 'responses' / (task + '.json')).read_text())
            self.assertEqual(receipt['status'], 'error')
            self.assertEqual(receipt['result']['downloaded_pages'], 2)
            self.assertEqual(receipt['result']['archived_episode_ids'], ['81'])
            self.assertNotIn('PRIVATE', json.dumps(receipt))
            if getattr(failure, 'source_http_status', None) == 403:
                self.assertEqual(receipt['error_code'], 'source_refused')
                self.assertEqual(receipt['result']['pause_reason'], 'source_refused')
                self.assertEqual(receipt['result']['source_http_status'], 403)
                if getattr(failure,'source_response_kind',None)=='challenge':
                    self.assertEqual(receipt['result']['source_response_kind'],'challenge')
                    with Inventory(library,read_only=True) as inventory:
                        self.assertEqual(inventory.status()['last_run']['summary']['source_response_kind'],'challenge')
                    from comic_archive.monitoring import library_status
                    self.assertEqual(library_status(library)['progress']['source_response_kind'],'challenge')
            state = batches.step(session, library)
            self.assertEqual(state['status'], 'paused')
            self.assertEqual(state['code'], 'login_required' if isinstance(failure, AuthenticationError) else
                             'source_refused' if getattr(failure,'source_http_status',None)==403 else 'task_failed')
            self.assertEqual((state['downloaded_pages'], state['unreported_reserved_pages']), (2, 0))
            self.assertEqual(state['downloaded_bytes'], receipt['result']['downloaded_bytes'])
            self.assertEqual(state['unreported_reserved_bytes'], 0)
            self.assertEqual((state['completed_batches'], state['archived_episodes']), (1, 1))
            self.assertIsNone(batches.step(session, library)['pending_task_id'])
            self.assertEqual(batches.step(session, library)['downloaded_pages'], 2)
            self.assertEqual(list((root / 'requests').glob('*.json')), [])
            self.assertEqual(verify(library), [])

    def test_authentication_failure_after_commit_keeps_exact_task_usage_and_holds(self):
        self.check_failure(AuthenticationError('Tapas requires a fresh login.'))

    def test_unexpected_failure_after_commit_retains_usage_without_raw_exception_data(self):
        self.check_failure(ValueError('PRIVATE_COOKIE_AND_SIGNED_URL'))

    def test_source_refusal_after_commit_keeps_exact_usage_and_blocks_successors(self):
        from comic_archive.models import ArchiveError
        refusal = ArchiveError('Tapas refused this reader request.')
        refusal.source_http_status = 403
        self.check_failure(refusal)

    def test_challenge_after_commit_reaches_task_receipt_and_monitor_without_submitting_successors(self):
        from comic_archive.models import ArchiveError
        refusal=ArchiveError('Tapas returned a browser challenge.')
        refusal.source_http_status=403
        refusal.source_response_kind='challenge'
        refusal.response_body='PRIVATE_COOKIE'
        self.check_failure(refusal)

    def test_invalid_response_classification_does_not_discard_exact_failure_usage(self):
        error=RuntimeError('PRIVATE')
        for kind in ('PRIVATE_COOKIE', True, {}, []):
            error._comic_failure_receipt=FailureReceipt('a'*24,'paused',2,3,4,('81',),0,
                                                       'source_refused',403,None,kind)
            result=failure_result(error)
            self.assertEqual(result['downloaded_pages'],2)
            self.assertNotIn('source_response_kind',result)
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_failure_receipt_rejects_untyped_or_malformed_error_data(self):
        error = RuntimeError('PRIVATE')
        error._comic_failure_receipt = {'downloaded_pages': 0}
        self.assertIsNone(failure_result(error))
        for receipt in (FailureReceipt('a' * 24, 'paused', True, 1, 1, ('81',), 0),
                        FailureReceipt('a' * 24, 'paused', 1, 1, 1, ('81', '81'), 0),
                        FailureReceipt('PRIVATE', 'paused', 1, 1, 1, ('81',), 0)):
            error._comic_failure_receipt = receipt
            self.assertIsNone(failure_result(error))

    def test_over_budget_error_result_keeps_full_allocation_and_does_not_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary); session = project / '.comic-session'; library = project / 'Library'
            root = session / 'worker'
            for name in ('requests', 'responses'): (root / name).mkdir(parents=True)
            atomic_json(root / 'status.json', {'state': 'ready'})
            batches._start(root, {'archive_only': True, 'refresh_lists': False, 'max_pages': 5,
                                 'batch_pages': 4, 'max_bytes': 1048576}, False)
            task = batches.step(session, library)['pending_task_id']; batches.step(session, library)
            atomic_json(root / 'responses' / (task + '.json'), {'task_id': task, 'status': 'error',
                'error_code': 'job_failed', 'result': {'run_id': 'a' * 24, 'status': 'paused',
                    'downloaded_pages': 5, 'downloaded_bytes': 1, 'pending_episode_count': 1,
                    'archived_episode_ids': ['81'], 'inventory': {'scan_remaining_series_count': 0}}})
            (root / 'requests' / (task + '.json')).unlink()
            state = batches.step(session, library)
            self.assertEqual(state['code'], 'task_failed')
            self.assertEqual((state['downloaded_pages'], state['unreported_reserved_pages']), (0, 4))
            self.assertEqual(state['unreported_reserved_bytes'], 1048576)
            self.assertIsNone(batches.step(session, library)['pending_task_id'])

    def test_source_refusal_receipt_is_kept_only_for_a_paused_run_with_a_refusal_status(self):
        def receipt(status='paused',http=403,retry_after=None):
            return FailureReceipt('a'*24,status,2,3,4,('81',),0,'source_refused',http,retry_after)
        error=RuntimeError('PRIVATE')
        for name,candidate in (('interrupted run',receipt('interrupted')),('server error',receipt(http=500)),
                               ('boolean status',receipt(http=True)),('no status',receipt(http=None))):
            with self.subTest(name):
                error._comic_failure_receipt=candidate
                self.assertIsNone(failure_result(error))
        error._comic_failure_receipt=receipt(http=429)
        result=failure_result(error)
        self.assertEqual((result['pause_reason'],result['source_http_status']),('source_refused',429))
        self.assertEqual((result['downloaded_pages'],result['archived_episode_ids']),(2,['81']))
        self.assertNotIn('retry_after_seconds',result)

    def test_retry_delay_is_reported_only_when_it_is_a_sane_whole_number_of_seconds(self):
        from comic_archive.retry_after import MAX_RETRY_AFTER
        error=RuntimeError('PRIVATE')
        for delay in (0,30,MAX_RETRY_AFTER):
            with self.subTest(delay=delay):
                error._comic_failure_receipt=FailureReceipt('a'*24,'paused',2,3,4,('81',),0,'source_refused',429,delay)
                self.assertEqual(failure_result(error)['retry_after_seconds'],delay)
        for delay in (-1,MAX_RETRY_AFTER+1,True,'30',30.5,None):
            with self.subTest(delay=delay):
                error._comic_failure_receipt=FailureReceipt('a'*24,'paused',2,3,4,('81',),0,'source_refused',429,delay)
                self.assertNotIn('retry_after_seconds',failure_result(error))
