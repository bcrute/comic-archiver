import json
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from comic_archive.archive import Store, verify
from comic_archive.downloads import AssetDownload
from comic_archive.errors import AccessDenied, RunLimit
from comic_archive.jobs import DownloadBudget
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.transport import ExpiredAsset
from test_archive import png


class Assets:
    def __init__(self):
        self.owner = threading.get_ident()
        self.lock = threading.Lock()
        self.active = self.maximum = 0
        self.calls, self.finishes, self.allowances = [], [], []
        self.failures = {}
        self.overlap = False
        self.second_started = threading.Event()
        self.pair_started = threading.Barrier(2)
        self.refresh_calls = 0
        self.revoke = False

    def assert_owner(self):
        if threading.get_ident() != self.owner:
            raise AssertionError('Browser, accounting or cache operation ran outside its owner thread')

    def check(self):
        self.assert_owner()

    def asset_request(self, page, max_bytes=None):
        self.assert_owner()
        self.allowances.append(max_bytes)
        return AssetDownload(partial(self.fetch, page, max_bytes),
                             lambda data: self.assert_owner(), self.assert_owner)

    def retrieve(self, page, max_bytes=None):
        self.assert_owner()
        return self.fetch(page, max_bytes)

    def fetch(self, page, max_bytes):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append(page.order)
        try:
            if self.overlap and page.order in (1, 2):
                self.pair_started.wait(3)
            if self.overlap and page.order == 1:
                if not self.second_started.wait(3):
                    raise AssertionError('Second transfer did not overlap the first')
            failure = self.failures.pop(page.order, None)
            if failure:
                raise failure
            data = png(page.order)
            if max_bytes is not None and len(data) > max_bytes:
                raise RunLimit('Page exceeds reserved bytes')
            return data
        finally:
            with self.lock:
                self.active -= 1
                self.finishes.append(page.order)
                if page.order == 2:
                    self.second_started.set()

    def refresh(self, episode):
        self.assert_owner()
        if self.active:
            raise AssertionError('Metadata refreshed while an asset was still in flight')
        self.refresh_calls += 1
        if self.revoke:
            raise AccessDenied('Authorization was revoked')
        return replace(episode, pages=tuple(replace(p, location=p.location + '?signature=refreshed')
                                             for p in episode.pages))


class DownloadWorkersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Library'
        self.store = Store(self.root, min_free_bytes=0)
        self.addCleanup(self.store.close)
        self.adapter = Assets()
        self.episode = Episode('81', '42', 'Episode', 1,
                               tuple(Page(i, f'https://cdn.tapas.io/pc/page-{i}.png', False)
                                     for i in range(1, 5)), 'tapas_unlocked', 'https://tapas.io/episode/81')
        self.manifest = Manifest(Series('42', 'Series'), (self.episode,))

    def archive(self, adapter=None, **options):
        return self.store.archive(self.manifest, adapter or self.adapter,
                                  progress=lambda line: None, download_workers=2, **options)

    def test_overlapping_transfers_produce_identical_ordered_cbz_and_cache_reuse(self):
        self.adapter.overlap = True
        folder = self.archive()
        cbz = next(folder.glob('*.cbz'))
        self.assertEqual(self.adapter.maximum, 2)
        self.assertEqual(verify(self.root), [])
        with zipfile.ZipFile(cbz) as archive:
            self.assertEqual(archive.namelist(), [f'{i:04d}.png' for i in range(1, 5)])
            self.assertEqual([archive.read(name) for name in archive.namelist()],
                             [png(i) for i in range(1, 5)])
        original = cbz.read_bytes()
        sidecar = cbz.with_suffix('.json').read_bytes()
        calls = list(self.adapter.calls)
        self.archive()
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(cbz.read_bytes(), original)
        self.assertEqual(cbz.with_suffix('.json').read_bytes(), sidecar)
        # A serial build has byte-for-byte identical deterministic ZIP entries.
        self.store.archive(self.manifest, self.adapter, progress=lambda line: None)
        self.assertEqual(cbz.read_bytes(), original)

    def test_failure_caches_other_inflight_page_and_does_not_start_next_wave(self):
        self.adapter.failures[1] = ArchiveError('Transfer failed')
        with self.assertRaises(ArchiveError):
            self.archive()
        self.assertEqual(set(self.adapter.calls), {1, 2})
        self.assertEqual(self.adapter.active, 0)
        self.assertIsNone(self.store.cached_page('tapas', self.episode, self.episode.pages[0]))
        self.assertEqual(self.store.cached_page('tapas', self.episode, self.episode.pages[1]), png(2))
        self.assertFalse(list(self.root.rglob('*.cbz')))
        self.archive()
        self.assertEqual(self.adapter.calls.count(2), 1)
        self.assertEqual(verify(self.root), [])

    def test_free_slot_is_refilled_before_slow_companion_finishes(self):
        third_started = threading.Event()
        original = self.adapter.fetch
        def fetch(page, allowance):
            if page.order == 1 and not third_started.wait(3):
                raise AssertionError('A free slot waited for its slow companion')
            if page.order == 3:
                third_started.set()
            return original(page, allowance)
        self.adapter.fetch = fetch
        self.archive()
        self.assertTrue(third_started.is_set())
        self.assertEqual(verify(self.root), [])

    def test_expired_url_drains_wave_before_one_owner_thread_refresh(self):
        self.adapter.failures[1] = ExpiredAsset('Expired')
        self.archive()
        self.assertEqual(self.adapter.refresh_calls, 1)
        self.assertEqual(self.adapter.calls.count(2), 1)
        self.assertEqual(verify(self.root), [])

    def test_revoked_access_stops_without_new_asset_requests(self):
        self.adapter.failures[1] = ExpiredAsset('Expired')
        self.adapter.revoke = True
        with self.assertRaises(AccessDenied):
            self.archive()
        self.assertEqual(set(self.adapter.calls), {1, 2})
        self.assertFalse(list(self.root.rglob('*.cbz')))

    def test_companion_refusal_prevents_expiry_refresh_and_new_transfers(self):
        self.adapter.failures = {1: ExpiredAsset('Expired'), 2: ArchiveError('Rate limit')}
        with self.assertRaisesRegex(ArchiveError, 'Rate limit'):
            self.archive()
        self.assertEqual(self.adapter.refresh_calls, 0)
        self.assertEqual(set(self.adapter.calls), {1, 2})

    def test_second_expiry_stops_after_one_refresh_and_retains_other_successes(self):
        self.adapter.failures = {1: ExpiredAsset('Expired'), 3: ExpiredAsset('Expired again')}
        with self.assertRaisesRegex(ArchiveError, 'Refreshed reader assets still failed'):
            self.archive()
        self.assertEqual(self.adapter.refresh_calls, 1)
        self.assertEqual(self.adapter.active, 0)
        self.assertEqual(self.store.cached_page('tapas', self.episode, self.episode.pages[0]), png(1))
        # A free slot may start page 4 before page 3's failure is observed.
        # All such in-flight work must be drained, with no incomplete CBZ.
        self.assertLessEqual(self.adapter.calls.count(4), 1)
        self.assertFalse(list(self.root.rglob('*.cbz')))

    def test_parallel_page_budget_is_reserved_and_other_success_is_counted_on_failure(self):
        budget = DownloadBudget(self.adapter, max_pages=2)
        self.adapter.failures[1] = ArchiveError('Transfer failed')
        with self.assertRaises(ArchiveError):
            self.archive(budget)
        self.assertEqual(budget.pages, 1)
        self.assertEqual(budget.bytes, len(png(2)))
        self.assertEqual((budget.reserved_pages, budget.reserved_bytes), (0, 0))
        self.adapter.calls.clear()
        budget = DownloadBudget(self.adapter, max_pages=1)
        with self.assertRaises(RunLimit):
            self.archive(budget)
        self.assertEqual(budget.pages, 1)
        self.assertEqual(self.adapter.calls, [1])
        self.assertEqual((budget.reserved_pages, budget.reserved_bytes), (0, 0))

    def test_aggregate_byte_budget_falls_back_to_one_transfer_near_limit(self):
        limit = len(png(1)) * 2 + 1
        budget = DownloadBudget(self.adapter, max_bytes=limit)
        with self.assertRaises(RunLimit):
            self.archive(budget)
        self.assertEqual(budget.pages, 2)
        self.assertLessEqual(budget.bytes, limit)
        self.assertEqual(self.adapter.maximum, 1)
        self.assertEqual(self.adapter.allowances, [limit, limit - len(png(1)), 1])
        self.assertEqual((budget.reserved_pages, budget.reserved_bytes), (0, 0))

    def test_cancellation_drains_and_caches_both_transfers_before_stopping(self):
        budget = DownloadBudget(self.adapter)
        from comic_archive.cancellation import RunCancelled
        class Cancellation:
            def check(inner):
                self.adapter.assert_owner()
                if budget.pages:
                    raise RunCancelled('Stopped')
        budget.cancellation = Cancellation()
        with self.assertRaises(RunCancelled):
            self.archive(budget)
        self.assertEqual(set(self.adapter.calls), {1, 2})
        self.assertEqual(budget.pages, 2)
        self.assertEqual(self.adapter.active, 0)
        self.assertEqual((budget.reserved_pages, budget.reserved_bytes), (0, 0))
        self.assertFalse(list(self.root.rglob('*.cbz')))

    def test_unknown_adapter_is_sequential_and_worker_limits_reject_before_download(self):
        self.adapter.asset_request = None
        self.archive()
        self.assertEqual(self.adapter.maximum, 1)
        for invalid in (0, 5, True, 2.0, '2'):
            with self.subTest(workers=invalid), self.assertRaises(ArchiveError):
                self.store.archive(self.manifest, self.adapter, download_workers=invalid)

    def test_changed_manifest_on_refresh_is_rejected_before_more_transfers(self):
        self.adapter.failures[1] = ExpiredAsset('Expired')
        self.adapter.refresh = lambda episode: replace(episode, pages=tuple(
            replace(p, location='https://cdn.tapas.io/pc/changed.png') if p.order == 1 else p
            for p in episode.pages))
        with self.assertRaisesRegex(ArchiveError, 'manifest changed'):
            self.archive()
        self.assertEqual(set(self.adapter.calls), {1, 2})

    def test_safe_asset_factory_bypasses_browser_and_keeps_signed_urls_private(self):
        from comic_archive.adapters.tapas_http import TapasHTTPAdapter
        from comic_archive.transport import Transport
        from test_transport_session import Client, Response
        assets = Transport(client=Client([Response(data=png(1))]))
        class Browser:
            def __init__(inner):
                inner.assets = assets
            def get(inner, *args, **kwargs):
                raise AssertionError('Browser was handed to an asset thread')
        adapter = TapasHTTPAdapter(transport=Browser())
        page = replace(self.episode.pages[0], location=self.episode.pages[0].location + '?signature=PRIVATE')
        request = adapter.asset_request(page)
        self.assertNotIn('PRIVATE', repr(request))
        with patch('comic_archive.transport.time.sleep'):
            self.assertEqual(request.fetch(), png(1))
        self.assertIsNone(adapter.asset_request(replace(page, location='https://tapas.io/episode/81')))

    def test_queue_and_cli_accept_two_workers_and_reject_noninteger_limits(self):
        from comic_archive.cli import parser
        from comic_archive.queue_client import submit_job
        session = Path(self.temp.name) / 'session'
        (session / 'worker/requests').mkdir(parents=True)
        result = submit_job(session, 'archive_discovered', {'download_workers': 2})
        request = json.loads((session / 'worker/requests' / (result['task_id'] + '.json')).read_text())
        self.assertEqual(request['args']['download_workers'], 2)
        for invalid in (0, 5, True, 2.0, '2'):
            with self.subTest(workers=invalid), self.assertRaises(ArchiveError):
                submit_job(session, 'archive_discovered', {'download_workers': invalid})
        for command in ('archive-discovered', 'sync'):
            self.assertEqual(parser().parse_args([command, '--download-workers', '2']).download_workers, 2)
        self.assertEqual(parser().parse_args(['archive-episode', 'https://tapas.io/episode/81',
                                             '--download-workers', '2']).download_workers, 2)

    @patch('builtins.print')
    def test_two_worker_job_rechecks_queued_unlock_before_any_asset_submission(self, output):
        from comic_archive.inventory import Inventory
        from comic_archive.jobs import archive_discovered
        from test_jobs import AccountAdapter
        adapter = AccountAdapter()
        adapter.denied = {'82'}
        def forbidden(page, **kwargs):
            raise AssertionError('No asset may be scheduled after denied authorization')
        adapter.asset_request = forbidden
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Series', [{'id':'82','title':'E82','order':2,
                                               'access_state':'tapas_unlocked'}], 'fixture')
        result = archive_discovered(adapter, self.root, limit=1, download_workers=2)
        self.assertEqual(result['unavailable_episode_ids'], ['82'])
        self.assertEqual(result['download_workers'], 2)
        self.assertEqual(adapter.downloads, [])

    def test_unknown_profiles_and_out_of_range_limits_are_rejected(self):
        from comic_archive.downloads import adapter_profile, validate_download_workers, validate_request_interval
        for profile in ('turbo', None, True, 'FAST'):
            with self.subTest(profile=profile):
                with self.assertRaises(ArchiveError):
                    validate_download_workers(2, profile)
                with self.assertRaises(ArchiveError):
                    validate_request_interval(1.0, profile)
        self.assertEqual(validate_download_workers(64, 'fast'), 64)
        self.assertEqual(validate_request_interval(1 / 128, 'fast'), 1 / 128)
        for workers, profile in ((65, 'fast'), (0, 'fast'), (5, 'normal')):
            with self.subTest(workers=workers, profile=profile), self.assertRaises(ArchiveError):
                validate_download_workers(workers, profile)
        for interval, profile in ((1 / 200, 'fast'), (0.2, 'normal'), (61, 'fast'), (float('inf'), 'fast')):
            with self.subTest(interval=interval, profile=profile), self.assertRaises(ArchiveError):
                validate_request_interval(interval, profile)
        class Declared:
            pass
        for declared, expected in (('fast', 'fast'), ('normal', 'normal'), ('FAST', 'normal'), (True, 'normal'), (None, 'normal')):
            adapter = Declared()
            adapter.download_profile = declared
            self.assertEqual(adapter_profile(adapter), expected)
        self.assertEqual(adapter_profile(object()), 'normal')


    def test_adapter_that_declines_a_page_request_still_gets_it_saved_through_normal_retrieval(self):
        self.adapter.asset_request = lambda page, max_bytes=None: None
        budget = DownloadBudget(self.adapter)
        self.assertIsNone(budget.asset_request(self.episode.pages[0]))
        self.assertEqual((budget.reserved_pages, budget.reserved_bytes), (0, 0))
        self.archive(budget)
        self.assertEqual(self.adapter.calls, [1, 2, 3, 4])
        self.assertEqual((budget.pages, budget.reserved_pages, budget.reserved_bytes), (4, 0, 0))
        self.assertEqual(verify(self.root), [])

    def test_budget_refreshes_expired_addresses_only_while_its_time_allows_the_job_to_continue(self):
        clock = [100.0]
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            budget = DownloadBudget(self.adapter, max_seconds=10)
            clock[0] = 105.0
            refreshed = budget.refresh(self.episode)
            self.assertEqual(self.adapter.refresh_calls, 1)
            self.assertTrue(all(page.location.endswith('?signature=refreshed') for page in refreshed.pages))
            clock[0] = 110.0
            with self.assertRaises(RunLimit):
                budget.refresh(self.episode)
        self.assertEqual(self.adapter.refresh_calls, 1)
