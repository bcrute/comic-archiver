import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from comic_archive.archive import Store, verify
from comic_archive.cli import parser
from comic_archive.downloads import AssetDownload
from comic_archive.hosted import WorkspaceRegistry
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_series_url, DownloadBudget
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.selection import series_link
from comic_archive.task_cancellation import cancelled_result
from comic_archive.worker import execute
from test_jobs import AccountAdapter
from test_novels import NovelAdapter
from test_archive import png


class ChosenComic(AccountAdapter):
    def series_identity(self, key):
        self.calls.append(('resolve', key))
        return {'source_series_id': '42', 'title': 'Series'}

    def inspect_library(self, *args, **kwargs):
        raise AssertionError('A chosen-series import must not scan account lists.')


class FastImportTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / 'Library'
        self.adapter = ChosenComic()

    def test_public_series_links_are_bounded_and_canonicalized(self):
        for url in ('https://tapas.io/series/Chosen', 'https://tapas.io/series/Chosen/info',
                    'https://tapas.io/series/Chosen/info/?utm_source=share#chapter'):
            self.assertEqual(series_link(url), 'Chosen')
        for url in (None, '', 42, 'http://tapas.io/series/x', 'https://tapas.io.evil.example/series/x',
                    'https://secret@tapas.io/series/x', 'https://tapas.io:443/series/x',
                    'https://tapas.io/episode/81', 'https://tapas.io/series/a/../../who-am-i',
                    'https://tapas.io/series/' + 'x' * 101):
            with self.subTest(url=url), self.assertRaises(ArchiveError):
                series_link(url)

    @patch('builtins.print')
    def test_new_library_imports_chosen_comic_without_global_discovery(self, output):
        result = archive_series_url(self.adapter, 'https://tapas.io/series/chosen/info', self.root, limit=1)
        self.assertEqual(result['source_series_id'], '42')
        self.assertEqual(result['archived_episode_ids'], ['82'])
        self.assertEqual(verify(self.root), [])
        self.assertEqual(self.adapter.calls[:3], ['identity', ('resolve', 'chosen'), ('series', '42')])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual([s['id'] for s in inventory.watched_series()], ['42'])

    @patch('builtins.print')
    def test_chosen_import_never_downloads_another_series_queue(self, output):
        with Inventory(self.root) as inventory:
            inventory.observe('10', 'Another', [{'id': '99', 'title': 'Other', 'order': 1,
                                                'access_state': 'tapas_unlocked'}], 'fixture')
        result = archive_series_url(self.adapter, 'https://tapas.io/series/chosen', self.root, limit=1)
        self.assertEqual(result['archived_episode_ids'], ['82'])
        self.assertNotIn(('capture', '99'), self.adapter.calls)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='99'").fetchone()[0], 'queued')

    @patch('builtins.print')
    def test_novel_link_and_revoked_comic_access_keep_normal_gates(self, output):
        novel = NovelAdapter()
        novel.series_identity = lambda key: {'source_series_id': '42', 'title': 'Novel'}
        result = archive_series_url(novel, 'https://tapas.io/series/novel/info', self.root, free_only=True, limit=1)
        self.assertEqual(result['downloaded_chapters'], 1)
        other = Path(self.work.name) / 'Other'
        self.adapter.denied.add('82')
        result = archive_series_url(self.adapter, 'https://tapas.io/series/chosen', other, limit=1)
        self.assertEqual(result['unavailable_episode_ids'], ['82'])
        self.assertEqual(self.adapter.calls[-1], ('capture', '82'))
        self.assertEqual(self.adapter.downloads, [])

    @patch('builtins.print')
    def test_binding_mismatch_and_invalid_identity_stop_before_scans_and_assets(self, output):
        with Inventory(self.root) as inventory:
            inventory.bind_account('12')
        with self.assertRaises(ArchiveError):
            archive_series_url(self.adapter, 'https://tapas.io/series/chosen', self.root)
        self.assertEqual(self.adapter.calls, ['identity'])
        for identity in ({}, {'source_series_id': 42, 'title': 'X'},
                         {'source_series_id': '42', 'title': ''}):
            adapter = ChosenComic()
            adapter.series_identity = Mock(return_value=identity)
            with self.assertRaises(ArchiveError):
                archive_series_url(adapter, 'https://tapas.io/series/chosen', Path(self.work.name) / 'Invalid')
            self.assertEqual(adapter.downloads, [])
            self.assertNotIn(('series', '42'), adapter.calls)

    def test_queue_cli_worker_and_server_caps_support_explicit_series_links(self):
        registry = WorkspaceRegistry(Path(self.work.name) / 'hosted')
        workspace = registry.create('owner')
        requests = workspace.session / 'worker/requests'
        requests.mkdir(parents=True)
        url = 'https://tapas.io/series/chosen/info'
        task = registry.submit('owner', workspace.id, 'archive_series_url', {'url': url, 'limit': 1})
        request = json.loads((requests / (task['task_id'] + '.json')).read_text())
        self.assertEqual(request['args']['max_seconds'], registry.limits.max_seconds)
        with self.assertRaises(ArchiveError):
            registry.submit('other', workspace.id, 'archive_series_url', {'url': url})
        for options in ({'url': url, 'max_pages': 1001}, {'url': 'https://evil.example/x'},
                        {'url': url, 'output': '/tmp/elsewhere'}):
            with self.assertRaises(ArchiveError):
                registry.submit('owner', workspace.id, 'archive_series_url', options)
        args = parser().parse_args(['import-series', url, '--via-worker'])
        self.assertEqual((args.limit, args.download_workers, args.max_pages), (1, 2, 1000))
        adapter = Mock(interval=1.0)
        with patch('comic_archive.jobs.archive_series_url', return_value={'status': 'limited'}) as job:
            execute(adapter, request)
            self.assertEqual(job.call_args.args[:2], (adapter, url))
        self.assertEqual(cancelled_result(workspace.library, 'archive_series_url')['status'], 'cancelled')

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

