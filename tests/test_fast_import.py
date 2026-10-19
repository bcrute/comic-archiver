from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive.archive import Store, verify
from comic_archive.downloads import AssetDownload
from comic_archive.jobs import DownloadBudget
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from test_archive import png


class FastImportTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / 'Library'

    def test_four_asset_slots_preserve_order_and_byte_reservations(self):
        owner = threading.get_ident()
        barrier = threading.Barrier(4)
        class Assets:
            active = peak = 0
            lock = threading.Lock()
            def check(self):
                self.assert_owner()
            def assert_owner(self):
                if threading.get_ident() != owner:
                    raise AssertionError('Accounting left the coordinator.')
            def asset_request(self, page, max_bytes=None):
                self.assert_owner()
                def fetch():
                    with self.lock:
                        self.active += 1
                        self.peak = max(self.active, self.peak)
                    try:
                        barrier.wait(3)
                        return png(page.order)
                    finally:
                        with self.lock: self.active -= 1
                return AssetDownload(fetch, lambda data: self.assert_owner(), self.assert_owner)
        assets = Assets()
        budget = DownloadBudget(assets, max_pages=4, max_bytes=512*1024**2)
        series = Series('s', 'Series')
        chapter = Episode('81', 's', 'E', 1, tuple(Page(i, 'https://cdn.tapas.io/pc/' + str(i), False)
                          for i in range(1, 5)), 'tapas_free')
        store = Store(self.root)
        try:
            store.archive(Manifest(series, (chapter,)), budget, progress=lambda _: None, download_workers=4)
        finally:
            store.close()
        self.assertEqual(assets.peak, 4)
        self.assertEqual((budget.pages, budget.reserved_pages, budget.reserved_bytes), (4, 0, 0))
        self.assertEqual(verify(self.root), [])


class WorkerAdapter:
    """Stands in for the browser-owning adapter so only the pool's own bookkeeping is exercised."""
    instances = []

    def __init__(self, session, preferred_browser='firefox', headless=False):
        self.session, self.closed, self.reusable, self.fail_close = session, False, True, False
        self.exit_arguments = None
        self.batches = 0
        WorkerAdapter.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.closed = True
        self.exit_arguments = arguments
        if self.fail_close:
            raise RuntimeError('browser cleanup failed')

    def begin_batch(self):
        self.batches += 1


class FastWorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.session = Path(self.work.name) / '.comic-session'
        self.session.mkdir()
        self.state = self.session / 'state.json'
        self.state.write_text('{"fixture":1}')
        WorkerAdapter.instances = []
        patcher = patch('comic_archive.fast_import.FastImportAdapter', WorkerAdapter)
        patcher.start()
        self.addCleanup(patcher.stop)


    def test_failed_batch_reports_its_own_error_even_when_browser_cleanup_also_fails(self):
        from comic_archive.fast_import import FastWorkerSession
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails), FastWorkerSession(self.session) as pool:
                with self.assertRaises(ArchiveError) as raised:
                    with pool.batch() as adapter:
                        adapter.fail_close = cleanup_fails
                        raise ArchiveError('Reader refused')
                self.assertEqual(str(raised.exception), 'Reader refused')
                self.assertTrue(adapter.closed)
                self.assertIs(adapter.exit_arguments[1], raised.exception)
                self.assertIsNone(pool.adapter)

    def test_cleanup_failure_after_a_finished_batch_never_replaces_the_job_result(self):
        from comic_archive.fast_import import FastWorkerSession
        with FastWorkerSession(self.session) as pool:
            with pool.batch() as adapter:
                adapter.fail_close = True
                adapter.reusable = False
            self.assertTrue(adapter.closed)
            self.assertIsNone(pool.adapter)
            with pool.batch() as adapter:
                pass
            self.assertIsNone(adapter.exit_arguments)

    def test_session_that_disappears_or_becomes_a_link_during_a_batch_is_not_trusted_afterwards(self):
        from comic_archive.fast_import import FastWorkerSession
        elsewhere = Path(self.work.name) / 'elsewhere.json'
        elsewhere.write_text('{"fixture":1}')
        for label in ('removed', 'symlinked'):
            for cleanup_fails in (False, True):
                with self.subTest(label=label, cleanup_fails=cleanup_fails), FastWorkerSession(self.session) as pool:
                    with pool.batch() as adapter:
                        adapter.fail_close = cleanup_fails
                        self.state.unlink()
                        if label == 'symlinked':
                            self.state.symlink_to(elsewhere)
                    self.assertTrue(adapter.closed)
                    self.assertIsNone(pool.adapter)
                    self.assertIsNone(pool.saved_state)
                    self.state.unlink(missing_ok=True)
                    self.state.write_text('{"fixture":1}')

    def test_session_replaced_by_a_link_between_batches_is_refused_and_the_idle_browser_closed(self):
        from comic_archive.fast_import import FastWorkerSession
        elsewhere = Path(self.work.name) / 'elsewhere.json'
        elsewhere.write_text('{"fixture":1}')
        with FastWorkerSession(self.session) as pool:
            with pool.batch() as adapter:
                pass
            self.state.unlink()
            self.state.symlink_to(elsewhere)
            with self.assertRaises(ArchiveError):
                with pool.batch():
                    self.fail('The batch must not start through a symlinked session')
            self.assertTrue(adapter.closed)
            self.assertIsNone(pool.adapter)
            self.assertEqual(len(WorkerAdapter.instances), 1)
        self.assertEqual(elsewhere.read_text(), '{"fixture":1}')

