from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from comic_archive import batches, completion
from comic_archive.archive import Store, verify
from comic_archive.fast_import import FastBrowserAdapter, FastImportAdapter, fast_options
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.models import ArchiveError, Page
from comic_archive.queue_client import submit_job
from comic_archive.worker import execute
from test_archive import png
from test_jobs import AccountAdapter
from test_transport_session import Client, Response


class FastImportTests(unittest.TestCase):
    def test_public_queue_selects_only_a_fixed_speed_without_expanding_explicit_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / '.comic-session'
            (session / 'worker/requests').mkdir(parents=True)
            result = submit_job(session, 'archive_discovered', {'speed': 'fast', 'max_pages': 10, 'max_bytes': 1000})
            request = json.loads((session / 'worker/requests' / (result['task_id'] + '.json')).read_text())
            self.assertEqual(request['args'], {'speed': 'fast', 'max_pages': 10, 'max_bytes': 1000})
            settings = fast_options(request['args'])
            self.assertEqual((settings['max_pages'], settings['max_bytes'], settings['download_workers']), (10, 1000, 64))
            for options in ({'speed': True}, {'speed': 'turbo'}, {'speed': 'fast', 'download_workers': 64},
                            {'speed': 'fast', 'request_interval': 0.001}, {'speed': 'fast', '_pipeline': True}):
                with self.subTest(options=options), self.assertRaises(ArchiveError):
                    submit_job(session, 'archive_discovered', options)
            with self.assertRaises(ArchiveError): submit_job(session, 'sync', {'speed': 'fast'})

    @patch('builtins.print')
    def test_normal_worker_dispatches_fast_import_with_saved_session_and_retains_limits(self, output):
        worker = SimpleNamespace(session=Path('.comic-session'), preferred_browser='firefox', headless=True)
        with patch('comic_archive.fast_import.FastImportAdapter') as factory, \
                patch('comic_archive.jobs.archive_discovered', return_value={'status': 'limited'}) as archive:
            result = execute(worker, {'operation': 'archive_discovered',
                                     'args': {'speed': 'fast', 'max_pages': 12, 'max_bytes': 900, 'max_seconds': 5}})
        factory.assert_called_once_with(worker.session, 'firefox', headless=True)
        self.assertEqual(result, {'status': 'limited'})
        self.assertEqual(archive.call_args.kwargs, {'limit': 200, 'max_pages': 12, 'max_bytes': 900,
                                                   'max_seconds': 5, 'download_workers': 64})
        self.assertIs(archive.call_args.args[0], factory.return_value.__enter__.return_value)

    @patch('builtins.print')
    @patch('comic_archive.transport.Transport._pace')
    def test_real_fast_job_uses_owner_browser_pool_cache_journal_and_ordered_exports(self, pace, output):
        owner_threads = []
        class Browser(AccountAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(); self.owner = threading.get_ident()
                from comic_archive.transport import Transport
                self.transport = SimpleNamespace(assets=Transport(client=Client([Response(data=png(1)) for _ in range(4)]),
                                                                  interval=1/128, profile='fast'))
            def check_owner(self):
                owner_threads.append(threading.get_ident())
                if threading.get_ident() != self.owner: raise AssertionError('Browser owner changed')
            def __enter__(self): self.check_owner(); return self
            def __exit__(self, *args): self.check_owner(); self.transport.assets.close()
            def account_identity(self): self.check_owner(); return super().account_identity()
            def capture_automatic(self, url):
                self.check_owner()
                manifest = super().capture_automatic(url)
                return replace(manifest, episodes=(replace(manifest.episodes[0], pages=tuple(
                    Page(i, f'https://us-a.tapas.io/pc/{url.rsplit("/",1)[1]}/{i}', False) for i in (1, 2))),))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'
            with Inventory(root) as inventory:
                inventory.observe('42', 'Series', [{'id': key, 'title': 'Episode', 'order': int(key)-80,
                                                   'access_state': 'tapas_free'} for key in ('81', '82')], 'fixture')
            with patch('comic_archive.fast_import.FastBrowserAdapter', Browser):
                with FastImportAdapter(Path(temporary) / '.comic-session') as adapter:
                    adapter.supports_series_metadata = False
                    result = archive_discovered(adapter, root, max_pages=4, max_bytes=8*1024**3,
                                                download_workers=64)
            self.assertEqual(result['archived_episode_ids'], ['81', '82'])
            self.assertEqual(result['downloaded_pages'], 4)
            self.assertEqual(result['download_workers'], 64)
            self.assertEqual(verify(root), [])
            self.assertEqual(len(set(owner_threads)), 1)
            self.assertNotIn(threading.get_ident(), owner_threads)
            store = Store(root)
            try: self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            finally: store.close()

    def test_fast_pool_rechecks_identity_and_access_on_each_capture(self):
        browser = FastBrowserAdapter(Path('unused'))
        browser.network_guard = Mock()
        browser.page = Mock(); browser.page.url = 'https://tapas.io/'
        browser.context = Mock(); browser.context.cookies.return_value = []
        browser.page.evaluate.return_value = {'status': 200, 'text': json.dumps(
            {'data': {'user': {'id': 11}}, 'error_details': None})}
        try:
            first = browser.automatic_adapter()
            self.assertEqual(first.account_identity(), '11')
            client = browser.transport.assets.client
            second = browser.automatic_adapter()
            self.assertIs(browser.transport.assets.client, client)
            self.assertIsNot(first, second)
            self.assertEqual(second.account_identity(), '11')
            self.assertEqual(browser.page.evaluate.call_count, 2)
        finally:
            browser.transport.close()

    def test_fast_backlog_keeps_speed_in_saved_pending_task_and_completion_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); session = root / '.comic-session'; library = root / 'Library'
            worker = session / 'worker'; (worker / 'requests').mkdir(parents=True)
            (worker / 'responses').mkdir()
            (worker / 'status.json').write_text(json.dumps({'state': 'ready'}))
            with Inventory(library): pass
            batches._start(worker, {'speed': 'fast', 'archive_only': True, 'refresh_lists': False}, False)
            completion.configure(session, library, window_budgets={'speed': 'fast'})
            batches.step(session, library)
            state = batches._load(worker)
            self.assertEqual(state['pending']['options']['speed'], 'fast')
            self.assertEqual(state['budgets']['speed'], 'fast')
            self.assertEqual(completion._load(worker)['window_budgets']['speed'], 'fast')
            with self.assertRaises(ArchiveError): batches.budgets({'speed': 'fast'})

    def test_source_refusal_is_visible_without_publishing_private_worker_errors(self):
        from comic_archive.archive import atomic_json
        from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
        with tempfile.TemporaryDirectory() as temporary:
            registry = LocalWorkspaceRegistry(Path(temporary))
            workspace = registry.resolve(LOCAL_OWNER, registry.workspace_id)
            worker = workspace.session / 'worker'
            (worker / 'requests').mkdir(parents=True); (worker / 'responses').mkdir()
            task = submit_job(workspace.session, 'archive_discovered', {'speed': 'fast'})['task_id']
            response = {'task_id':task,'status':'error','error_code':'source_refused',
                        'error':'PRIVATE_BODY_AND_COOKIES',
                        'result':{'pause_reason':'source_refused','source_http_status':403,'private':'PRIVATE'}}
            atomic_json(worker / 'responses' / (task+'.json'), response)
            public = registry.task(LOCAL_OWNER, registry.workspace_id, task)
            self.assertEqual(public, {'task_id':task,'status':'error','code':'source_refused','source_http_status':403})
            self.assertNotIn('PRIVATE', json.dumps(public))
            response['result']['source_http_status'] = True
            atomic_json(worker / 'responses' / (task+'.json'), response)
            self.assertNotIn('source_http_status', registry.task(LOCAL_OWNER, registry.workspace_id, task))

