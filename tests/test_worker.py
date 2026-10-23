import json
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from support import PRIVATE_FILE
from comic_archive import background
from comic_archive.models import ArchiveError
from comic_archive.errors import LibraryBusy
from comic_archive.locking import LibraryLock
from comic_archive.fast_import import FAST_WORKERS
from comic_archive.worker import execute, private_json, run_worker, _embedded_reader, _EmbeddedFastUnavailable


class RecordingAdapter:
    """The browser adapter's stand-in. The queued task it serves is a cloud upload, counted here."""
    def __init__(self):
        self.calls = []
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def upload(self,*args,**kwargs):
        self.calls.append('upload')
        return {'code':'old'}


class WorkerTests(unittest.TestCase):
    def test_embedded_startup_does_not_enter_a_network_reader_and_owns_its_close(self):
        calls = []
        class Reader:
            def __enter__(self):
                raise AssertionError('Held startup must not authenticate over the network')
            def close(self):
                calls.append('closed')
        reader = Reader()
        with _embedded_reader(lambda: reader) as owned:
            self.assertIs(owned, reader)
        self.assertEqual(calls, ['closed'])
        problem = ArchiveError('Original failure')
        with self.assertRaises(ArchiveError) as caught:
            with _embedded_reader(lambda: reader):
                raise problem
        self.assertIs(caught.exception, problem)
        self.assertEqual(calls, ['closed', 'closed'])

    def test_an_embedded_worker_without_native_fast_cannot_launch_a_desktop_browser(self):
        from types import SimpleNamespace
        with patch('comic_archive.fast_import.FastImportAdapter', side_effect=AssertionError('Desktop browser')):
            with self.assertRaisesRegex(ArchiveError, 'native Fast'):
                execute(SimpleNamespace(interval=1.0), {'operation': 'archive_discovered',
                    'args': {'speed': 'fast'}}, fast_session=_EmbeddedFastUnavailable())

    def work_in_temporary_directory(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        previous=os.getcwd();os.chdir(temporary.name);self.addCleanup(os.chdir,previous)
        return Path(temporary.name)

    def serve_one_pass(self,session,adapter,**options):
        with patch('builtins.print'),patch('comic_archive.worker.TapasAdapter',return_value=adapter), \
                patch('comic_archive.cloud.upload_pending',side_effect=adapter.upload), \
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
                    with self.assertRaises(KeyboardInterrupt):run_worker(session,'firefox')
                response=session/'worker/responses/cloud-task.json';value=json.loads(response.read_text())
                self.assertEqual(value['status'],'error');self.assertEqual(value['error_code'],code)
                self.assertEqual(value['error'],MESSAGES[code]);self.assertFalse(request.exists())
                self.assertEqual(response.stat().st_mode&0o777,PRIVATE_FILE)

    def test_cloud_connection_tests_and_cleanup_are_bounded_without_source_navigation(self):
        test_id='f257c685-2a39-4e16-8dc0-353338e2f7d7'
        adapter=Mock()
        with patch('comic_archive.cloud.smoke_test',return_value={'verified_episodes':2}) as test:
            self.assertEqual(execute(adapter,{'operation':'cloud_test','args':{'provider':'b2'}}),{'verified_episodes':2})
            test.assert_called_once_with(Path('Library'),'b2',episodes=2,stream=True)
        with patch('comic_archive.cloud.cleanup_test',return_value={'test_files_deleted':True}) as cleanup:
            self.assertTrue(execute(adapter,{'operation':'cloud_cleanup','args':{'test_id':test_id}})['test_files_deleted'])
            cleanup.assert_called_once_with(Path('Library'),test_id)
        for operation,args in [('cloud_test',{}),('cloud_test',{'provider':[]}),('cloud_test',{'provider':'b2','episodes':999}),
                ('cloud_test',{'provider':'other'}),('cloud_test',{'provider':'google'}),('cloud_cleanup',{'test_id':'../other'}),('cloud_cleanup',{'test_id':test_id,'path':'/other'})]:
            with self.subTest(operation=operation,args=args),self.assertRaises(ArchiveError):
                execute(adapter,{'operation':operation,'args':args})
        adapter.capture_automatic.assert_not_called();adapter.page.goto.assert_not_called()

    def test_cloud_retry_is_a_fixed_operation_without_source_navigation(self):
        adapter = Mock()
        with patch('comic_archive.cloud.upload_pending',return_value={'completed':True}) as action:
            self.assertEqual(execute(adapter,{'operation':'cloud_upload','args':{}}),{'completed':True})
            action.assert_called_once_with(Path('Library'),limit=3)
        adapter.page.goto.assert_not_called()
        with self.assertRaises(ArchiveError):execute(adapter,{'operation':'cloud_upload','args':{'output':'/private'}})

    def test_task_boundary_rejects_untrusted_arguments_before_source_methods(self):
        cases = [
            ('audit_account', {'output': '/outside'}), ('cloud_upload', {'provider': 'b2'}),
            ('archive_discovered', {'output': '/outside'}), ('sync', {'categories': ['purchased'], 'cookies': 'PRIVATE'}),
        ]
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                adapter = Mock()
                with patch('comic_archive.cloud.upload_pending'), self.assertRaises(ArchiveError):
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
    def test_queued_stop_produces_a_terminal_receipt_without_executing(self,output):
        from comic_archive.queue_client import submit_job
        from comic_archive.task_cancellation import cancelled_result
        class LoadedAdapter:
            __module__='comic_archive.adapters.tapas'
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'.comic-session';root=session/'worker'
            (root/'requests').mkdir(parents=True);(root/'responses').mkdir();(root/'cancellations').mkdir(mode=0o700)
            private_json(root/'status.json',{'state':'ready'})
            task=submit_job(session,'sync',{'max_pages':1})['task_id']
            # A pause left for a task the worker had not yet started; worker restarts reach it still queued.
            private_json(root/'cancellations'/f'{task}.json',{'document_type':'queued_comic_import_stop','task_id':task})
            def receipt(library,operation):return cancelled_result(Path(work)/'Library',operation)
            with patch('comic_archive.worker.TapasAdapter',return_value=LoadedAdapter()),\
                    patch('comic_archive.worker.execute') as execute_task,\
                    patch('comic_archive.task_cancellation.cancelled_result',side_effect=receipt),\
                    patch('comic_archive.worker.time.sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):run_worker(session,'firefox')
                execute_task.assert_not_called()
            result=json.loads((root/'responses'/f'{task}.json').read_text())
            self.assertEqual(result['status'],'ok');self.assertTrue(result['result']['cancelled_before_start'])
            self.assertEqual(result['result']['downloaded_pages'],0)
            self.assertFalse((root/'requests'/f'{task}.json').exists())

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

    @patch('builtins.print')
    def test_a_stop_request_being_written_delays_a_task_instead_of_failing_it(self, output):
        class LoadedAdapter:
            def __enter__(self):return self
            def __exit__(self,*args):pass
        for wait,expected in ((10,'ok'),(0,'error')):
            with tempfile.TemporaryDirectory() as work:
                session=Path(work)
                request=session/'worker/requests/test-task.json'
                request.parent.mkdir(parents=True)
                private_json(request,{'operation':'cloud_upload','args':{}})
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
                        patch('comic_archive.cloud.upload_pending',return_value={'complete':False}),\
                        patch('comic_archive.worker.ADMISSION_WAIT_SECONDS',wait),\
                        patch('comic_archive.task_cancellation.admission_lock',side_effect=lock),\
                        patch('comic_archive.worker.time.sleep',side_effect=sleep):
                    with self.assertRaises(KeyboardInterrupt):
                        run_worker(session,'firefox')
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
        with patch('comic_archive.cloud.upload_pending',side_effect=ValueError('cookie=secret signed-url-secret')), \
                self.assertRaises(ArchiveError) as raised:
            execute(adapter, {'operation':'cloud_upload','args':{}})
        self.assertIn('ValueError', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))

    def test_only_fixed_operations_allowed(self):
        adapter = Mock()
        for operation in ["eval", "shell", "navigate", "unlock", "login", "inspect_episode", "check_session",
                          "configure_daily", "purge_cache", "enrich_series", "archive_watched_series"]:
            with self.assertRaises(ArchiveError):
                execute(adapter, {"operation": operation, "args": {"code": "arbitrary"}})
        # Nothing queues these any more; a request file written by hand gets nowhere either.
        for operation, args in (("discover_available", {}), ("archive_series", {"id": "42", "output": "Library"}),
                                ("archive_episode", {"url": "https://tapas.io/episode/81", "output": "Library"})):
            with self.assertRaisesRegex(ArchiveError, "accepts only fixed"):
                execute(adapter, {"operation": operation, "args": args})
        adapter.assert_not_called()
        self.assertEqual(adapter.mock_calls, [])

    @patch("builtins.print")
    def test_file_queue_processes_a_task_and_writes_private_response(self, output):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work)
            requests = session / "worker/requests"
            requests.mkdir(parents=True)
            task = requests / "test-task.json"
            private_json(task, {"operation": "cloud_upload", "args": {}})
            with patch("comic_archive.worker.TapasAdapter"), \
                    patch("comic_archive.cloud.upload_pending", return_value={"uploaded": 0}) as upload, \
                    patch("comic_archive.worker.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_worker(session, "firefox")
                upload.assert_called_once_with(Path("Library"), limit=3)
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
                    execute(adapter,{'operation':'cloud_upload','args':args})
                self.assertEqual(adapter.mock_calls,[])

    def test_malformed_options_of_fixed_operations_are_refused_before_any_source_method(self):
        self.work_in_temporary_directory()
        url='https://tapas.io/episode/81'
        cases=[('archive_discovered',{'url':url}),('archive_discovered',{'free_only':'yes'}),
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

    def test_account_audit_runs_against_the_fixed_library_path_and_keeps_its_report_private(self):
        self.work_in_temporary_directory()
        fields=('audited_at','archive_integrity_passed','verified_inventory_episode_count','portable_archive_count',
                'missing_recorded_accessible_episode_count','coverage_blockers','series_scan_gap_count','account_inventory_complete')
        def audit(library,report):
            report.parent.mkdir(parents=True);report.write_text('{}')
            return dict.fromkeys(fields,1)|{'inventory_index_problems':['a','b'],'unindexed_archives':['c'],'private':'PRIVATE'}
        adapter=Mock()
        with patch('comic_archive.audit.audit_library',side_effect=audit) as audited:
            result=execute(adapter,{'operation':'audit_account','args':{}})
        audited.assert_called_once_with(Path('Library'),Path('Library/.archive-state/account-audit.json'))
        self.assertEqual(result,dict.fromkeys(fields,1)|{'inventory_index_problem_count':2,'unindexed_archive_count':1})
        self.assertEqual(Path('Library/.archive-state/account-audit.json').stat().st_mode&0o777,PRIVATE_FILE)
        self.assertEqual(adapter.mock_calls,[])

    def test_fast_imports_use_the_fast_adapter_with_filled_limits_and_leave_the_browser_adapter_alone(self):
        self.work_in_temporary_directory()
        adapter=Mock(interval=1.0);fast=Mock()
        session=MagicMock();session.batch.return_value.__enter__.return_value=fast
        session.batch.return_value.__exit__.return_value=False
        with patch('comic_archive.cloud.upload_pending'), \
                patch('comic_archive.jobs.archive_discovered',return_value={'status':'limited'}) as archive:
            result=execute(adapter,{'operation':'archive_discovered','args':{'speed':'fast','limit':1}},fast_session=session)
            self.assertEqual(result,{'status':'limited'})
            args,options=archive.call_args
            self.assertIs(args[0],fast);self.assertEqual(args[1:],(Path('Library'),None))
            self.assertEqual(options['limit'],1);self.assertNotIn('speed',options)
            self.assertEqual(options['download_workers'],FAST_WORKERS);self.assertIn('max_bytes',options)
        self.assertEqual(adapter.mock_calls,[])

    def test_a_failing_cloud_upload_is_reported_with_the_archive_job_instead_of_stopping_it(self):
        from comic_archive.cloud_errors import CloudTransferError
        from comic_archive.network import NetworkPolicyHold
        self.work_in_temporary_directory()
        request={'operation':'archive_discovered','args':{'limit':1}}
        for failure,code in ((CloudTransferError('cloud_login_required'),'cloud_login_required'),
                             (CloudTransferError('cloud_cap'),'cloud_cap'),(ArchiveError('Cloud retry state is invalid.'),'cloud_unknown'),
                             (OSError('PRIVATE'),'cloud_unknown')):
            with self.subTest(code=code),patch('comic_archive.cloud.upload_pending',side_effect=failure) as upload, \
                    patch('comic_archive.jobs.archive_discovered',return_value={'status':'finished'}) as series:
                result=execute(Mock(interval=1.0),request)
                self.assertEqual(result,{'status':'finished','cloud_upload_error_code':code})
                upload.assert_called_once();series.assert_called_once()
        # A connection hold is left to the job, which meets the same rule and pauses with its own receipt.
        paused={'status':'paused','pause_reason':'network_policy'}
        with patch('comic_archive.cloud.upload_pending',side_effect=NetworkPolicyHold('held')), \
                patch('comic_archive.jobs.archive_discovered',return_value=paused) as series:
            self.assertEqual(execute(Mock(interval=1.0),request),paused)
            series.assert_called_once()

    def test_a_symlinked_request_is_never_followed_or_run_but_later_requests_still_are(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            outside=Path(work)/'outside.json'
            private_json(outside,{'operation':'cloud_upload','args':{}})
            (requests/'a-link.json').symlink_to(outside)
            private_json(requests/'b-real.json',{'operation':'cloud_upload','args':{}})
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,['upload'])
            self.assertEqual([path.name for path in (session/'worker/responses').iterdir()],['b-real.json'])
            self.assertTrue((requests/'a-link.json').is_symlink());self.assertTrue(outside.exists())

    def test_a_wrongly_named_request_is_dropped_and_later_requests_still_run(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            # 'bad name.json' sorts first, so a worker that stopped on it would never reach the task behind it.
            (requests/'bad name.json').write_text('{"operation":"cloud_upload","args":{}}')
            private_json(requests/'good-task.json',{'operation':'cloud_upload','args':{}})
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,['upload'])
            self.assertEqual([path.name for path in (session/'worker/responses').iterdir()],['good-task.json'])
            self.assertFalse((requests/'bad name.json').exists());self.assertFalse((requests/'good-task.json').exists())

    def test_a_request_that_already_has_a_response_is_dropped_and_not_run_again(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';root=session/'worker'
            (root/'requests').mkdir(parents=True);(root/'responses').mkdir()
            request=root/'requests/test-task.json';response=root/'responses/test-task.json'
            private_json(request,{'operation':'cloud_upload','args':{}})
            private_json(response,{'task_id':'test-task','status':'ok','result':{'first':True}})
            before=response.read_bytes()
            adapter=RecordingAdapter();self.serve_one_pass(session,adapter)
            self.assertEqual(adapter.calls,[]);self.assertEqual(response.read_bytes(),before)
            self.assertFalse(request.exists())

    def test_unusable_requests_get_an_error_response_without_raw_exception_text(self):
        cases={'too large':(json.dumps({'operation':'cloud_upload','args':{'padding':'x'*17000}}),
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

    def test_worker_failing_after_startup_is_not_marked_as_a_startup_failure(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session'
            with patch('builtins.print'),patch('comic_archive.worker.TapasAdapter',return_value=RecordingAdapter()), \
                    patch('comic_archive.account_export.worker_tick'),patch('comic_archive.completion.worker_tick'), \
                    patch('comic_archive.worker.time.sleep',side_effect=ArchiveError('Safe failure')):
                with self.assertRaises(ArchiveError):run_worker(session,'firefox')
            status=json.loads((session/'worker/status.json').read_text())
            self.assertEqual(status['state'],'stopped');self.assertNotIn('last_error_code',status)
            self.assertFalse(background.running(session/'worker'))

    def test_a_failing_controller_tick_never_stops_the_worker_serving_its_queue(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            private_json(requests/'test-task.json',{'operation':'cloud_upload','args':{}})
            adapter=RecordingAdapter()
            with patch('comic_archive.account_export.worker_tick',side_effect=sqlite3.DatabaseError('PRIVATE')) as account, \
                    patch('comic_archive.completion.worker_tick',side_effect=RuntimeError('PRIVATE')) as backlog:
                self.serve_one_pass(session,adapter)
            account.assert_called_once();backlog.assert_called_once()
            self.assertEqual(adapter.calls,['upload'])
            value=json.loads((session/'worker/responses/test-task.json').read_text())
            self.assertEqual(value['status'],'ok')

    def test_a_result_that_cannot_be_saved_ends_its_task_with_a_fixed_error(self):
        class Unsaveable(RecordingAdapter):
            def upload(self,*args,**kwargs):
                self.calls.append('upload');return {'value':object()}
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session';requests=session/'worker/requests';requests.mkdir(parents=True)
            for name in ('a-task','b-task'):
                private_json(requests/f'{name}.json',{'operation':'cloud_upload','args':{}})
            adapter=Unsaveable();self.serve_one_pass(session,adapter)
            self.assertEqual(len(adapter.calls),2)
            for name in ('a-task','b-task'):
                value=json.loads((session/f'worker/responses/{name}.json').read_text())
                self.assertEqual((value['status'],value['error_code']),('error','job_failed'))
            self.assertEqual(list(requests.iterdir()),[])


if __name__ == "__main__":
    unittest.main()
