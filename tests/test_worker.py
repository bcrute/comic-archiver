import json
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
import types
from support import PRIVATE_FILE
from comic_archive import background
from comic_archive.models import ArchiveError
from comic_archive.errors import LibraryBusy
from comic_archive.locking import LibraryLock
from comic_archive.inventory import Inventory
from comic_archive.fast_import import FAST_WORKERS
from comic_archive.worker import execute, private_json, run_worker


class RecordingAdapter:
    def __init__(self):
        self.calls = []
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def inspect_episode(self,url,interactive=False):
        self.calls.append(url)
        return {'code':'old'}


class WorkerTests(unittest.TestCase):
    def work_in_temporary_directory(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        previous=os.getcwd();os.chdir(temporary.name);self.addCleanup(os.chdir,previous)
        return Path(temporary.name)

    def serve_one_pass(self,session,adapter,**options):
        options.setdefault('reload_adapter',False)
        with patch('builtins.print'),patch('comic_archive.worker.TapasAdapter',return_value=adapter), \
                patch('comic_archive.worker.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):run_worker(session,'firefox',**options)

    @patch('builtins.print')
    def test_worker_writes_fixed_cloud_error_code_and_message_for_dashboard_repair(self, output):
        from comic_archive.cloud_errors import CloudTransferError, MESSAGES
        class LoadedAdapter:
            def __enter__(self):return self
            def __exit__(self,*args):pass
        for code in MESSAGES:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as work:
                session=Path(work);request=session/'worker/requests/cloud-task.json'
                request.parent.mkdir(parents=True)
                private_json(request,{'operation':'cloud_upload','args':{}})
                with patch('comic_archive.worker.TapasAdapter',return_value=LoadedAdapter()), \
                        patch('comic_archive.cloud.upload_pending',side_effect=CloudTransferError(code)), \
                        patch('comic_archive.worker.time.sleep',side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):run_worker(session,'firefox',reload_adapter=False)
                response=session/'worker/responses/cloud-task.json';value=json.loads(response.read_text())
                self.assertEqual(value['status'],'error');self.assertEqual(value['error_code'],code)
                self.assertEqual(value['error'],MESSAGES[code]);self.assertFalse(request.exists())
                self.assertEqual(response.stat().st_mode&0o777,PRIVATE_FILE)

    def test_cloud_connection_tests_and_cleanup_are_bounded_without_source_navigation(self):
        test_id='f257c685-2a39-4e16-8dc0-353338e2f7d7'
        adapter=Mock()
        with patch('comic_archive.cloud.smoke_test',return_value={'verified_episodes':2}) as test:
            self.assertEqual(execute(adapter,{'operation':'cloud_test','args':{'provider':'google'}}),{'verified_episodes':2})
            test.assert_called_once_with(Path('Library'),'google',episodes=2,stream=True)
            test.reset_mock()
            execute(adapter, {'operation':'cloud_test','args':{'provider':'dropbox'}})
            test.assert_called_once_with(Path('Library'), 'dropbox', episodes=2, stream=True)
            test.reset_mock()
            execute(adapter, {'operation':'cloud_test','args':{'provider':'proton'}})
            test.assert_called_once_with(Path('Library'), 'proton', episodes=2)
        with patch('comic_archive.cloud.cleanup_test',return_value={'test_files_deleted':True}) as cleanup:
            self.assertTrue(execute(adapter,{'operation':'cloud_cleanup','args':{'test_id':test_id}})['test_files_deleted'])
            cleanup.assert_called_once_with(Path('Library'),test_id)
        for operation,args in [('cloud_test',{}),('cloud_test',{'provider':[]}),('cloud_test',{'provider':'google','episodes':999}),
                ('cloud_test',{'provider':'other'}),('cloud_cleanup',{'test_id':'../other'}),('cloud_cleanup',{'test_id':test_id,'path':'/other'})]:
            with self.subTest(operation=operation,args=args),self.assertRaises(ArchiveError):
                execute(adapter,{'operation':operation,'args':args})
        adapter.capture_automatic.assert_not_called();adapter.page.goto.assert_not_called()

    def test_cloud_retry_and_cache_purge_are_fixed_operations_without_source_navigation(self):
        for operation,module in (('cloud_upload','comic_archive.cloud.upload_pending'),('purge_cache','comic_archive.purge.purge_library')):
            adapter = Mock()
            with patch(module,return_value={'completed':True}) as action:
                self.assertEqual(execute(adapter,{'operation':operation,'args':{}}),{'completed':True})
                action.assert_called_once()
            adapter.page.goto.assert_not_called()
            with self.assertRaises(ArchiveError):execute(adapter,{'operation':operation,'args':{'output':'/private'}})

    def test_fixed_inspections_dispatch_without_capture_or_browser_navigation(self):
        cases = [
            ('inspect_reader_identity', {'url': 'https://tapas.io/episode/81'}, ('https://tapas.io/episode/81',), {}),
            ('inspect_episode_access', {'url': 'https://tapas.io/episode/81'}, ('https://tapas.io/episode/81',), {}),
            ('inspect_library_pages', {}, ('subscribed', 3), {}),
            ('inspect_library_pages', {'category': 'recent', 'max_pages': 2}, ('recent', 2), {}),
            ('inspect_episode', {'url': 'https://tapas.io/episode/81', 'details': True}, ('https://tapas.io/episode/81',), {'interactive': False, 'details': True}),
            ('inspect_series', {'id': '42', 'strict_order': False}, ('42',), {'strict_order': False}),
            ('inspect_series_metadata', {'id': '42'}, ('42',), {}),
            ('inspect_library', {}, (), {}),
            ('inspect_library', {'category': 'recent', 'details': True}, (), {'category': 'recent', 'details': True}),
            ('inspect_free_access', {}, (), {}),
            ('inspect_library_filter', {}, ('subscribed', 'comics'), {}),
            ('inspect_library_filter', {'category': 'updated', 'content_type': 'novels'}, ('updated', 'novels'), {}),
            ('inspect_account_navigation', {}, (), {}),
        ]
        for operation, args, expected_args, expected_kwargs in cases:
            with self.subTest(operation=operation, args=args):
                adapter = Mock()
                getattr(adapter, operation).return_value = {'synthetic': True}
                self.assertEqual(execute(adapter, {'operation': operation, 'args': args}), {'synthetic': True})
                getattr(adapter, operation).assert_called_once_with(*expected_args, **expected_kwargs)
                adapter.capture_automatic.assert_not_called()
                adapter.page.goto.assert_not_called()

    def test_inspection_boundary_rejects_untrusted_arguments_before_source_methods(self):
        cases = [
            ('audit_account', {'output': '/outside'}), ('check_session', {'account_id': '11'}),
            ('inspect_reader_identity', {'url': 'https://tapas.io/episode/81?buy=1'}),
            ('inspect_episode_access', {'url': 'https://evil.test/episode/81'}),
            ('inspect_library_pages', {'url': 'https://evil.test/'}),
            ('inspect_episode', {'url': 'https://tapas.io/episode/81', 'details': 'true'}),
            ('inspect_series', {'id': '42', 'strict_order': 1}),
            ('inspect_series_metadata', {'id': '42', 'url': 'https://evil.test/'}),
            ('enrich_series', {'id': '42', 'refresh': 'true'}),
            ('inspect_library', {'category': 'purchases'}),
            ('inspect_free_access', {'action': 'unlock'}),
            ('inspect_library_filter', {'category': 'updated', 'content_type': 'payments'}),
            ('inspect_account_navigation', {'action': 'buy'}),
            ('inspect_transaction_history', {'max_pages': True}),
            ('inspect_transaction_history', {'max_pages': 501}),
        ]
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                adapter = Mock()
                with self.assertRaises(ArchiveError):
                    execute(adapter, {'operation': operation, 'args': args})
                self.assertEqual(adapter.mock_calls, [])

    def test_new_user_jobs_do_not_require_a_legacy_discovery_file(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            os.chdir(temporary)
            try:
                adapter = Mock(interval=1.0)
                with patch('comic_archive.jobs.sync', return_value={'status': 'finished'}) as sync:
                    execute(adapter, {'operation': 'sync', 'args': {'scan_only': True}})
                    self.assertIsNone(sync.call_args.args[2])
                with patch('comic_archive.jobs.archive_discovered', return_value={'status': 'finished'}) as archive:
                    execute(adapter, {'operation': 'archive_discovered', 'args': {}})
                    self.assertIsNone(archive.call_args.args[2])
                    from comic_archive.worker import _archive_operation
                    _archive_operation(adapter, 'archive_discovered', {})
                    self.assertIsNone(archive.call_args.args[2])
                report = Path('Library/discovery.json')
                report.parent.mkdir()
                report.write_text('legacy report')
                from comic_archive.worker import _discovery_report
                self.assertEqual(_discovery_report(), report)
                report.unlink()
                report.symlink_to('missing-target')
                self.assertEqual(_discovery_report(), report)
            finally:
                os.chdir(previous)

    @patch('builtins.print')
    def test_queued_stop_produces_a_terminal_receipt_without_executing_or_reloading(self,output):
        from comic_archive.queue_client import submit_job
        from comic_archive.task_cancellation import request_queued_stop, cancelled_result
        class LoadedAdapter:
            __module__='comic_archive.adapters.tapas'
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'.comic-session';root=session/'worker'
            (root/'requests').mkdir(parents=True);(root/'responses').mkdir()
            private_json(root/'status.json',{'state':'ready'})
            task=submit_job(session,'sync',{'max_pages':1})['task_id']
            request_queued_stop(session,task)
            def receipt(library,operation):return cancelled_result(Path(work)/'Library',operation)
            with patch('comic_archive.worker.TapasAdapter',return_value=LoadedAdapter()),\
                    patch('comic_archive.worker.execute') as execute_task,\
                    patch('comic_archive.worker.importlib.reload') as reload,\
                    patch('comic_archive.task_cancellation.cancelled_result',side_effect=receipt),\
                    patch('comic_archive.worker.time.sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):run_worker(session,'firefox')
                execute_task.assert_not_called();reload.assert_not_called()
            result=json.loads((root/'responses'/f'{task}.json').read_text())
            self.assertEqual(result['status'],'ok');self.assertTrue(result['result']['cancelled_before_start'])
            self.assertEqual(result['result']['downloaded_pages'],0)
            self.assertFalse((root/'requests'/f'{task}.json').exists())
    def test_session_check_cannot_accept_credentials_accounts_or_source_paths(self):
        adapter=Mock()
        for args in ({'identity':'11'},{'cookies':'PRIVATE'},{'output':'/PRIVATE'},{'url':'https://tapas.io/episode/81'}):
            with self.assertRaises(ArchiveError):execute(adapter,{'operation':'check_session','args':args})
        adapter.account_identity.assert_not_called()
        with patch('comic_archive.jobs.check_session',return_value={'authenticated':True}) as check:
            self.assertEqual(execute(adapter,{'operation':'check_session','args':{}}),{'authenticated':True})
            check.assert_called_once_with(adapter,Path('Library'))

    def test_selected_series_dispatch_retains_budgets_fixed_output_and_request_spacing(self):
        adapter=Mock();adapter.interval=1.0
        for args in ({},{'id':'../42'},{'id':'42','output':'/PRIVATE'},{'id':'42','purchase':True}):
            with self.assertRaises(ArchiveError):execute(adapter,{'operation':'archive_watched_series','args':args})
        adapter.capture_automatic.assert_not_called()
        with patch('comic_archive.jobs.archive_watched_series',return_value={'status':'limited'}) as job:
            result=execute(adapter,{'operation':'archive_watched_series','args':{'id':'42','max_pages':1,'request_interval':.5}})
            job.assert_called_once_with(adapter,'42',Path('Library'),max_pages=1)
            self.assertEqual(result,{'status':'limited'})
        self.assertEqual(adapter.interval,1.0)

    def test_duplicate_worker_is_rejected_before_browser_start_or_status_changes(self):
        import threading
        with tempfile.TemporaryDirectory() as work:
            session=Path(work);root=session/'worker';root.mkdir()
            private_json(root/'status.json',{'state':'busy','task_id':'existing'})
            observed=[]
            def start():
                try:run_worker(session,'firefox')
                except LibraryBusy:observed.append('busy')
            with LibraryLock(root/'.runtime'),patch('comic_archive.worker.TapasAdapter') as browser:
                thread=threading.Thread(target=start);thread.start();thread.join(timeout=3)
                self.assertFalse(thread.is_alive());self.assertEqual(observed,['busy'])
                browser.assert_not_called()
            self.assertEqual(json.loads((root/'status.json').read_text())['task_id'],'existing')

    @patch('comic_archive.daily.start_scheduler',return_value=True)
    def test_schedule_operation_accepts_only_fixed_choices_and_never_reads_comics(self,start):
        with tempfile.TemporaryDirectory() as work:
            adapter=Mock();adapter.session=Path(work)
            for args in ({'output':'/tmp/outside'},{'options':{'unlock':True}},{'options':{'free_only':False}}):
                with self.assertRaises(ArchiveError):execute(adapter,{'operation':'configure_daily','args':args})
            self.assertFalse((adapter.session/'worker/daily.json').exists())
            result=execute(adapter,{'operation':'configure_daily','args':{'enabled':True,'at':'08:00','timezone':'America/Chicago'}})
            self.assertTrue(result['enabled']);self.assertEqual(result['scope'],'free_only')
            adapter.page.goto.assert_not_called();adapter.capture_automatic.assert_not_called()
            start.assert_called_once_with(adapter.session)

    @patch('builtins.print')
    def test_stable_worker_executes_fixed_tasks_without_reloading_its_adapter(self, output):
        class LoadedAdapter:
            __module__='comic_archive.adapters.tapas'
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def inspect_episode(self,url,interactive=False):return {'complete':False}
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)
            request=session/'worker/requests/test-task.json'
            request.parent.mkdir(parents=True)
            private_json(request,{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
            with patch('comic_archive.worker.TapasAdapter',return_value=LoadedAdapter()),patch('comic_archive.worker.importlib.reload') as reload,patch('comic_archive.worker.time.sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_worker(session,'firefox',reload_adapter=False)
                reload.assert_not_called()
            response=json.loads((session/'worker/responses/test-task.json').read_text())
            self.assertEqual(response['status'],'ok')
            self.assertEqual(response['result'],{'complete':False})

    @patch('builtins.print')
    def test_a_stop_request_being_written_delays_a_task_instead_of_failing_it(self, output):
        class LoadedAdapter:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def inspect_episode(self,url,interactive=False):return {'complete':False}
        for wait,expected in ((10,'ok'),(0,'error')):
            with tempfile.TemporaryDirectory() as work:
                session=Path(work)
                request=session/'worker/requests/test-task.json'
                request.parent.mkdir(parents=True)
                private_json(request,{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
                held=LibraryLock(session/'worker/.admission');attempts=[]
                def lock(session,held=held,attempts=attempts):
                    attempts.append(1)
                    if len(attempts)<3:raise LibraryBusy('PRIVATE')
                    return held
                pauses=[]
                def sleep(seconds,pauses=pauses):
                    pauses.append(seconds)
                    if seconds!=.05:raise KeyboardInterrupt
                with patch('comic_archive.worker.TapasAdapter',return_value=LoadedAdapter()),\
                        patch('comic_archive.worker.ADMISSION_WAIT_SECONDS',wait),\
                        patch('comic_archive.task_cancellation.admission_lock',side_effect=lock),\
                        patch('comic_archive.worker.time.sleep',side_effect=sleep):
                    with self.assertRaises(KeyboardInterrupt):
                        run_worker(session,'firefox',reload_adapter=False)
                response=json.loads((session/'worker/responses/test-task.json').read_text())
                self.assertEqual(response['status'],expected)
                self.assertEqual(pauses,[.05,.05,.5] if wait else [.5])
                with held:pass  # Released again.

    def test_job_request_spacing_is_bounded_and_restored_after_failure(self):
        adapter=Mock()
        adapter.interval=1.0
        for value in (True,float('nan'),.24,61):
            with self.assertRaises(ArchiveError):
                execute(adapter,{'operation':'archive_discovered','args':{'request_interval':value}})
        with patch('comic_archive.jobs.archive_discovered',side_effect=ArchiveError('stop')) as job:
            def failed(*args,**kwargs):
                self.assertEqual(adapter.interval,.5)
                raise ArchiveError('stop')
            job.side_effect=failed
            with self.assertRaises(ArchiveError):
                execute(adapter,{'operation':'archive_discovered','args':{'request_interval':.5}})
        self.assertEqual(adapter.interval,1.0)
    def test_unexpected_exception_exposes_location_without_credential_message(self):
        adapter = Mock()
        adapter.inspect_episode.side_effect = ValueError('cookie=secret signed-url-secret')
        with self.assertRaises(ArchiveError) as raised:
            execute(adapter, {'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
        self.assertIn('ValueError', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))

    def test_only_fixed_operations_allowed(self):
        adapter = Mock()
        for operation in ["eval", "shell", "navigate", "unlock", "login"]:
            with self.assertRaises(ArchiveError):
                execute(adapter, {"operation": operation, "args": {"code": "arbitrary"}})
        # Nothing queues these any more; a request file written by hand gets nowhere either.
        for operation, args in (("discover_available", {}), ("archive_series", {"id": "42", "output": "Library"}),
                                ("archive_episode", {"url": "https://tapas.io/episode/81", "output": "Library"})):
            with self.assertRaisesRegex(ArchiveError, "accepts only fixed"):
                execute(adapter, {"operation": operation, "args": args})
        adapter.assert_not_called()
        self.assertEqual(adapter.mock_calls, [])

    def test_library_inspection_cannot_request_purchases_or_untyped_details(self):
        adapter = Mock()
        for args in ({"category": "purchased"}, {"category": "recent", "details": "yes"}):
            with self.assertRaises(ArchiveError):
                execute(adapter, {"operation": "inspect_library", "args": args})
        self.assertEqual(adapter.mock_calls, [])

    def test_history_inspection_is_bounded_and_cannot_request_actions(self):
        adapter = Mock()
        for args in ({'max_pages':True},{'max_pages':501},{'max_pages':0},{'purchase':True}):
            with self.assertRaises(ArchiveError):
                execute(adapter,{'operation':'inspect_transaction_history','args':args})
        adapter.inspect_transaction_history.assert_not_called()
        execute(adapter,{'operation':'inspect_transaction_history','args':{'max_pages':3}})
        adapter.inspect_transaction_history.assert_called_once_with(3)

    def test_history_pages_checkpoint_public_links_without_private_cursor_data(self):
        with tempfile.TemporaryDirectory() as work:
            adapter=Mock()
            adapter.session=Path(work)
            root=adapter.session/'worker'
            root.mkdir()
            private_json(root/'status.json',{'state':'busy','task_id':'history-example'})
            observed={'authenticated':True,'complete':False,'pages':[{'number':1,'row_count':20}],
                      'content_links':['https://tapas.io/series/example']}
            def inspect(max_pages,**options):
                options['on_page'](observed)
                self.assertEqual(json.loads((root/'progress.json').read_text())['history_page_count'],1)
                return observed
            adapter.inspect_transaction_history.side_effect=inspect
            result=execute(adapter,{'operation':'inspect_transaction_history','args':{'max_pages':3}})
            self.assertEqual(result,observed)
            value=json.loads((root/'history.json').read_text())
            self.assertFalse(value['private_cursor_persisted'])
            self.assertFalse(value['account_inventory_complete'])
            self.assertEqual(value['content_links'],observed['content_links'])
            self.assertEqual((root/'history.json').stat().st_mode & 0o777,PRIVATE_FILE)

    @patch("builtins.print")
    def test_file_queue_processes_inspection_and_writes_private_response(self, output):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work)
            requests = session / "worker/requests"
            requests.mkdir(parents=True)
            task = requests / "test-task.json"
            private_json(task, {"operation": "inspect_episode", "args": {"url": "https://tapas.io/episode/81"}})
            with patch("comic_archive.worker.TapasAdapter") as adapter_type, patch("comic_archive.worker.time.sleep", side_effect=KeyboardInterrupt):
                adapter = adapter_type.return_value.__enter__.return_value
                adapter.inspect_episode.return_value = {"source_url": "https://tapas.io/episode/81", "complete": False}
                with self.assertRaises(KeyboardInterrupt):
                    run_worker(session, "firefox")
                adapter.inspect_episode.assert_called_once_with("https://tapas.io/episode/81", interactive=False)
            response = session / "worker/responses/test-task.json"
            self.assertEqual(json.loads(response.read_text())["status"], "ok")
            self.assertEqual(response.stat().st_mode & 0o777, PRIVATE_FILE)
            self.assertFalse(task.exists())
            self.assertEqual(json.loads((session / "worker/status.json").read_text())["state"], "stopped")


    def test_arguments_that_are_not_an_object_are_refused(self):
        for args in ([],'x',5,None):
            with self.subTest(args=args):
                adapter=Mock()
                with self.assertRaisesRegex(ArchiveError,'must be an object'):
                    execute(adapter,{'operation':'check_session','args':args})
                self.assertEqual(adapter.mock_calls,[])

    def test_page_size_inspection_needs_a_bounded_size_and_a_series_already_in_the_library(self):
        self.work_in_temporary_directory()
        with Inventory(Path('Library')) as inventory:
            inventory.observe('42','S',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
        adapter=Mock()
        for args in ({},{'id':42},{'id':'0'},{'id':'42','max_limit':30},{'id':'42','max_limit':True},{'id':'42','path':'/tmp'}):
            with self.subTest(args=args),self.assertRaises(ArchiveError) as caught:
                execute(adapter,{'operation':'inspect_episode_page_size','args':args})
            self.assertNotIn('Worker task failed',str(caught.exception))
        with self.assertRaisesRegex(ArchiveError,'already observed'):
            execute(adapter,{'operation':'inspect_episode_page_size','args':{'id':'43'}})
        self.assertEqual(adapter.mock_calls,[])
        adapter.inspect_episode_page_size.return_value={'page_size':100}
        result=execute(adapter,{'operation':'inspect_episode_page_size','args':{'id':'42','max_limit':100}})
        self.assertEqual(result,{'page_size':100})
        adapter.inspect_episode_page_size.assert_called_once_with('42',100)

    def test_malformed_options_of_fixed_operations_are_refused_before_any_source_method(self):
        self.work_in_temporary_directory()
        url='https://tapas.io/episode/81'
        cases=[('configure_priority',{}),('configure_priority',{'priority':'cheapest_first'}),
            ('configure_priority',{'priority':'owned_first','path':'/tmp'}),
            ('inspect_library_pages',{'category':'recent','url':'https://evil.test/'}),
            ('inspect_episode',{}),('inspect_episode',{'url':url+'?buy=1'}),('inspect_episode',{'url':url,'cookies':'PRIVATE'}),
            ('diagnose_headless',{}),('diagnose_headless',{'url':'https://tapas.io/series/1'}),
            ('diagnose_headless',{'url':url,'cookies':'PRIVATE'}),
            ('archive_discovered',{'retry_episode_id':'0','limit':1}),('archive_discovered',{'retry_episode_id':'81'}),
            ('archive_discovered',{'retry_episode_id':81,'limit':1}),('archive_discovered',{'retry_episode_id':'81','limit':2}),
            ('sync',{'speed':'fast'})]
        for operation,args in cases:
            with self.subTest(operation=operation,args=args):
                adapter=Mock(interval=1.0)
                with patch('comic_archive.cloud.upload_pending'),self.assertRaises(ArchiveError) as caught:
                    execute(adapter,{'operation':operation,'args':args})
                self.assertNotIn('Worker task failed',str(caught.exception))
                self.assertEqual(adapter.mock_calls,[])

    def test_fixed_operations_run_their_handlers_against_the_fixed_library_path(self):
        url='https://tapas.io/episode/81'
        adapter=Mock()
        adapter.diagnose_headless.return_value={'headless':'ok'}
        self.assertEqual(execute(adapter,{'operation':'diagnose_headless','args':{'url':url}}),{'headless':'ok'})
        adapter.diagnose_headless.assert_called_once_with(url)
        with patch('comic_archive.metadata.enrich_series',autospec=True,return_value={'enriched':True}) as enrich:
            result=execute(adapter,{'operation':'enrich_series','args':{'id':'my-series','refresh':True}})
            self.assertEqual(result,{'enriched':True})
            enrich.assert_called_once_with(adapter,'my-series',Path('Library'),True)

    def test_fast_imports_use_the_fast_adapter_with_filled_limits_and_leave_the_browser_adapter_alone(self):
        self.work_in_temporary_directory()
        adapter=Mock(interval=1.0);fast=Mock()
        session=MagicMock();session.batch.return_value.__enter__.return_value=fast
        session.batch.return_value.__exit__.return_value=False
        with patch('comic_archive.cloud.upload_pending'), \
                patch('comic_archive.jobs.archive_watched_series',return_value={'status':'limited'}) as watched, \
                patch('comic_archive.jobs.archive_series_url',return_value={'status':'finished'}) as series:
            result=execute(adapter,{'operation':'archive_watched_series','args':{'id':'42','speed':'fast','limit':1}},fast_session=session)
            self.assertEqual(result,{'status':'limited'})
            args,options=watched.call_args
            self.assertIs(args[0],fast);self.assertEqual(args[1:],('42',Path('Library')))
            self.assertEqual(options['limit'],1);self.assertNotIn('speed',options)
            self.assertEqual(options['download_workers'],FAST_WORKERS);self.assertIn('max_bytes',options)
            result=execute(adapter,{'operation':'archive_series_url','args':{'url':'https://tapas.io/series/example','speed':'fast'}},fast_session=session)
            self.assertEqual(result,{'status':'finished'})
            args,options=series.call_args
            self.assertIs(args[0],fast);self.assertEqual(args[1:],('https://tapas.io/series/example',Path('Library')))
        self.assertEqual(adapter.mock_calls,[])

    def test_history_checkpoint_records_only_a_well_formed_task_id(self):
        observed={'authenticated':True,'complete':False,'pages':[{'number':1,'row_count':20}],
                  'content_links':['https://tapas.io/series/example']}
        cases=[(None,None),('{broken',None),('[]',None),(json.dumps({'task_id':'../../x'}),None),
               (json.dumps({'task_id':7}),None),(json.dumps({'task_id':'a'*101}),None),
               (json.dumps({'task_id':'history-1'}),'history-1')]
        for text,expected in cases:
            with self.subTest(status=text),tempfile.TemporaryDirectory() as work:
                adapter=Mock();adapter.session=Path(work);root=adapter.session/'worker';root.mkdir()
                if text is not None:(root/'status.json').write_text(text)
                def inspect(max_pages,**options):
                    options['on_page'](observed)
                    return observed
                adapter.inspect_transaction_history.side_effect=inspect
                execute(adapter,{'operation':'inspect_transaction_history','args':{'max_pages':3}})
                progress=json.loads((root/'progress.json').read_text())
                self.assertEqual(progress['task_id'],expected)
                self.assertEqual(progress['history_page_count'],1)

    def test_a_symlinked_request_is_never_followed_or_run_but_later_requests_still_are(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            outside=Path(work)/'outside.json'
            private_json(outside,{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
            (requests/'a-link.json').symlink_to(outside)
            private_json(requests/'b-real.json',{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/82'}})
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,['https://tapas.io/episode/82'])
            self.assertEqual([path.name for path in (session/'worker/responses').iterdir()],['b-real.json'])
            self.assertTrue((requests/'a-link.json').is_symlink());self.assertTrue(outside.exists())

    def test_a_wrongly_named_request_is_dropped_and_later_requests_still_run(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            # 'bad name.json' sorts first, so a worker that stopped on it would never reach the task behind it.
            (requests/'bad name.json').write_text('{"operation":"inspect_episode","args":{"url":"https://tapas.io/episode/81"}}')
            private_json(requests/'good-task.json',{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/82'}})
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,['https://tapas.io/episode/82'])
            self.assertEqual([path.name for path in (session/'worker/responses').iterdir()],['good-task.json'])
            self.assertFalse((requests/'bad name.json').exists());self.assertFalse((requests/'good-task.json').exists())

    def test_a_request_that_already_has_a_response_is_dropped_and_not_run_again(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';root=session/'worker'
            (root/'requests').mkdir(parents=True);(root/'responses').mkdir()
            request=root/'requests/test-task.json';response=root/'responses/test-task.json'
            private_json(request,{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
            private_json(response,{'task_id':'test-task','status':'ok','result':{'first':True}})
            before=response.read_bytes()
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,[]);self.assertEqual(response.read_bytes(),before)
            self.assertFalse(request.exists())

    def test_unusable_requests_get_an_error_response_without_raw_exception_text(self):
        cases={'too large':(json.dumps({'operation':'inspect_episode','args':{'padding':'x'*17000}}),
                            'Worker request is too large.','job_failed'),
               'not an object':('[]','Worker request must be an object.','job_failed'),
               'not JSON':('{PRIVATE_COOKIE','Worker task failed; raw exceptions suppressed to protect credentials.',None)}
        for name,(text,message,code) in cases.items():
            with self.subTest(name),tempfile.TemporaryDirectory() as work:
                session=Path(work)/'session';root=session/'worker';(root/'requests').mkdir(parents=True)
                request=root/'requests/test-task.json';request.write_text(text)
                adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
                response=root/'responses/test-task.json';value=json.loads(response.read_text())
                self.assertEqual(value['status'],'error');self.assertEqual(value['error'],message)
                self.assertEqual(value.get('error_code'),code);self.assertNotIn('PRIVATE',response.read_text())
                self.assertEqual(response.stat().st_mode&0o777,PRIVATE_FILE)
                self.assertEqual(adapter.calls,[]);self.assertFalse(request.exists())

    def test_adapter_code_is_reloaded_between_tasks_only_when_reload_is_enabled(self):
        class LoadedAdapter(RecordingAdapter):
            __module__='comic_archive.adapters.tapas'
        class NewCode(LoadedAdapter):
            def inspect_episode(self,url,interactive=False):return {'code':'new'}
        for reload_adapter,expected in ((True,'new'),(False,'old')):
            with self.subTest(reload_adapter=reload_adapter),tempfile.TemporaryDirectory() as work:
                session=Path(work)/'session';request=session/'worker/requests/test-task.json'
                request.parent.mkdir(parents=True)
                private_json(request,{'operation':'inspect_episode','args':{'url':'https://tapas.io/episode/81'}})
                with patch('comic_archive.worker.importlib.reload',return_value=types.SimpleNamespace(TapasAdapter=NewCode)):
                    self.serve_one_pass(session,LoadedAdapter(),reload_adapter=reload_adapter)
                value=json.loads((session/'worker/responses/test-task.json').read_text())
                self.assertEqual(value['result'],{'code':expected})

    def test_worker_failing_after_startup_is_not_marked_as_a_startup_failure(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session'
            with patch('builtins.print'),patch('comic_archive.worker.TapasAdapter',return_value=RecordingAdapter()), \
                    patch('comic_archive.account_export.worker_tick'), \
                    patch('comic_archive.completion.worker_tick',side_effect=ArchiveError('Safe failure')):
                with self.assertRaises(ArchiveError):run_worker(session,'firefox',reload_adapter=False)
            status=json.loads((session/'worker/status.json').read_text())
            self.assertEqual(status['state'],'stopped');self.assertNotIn('last_error_code',status)
            self.assertFalse(background.running(session/'worker'))


if __name__ == "__main__":
    unittest.main()
