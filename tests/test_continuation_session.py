"""Real job/cache boundaries with one owned browser and injected source faults."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx

from comic_archive.archive import verify
from comic_archive.cancellation import RunCancelled
from comic_archive.errors import AuthenticationError, RunLimit
from comic_archive.fast_import import FastWorkerSession
from comic_archive.inventory import Inventory
from comic_archive.jobs import DownloadBudget, archive_discovered
from comic_archive.models import ArchiveError, Page
from comic_archive.run_receipts import failure_result
from comic_archive.transport import Transport
from test_archive import png
from test_jobs import AccountAdapter


class ContinuationSessionTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / 'Library'
        self.session = Path(self.work.name) / '.comic-session'
        self.session.mkdir()
        (self.session / 'state.json').write_text('{"fixture":1}')
        self.created, self.requests, self.captures, self.identities = [], [], [], []
        self.timeouts, self.refused, self.server_waits = {}, set(), {}
        self.identity = '11'
        case = self

        class Browser(AccountAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.owner = threading.get_ident()
                self.access = {key: 'tapas_free' for key in ('81', '82', '83')}
                self.closed = False
                case.created.append(self)
                self.transport = SimpleNamespace(assets=Transport(client=httpx.Client(
                    transport=httpx.MockTransport(case.reply)), interval=1/128, profile='fast'))

            def owned(self):
                if self.owner != threading.get_ident():
                    raise AssertionError('Browser ownership changed')

            def __enter__(self):
                self.owned()
                return self

            def __exit__(self, *args):
                self.owned()
                self.closed = True
                self.transport.assets.close()

            def account_identity(self):
                self.owned()
                case.identities.append(case.identity)
                return case.identity

            def capture_automatic(self, url):
                self.owned()
                if self.account_identity() != self.expected_account_id:
                    raise AuthenticationError('Account changed before the reader')
                key = url.rsplit('/', 1)[1]
                case.captures.append((key, self.owner))
                if key in case.refused:
                    error = ArchiveError('Reader refused; private response omitted')
                    error.source_http_status = 403
                    raise error
                manifest = super().capture_automatic(url)
                episode = replace(manifest.episodes[0], pages=tuple(
                    Page(n, f'https://us-a.tapas.io/pc/{key}/{n}.png', False) for n in (1, 2)))
                return replace(manifest, episodes=(episode,))

        self.patch_browser = patch('comic_archive.fast_import.FastBrowserAdapter', Browser)
        self.patch_browser.start()
        self.addCleanup(self.patch_browser.stop)
        for method in ('_pace', '_wait'):
            mock = patch.object(Transport, method)
            mock.start()
            self.addCleanup(mock.stop)
        output = patch('builtins.print')
        output.start()
        self.addCleanup(output.stop)
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [
                {'id': key, 'title': 'E', 'order': int(key)-80, 'access_state': 'tapas_free'}
                for key in ('81', '82', '83')], 'fixture')

    def reply(self, request):
        path = request.url.path
        self.requests.append(path)
        if self.timeouts.get(path, 0):
            self.timeouts[path] -= 1
            raise httpx.ReadTimeout('PRIVATE_SIGNED_URL')
        if path in self.server_waits:
            return httpx.Response(503, headers={'Retry-After': str(self.server_waits[path])})
        return httpx.Response(200, content=png(int(path.rsplit('/', 1)[1].split('.')[0])))

    def job(self, pool, **options):
        with pool.batch() as adapter:
            adapter.supports_series_metadata = False
            return archive_discovered(adapter, self.root, max_bytes=8*1024**3,
                                      download_workers=64, **options)

    def test_partial_pages_cross_repeated_batches_with_fresh_access_and_exact_metrics(self):
        with FastWorkerSession(self.session) as pool:
            results = [self.job(pool, max_pages=1) for _ in range(4)]
            self.assertEqual([r['downloaded_pages'] for r in results], [1, 1, 1, 1])
            self.assertEqual([r['asset_metrics']['requests'] for r in results], [1, 1, 1, 1])
            self.assertEqual([r['archived_episode_ids'] for r in results], [[], ['81'], [], ['82']])
            # A candidate captured after the final page consumes no assets;
            # the following batch must capture/access-check it again.
            self.assertEqual([key for key, owner in self.captures], ['81', '81', '82', '82', '82', '83'])
            self.assertEqual(len(set(owner for key, owner in self.captures)), 1)
            self.assertEqual(len(self.created), 1)
            self.assertGreaterEqual(len(self.identities), 8)
        self.assertTrue(self.created[0].closed)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(verify(self.root), [])

    def test_three_read_timeouts_defer_one_episode_then_resume_only_missing_page(self):
        self.timeouts['/pc/81/2.png'] = 3
        with FastWorkerSession(self.session) as pool:
            first = self.job(pool, max_pages=6)
            self.assertEqual(first['deferred_asset_episode_ids'], ['81'])
            self.assertEqual(first['archived_episode_ids'], ['82', '83'])
            self.assertEqual(first['downloaded_pages'], 5)
            self.assertEqual(first['asset_metrics']['error_counts'], {'read_timeout': 3})
            cooling = self.job(pool, max_pages=6)
            self.assertEqual(cooling['downloaded_pages'],0)
            self.assertEqual(cooling['pending_episode_count'],1)
            self.assertGreater(cooling['asset_retry_after_seconds'],0)
            pool.close()
            with patch('comic_archive.inventory.timestamp',return_value=(datetime.now(timezone.utc)+timedelta(seconds=301)).isoformat()):
                second = self.job(pool, max_pages=6)
            self.assertEqual(second['archived_episode_ids'], ['81'])
            self.assertEqual(second['downloaded_pages'], 1)
            self.assertEqual(second['asset_metrics']['requests'], 1)
            self.assertEqual(second['asset_metrics']['error_counts'], {})
            self.assertEqual(len(self.created), 2)
        self.assertEqual(verify(self.root), [])

    def test_source_refusal_closes_context_and_keeps_prior_complete_exports(self):
        with FastWorkerSession(self.session) as pool:
            first = self.job(pool, max_pages=2, limit=1)
            self.refused.add('82')
            before = len(self.requests)
            with self.assertRaises(ArchiveError) as caught:
                self.job(pool, max_pages=2, limit=1)
            result = failure_result(caught.exception)
            self.assertEqual(result['source_http_status'], 403)
            self.assertEqual(result['downloaded_pages'], 0)
            self.assertEqual(len(self.requests), before)
            self.assertIsNone(pool.adapter)
            self.assertTrue(self.created[0].closed)
            self.assertEqual(first['archived_episode_ids'], ['81'])
        self.assertEqual(verify(self.root), [])

    def test_account_switch_in_reused_browser_stops_before_capture_or_assets(self):
        with FastWorkerSession(self.session) as pool:
            self.job(pool, max_pages=1)
            before = len(self.requests), len(self.captures)
            self.identity = '22'
            with self.assertRaises(AuthenticationError):
                self.job(pool, max_pages=1)
            self.assertEqual((len(self.requests), len(self.captures)), before)
            self.assertIsNone(pool.adapter)

    def test_external_session_import_invalidates_idle_browser_and_keeps_cache(self):
        with FastWorkerSession(self.session) as pool:
            self.job(pool, max_pages=1)
            (self.session / 'state.json').write_text('{"fixture":2,"new_session":true}')
            result = self.job(pool, max_pages=1)
            self.assertTrue(self.created[0].closed)
            self.assertEqual(len(self.created), 2)
            self.assertEqual(result['downloaded_pages'], 1)
            self.assertEqual(result['archived_episode_ids'], ['81'])
        self.assertEqual(verify(self.root), [])

    def test_worker_restart_rechecks_access_and_reuses_only_verified_cached_pages(self):
        with FastWorkerSession(self.session) as pool:
            first = self.job(pool, max_pages=1)
        with FastWorkerSession(self.session) as restarted:
            second = self.job(restarted, max_pages=1)
        self.assertEqual((first['downloaded_pages'], second['downloaded_pages']), (1, 1))
        self.assertEqual(second['archived_episode_ids'], ['81'])
        self.assertEqual([key for key, owner in self.captures], ['81', '81', '82'])
        self.assertEqual(len(self.created), 2)
        self.assertEqual(verify(self.root), [])

    def test_same_size_and_timestamp_session_change_is_adopted_before_new_assets(self):
        state = self.session / 'state.json'
        with FastWorkerSession(self.session) as pool:
            self.job(pool, max_pages=1)
            stat = state.stat()
            state.write_text('{"fixture":2}')
            os.utime(state, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.job(pool, max_pages=1)
            self.assertTrue(self.created[0].closed)
            self.assertEqual(len(self.created), 2)

    def test_missing_session_closes_idle_browser_without_using_old_credentials(self):
        with FastWorkerSession(self.session) as pool:
            self.job(pool, max_pages=1)
            (self.session / 'state.json').unlink()
            before = len(self.requests)
            with self.assertRaises(ArchiveError):
                self.job(pool, max_pages=1)
            self.assertIsNone(pool.adapter)
            self.assertTrue(self.created[0].closed)
            self.assertEqual(len(self.requests), before)

    def test_server_wait_is_retained_and_unhealthy_context_is_closed(self):
        self.server_waits['/pc/81/1.png'] = 900
        with FastWorkerSession(self.session) as pool:
            result = self.job(pool, max_pages=2, limit=1)
            self.assertEqual(result['status'], 'paused')
            self.assertEqual(result['retry_after_seconds'], 900)
            self.assertEqual(result['source_http_status'], 503)
            self.assertIsNone(pool.adapter)

    def test_cancelled_job_closes_context_without_inheriting_cancellation_on_repair(self):
        check = DownloadBudget.check
        def stopped(budget):
            if budget.pages:
                raise RunCancelled('Stopped')
            return check(budget)
        with FastWorkerSession(self.session) as pool:
            with patch.object(DownloadBudget, 'check', stopped):
                result = self.job(pool, max_pages=1)
            self.assertEqual(result['status'], 'cancelled')
            self.assertIsNone(pool.adapter)
            repaired = self.job(pool, max_pages=2, limit=1)
            self.assertEqual(repaired['archived_episode_ids'], ['81'])
            self.assertEqual(repaired['downloaded_pages'], 1)
        self.assertEqual(verify(self.root), [])

    def test_late_prepared_refusal_after_time_limit_retains_completed_archive_receipt(self):
        ready = threading.Event()
        pool = FastWorkerSession(self.session)
        self.addCleanup(pool.close)
        with pool.batch() as adapter:
            adapter.supports_series_metadata = False
            capture = adapter._inner.capture_automatic
            def late(url):
                if url.endswith('/82'):
                    if not ready.wait(3):
                        raise AssertionError('Batch boundary never arrived')
                    self.refused.add('82')
                return capture(url)
            adapter._inner.capture_automatic = late
            check = DownloadBudget.check
            def expired(budget):
                if budget.pages == 2 and list(self.root.glob('*/*.cbz')):
                    ready.set()
                    raise RunLimit('Time budget ended after the completed export')
                return check(budget)
            with patch.object(DownloadBudget, 'check', expired):
                with self.assertRaises(ArchiveError) as caught:
                    archive_discovered(adapter, self.root, max_pages=4,
                                       max_bytes=8*1024**3, download_workers=64)
            receipt = failure_result(caught.exception)
            self.assertEqual(receipt['source_http_status'], 403)
            self.assertEqual(receipt['downloaded_pages'], 2)
            self.assertEqual(receipt['archived_episode_ids'], ['81'])
            with Inventory(self.root, read_only=True) as inventory:
                summary = inventory.status()['last_run']['summary']
                self.assertEqual(summary['status'], 'paused')
                self.assertEqual(summary['pause_reason'], 'source_refused')
            self.assertEqual(verify(self.root), [])

    def test_worker_dispatch_uses_resident_pool_and_retains_fixed_caps(self):
        from comic_archive.worker import execute
        worker = SimpleNamespace(session=self.session, preferred_browser='firefox', headless=True)
        def operation(adapter, name, options):
            self.assertEqual(name, 'archive_discovered')
            self.assertEqual(options['download_workers'], 64)
            adapter.supports_series_metadata = False
            return archive_discovered(adapter, self.root, **options)
        request = {'operation': 'archive_discovered',
                   'args': {'speed': 'fast', 'max_pages': 1, 'max_bytes': 8*1024**3, 'max_seconds': 60}}
        with FastWorkerSession(self.session) as pool, patch('comic_archive.worker._archive_operation', operation):
            first = execute(worker, request, fast_session=pool)
            second = execute(worker, request, fast_session=pool)
        self.assertEqual((first['downloaded_pages'], second['downloaded_pages']), (1, 1))
        self.assertEqual(second['archived_episode_ids'], ['81'])
        self.assertEqual(len(self.created), 1)

    def test_real_worker_queue_reuses_browser_and_drains_after_two_bounded_jobs(self):
        from comic_archive import background, worker
        requests = self.session / 'worker/requests'
        requests.mkdir(parents=True)
        names = ['job-' + f'{n:024x}' for n in (1, 2)]
        request = {'operation':'archive_discovered',
                   'args':{'speed':'fast','max_pages':1,'max_bytes':8*1024**3,'max_seconds':60}}
        for name in names:
            worker.private_json(requests / (name + '.json'), request)
        calls = []
        def operation(adapter, name, options):
            self.assertEqual(name, 'archive_discovered')
            adapter.supports_series_metadata = False
            result = archive_discovered(adapter, self.root, **options)
            calls.append(result)
            if len(calls) == 2:
                self.assertTrue(background.stop(self.session)['stop_requested'])
            return result
        # Bound the loop even if a future dispatch failure prevents the stop. The worker keeps
        # serving its queue past an ordinary controller failure, so the bound is not an Exception.
        class Unbounded(BaseException):
            pass
        ticks = []
        def tick(*args):
            ticks.append(None)
            if len(ticks) > 3:
                raise Unbounded('The worker did not drain after its two jobs')
        ordinary = MagicMock()
        ordinary.interval = 1.0
        ordinary.__enter__.return_value = ordinary
        with patch.object(worker, 'TapasAdapter', return_value=ordinary), \
                patch.object(worker, '_archive_operation', operation), \
                patch('comic_archive.completion.worker_tick', tick), \
                patch('comic_archive.daily.start_scheduler'), \
                patch('comic_archive.daily.stop_scheduler'):
            worker.run_worker(self.session, headless=True, reload_adapter=False)
        receipts = [json.loads((self.session / 'worker/responses' / (name + '.json')).read_text())
                    for name in names]
        self.assertEqual([r['status'] for r in receipts], ['ok', 'ok'])
        self.assertEqual([r['result']['downloaded_pages'] for r in receipts], [1, 1])
        self.assertEqual(receipts[1]['result']['archived_episode_ids'], ['81'])
        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.created[0].closed)
        self.assertEqual(background.status(self.session)['running'], False)
        self.assertEqual(list(requests.glob('*.json')), [])
        self.assertEqual(verify(self.root), [])
