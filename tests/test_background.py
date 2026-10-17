from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE
from comic_archive import background, osdep, worker
from comic_archive.locking import LibraryLock
from comic_archive.models import ArchiveError


class JumpingClock:
    """Stands in for the time module: every reading moves on, so deadlines pass without waiting."""
    def __init__(self, step=20):
        self.now = 0
        self.step = step
    def monotonic(self):
        self.now += self.step
        return self.now
    def sleep(self, seconds):
        pass


class BackgroundTests(unittest.TestCase):
    def record(self, session, **fields):
        root = session / "worker"
        root.mkdir(parents=True, exist_ok=True)
        worker.private_json(root / "status.json", {"state": "ready", "headless": True,
            "worker_id": "a" * 32, "pid": 999, **fields})
        return root

    def test_status_is_read_only_and_a_stale_status_does_not_prove_running(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            self.assertFalse(background.status(session)["running"])
            self.assertFalse(session.exists())
            root = self.record(session, state="busy", cookies="PRIVATE")
            result = background.status(session)
            self.assertEqual(result["state"], "stopped")
            self.assertNotIn("PRIVATE", json.dumps(result))
            background.stop(session)
            self.assertFalse((root / "drain.json").exists())

    def test_duplicate_start_does_not_launch_or_modify_an_existing_worker(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            root = self.record(session)
            before = (root / "status.json").read_bytes()
            with LibraryLock(root / ".runtime"), patch.object(background.subprocess, "Popen") as launch:
                result = background.start(session)
            self.assertTrue(result["already_running"])
            launch.assert_not_called()
            self.assertEqual(before, (root / "status.json").read_bytes())

    def test_stop_uses_a_runtime_scoped_marker_and_never_signals_a_process(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            root = self.record(session, state="busy")
            with LibraryLock(root / ".runtime"), patch.object(os, "kill") as signal:
                result = background.stop(session)
            self.assertTrue(result["stop_requested"])
            self.assertEqual(json.loads((root / "drain.json").read_text())["worker_id"], "a" * 32)
            self.assertEqual((root / "drain.json").stat().st_mode & 0o777, PRIVATE_FILE)
            signal.assert_not_called()
            self.assertFalse(background.drain_requested(root, "b" * 32))

    def test_old_worker_is_not_stopped_or_relabelled_as_new(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            root = self.record(session)
            (root / "status.json").write_text(json.dumps({"state": "busy", "worker_version": 34}))
            with LibraryLock(root / ".runtime"):
                self.assertFalse(background.status(session)["stop_supported"])
                with self.assertRaisesRegex(ArchiveError, "older worker"):
                    background.stop(session)
            self.assertFalse((root / "drain.json").exists())

    def test_start_uses_only_fixed_detached_headless_command_and_private_log(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            process = Mock(pid=456)
            process.poll.return_value = None
            runtime = None
            def launch(command, **kwargs):
                nonlocal runtime
                self.assertEqual(command, [sys.executable, "-m", "comic_archive", "--session-dir", str(session),
                    "--browser", "firefox", "--headless", "--request-interval", "0.5",
                    "browser-worker", "--stable-code"])
                self.assertEqual(kwargs, kwargs | osdep.detached())  # Its own session: it outlives the app.
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["cwd"], Path(work))
                runtime = LibraryLock(session / "worker/.runtime")
                runtime.__enter__()
                self.record(session, pid=456)
                return process
            try:
                with patch("comic_archive.session.load_session"), \
                        patch.object(background.subprocess, "Popen", side_effect=launch):
                    result = background.start(session, "firefox", .5)
                self.assertTrue(result["running"])
                self.assertTrue(result["headless"])
                self.assertNotIn('authenticated',result)
                self.assertEqual((session / "worker/background.log").stat().st_mode & 0o777, PRIVATE_FILE)
            finally:
                if runtime:
                    runtime.__exit__(None, None, None)

    def test_the_built_in_engine_can_be_chosen_for_the_detached_worker(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            process = Mock(pid=456)
            process.poll.return_value = None
            runtime = None
            commands = []
            def launch(command, **kwargs):
                nonlocal runtime
                commands.append(command)
                runtime = LibraryLock(session / "worker/.runtime")
                runtime.__enter__()
                self.record(session, pid=456)
                return process
            try:
                with patch("comic_archive.session.load_session"), \
                        patch.object(background.subprocess, "Popen", side_effect=launch):
                    background.start(session, "qt", 1.0)
                command, = commands
                self.assertEqual(command[command.index("--browser") + 1], "qt")
            finally:
                if runtime:
                    runtime.__exit__(None, None, None)
        with self.assertRaises(ArchiveError):
            background.start(Path("unused"), "webkit", 1.0)

    def test_legacy_active_status_without_runtime_lock_blocks_duplicate_start(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = session / 'worker'
            root.mkdir(parents=True)
            (root / 'status.json').write_text(json.dumps({'state':'busy','worker_version':34}))
            with patch.object(background.subprocess,'Popen') as launch:
                result = background.start(session)
            self.assertTrue(result['start_deferred'])
            self.assertIsNone(result['running'])
            self.assertEqual(result['liveness'],'legacy_status_only')
            launch.assert_not_called()
            with self.assertRaises(ArchiveError):
                background.stop(session)
            self.assertFalse((root / 'drain.json').exists())

    def test_graceful_stop_finishes_current_receipt_and_preserves_next_request(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            root = self.record(session)
            requests = root / "requests"
            requests.mkdir()
            names = ["job-" + f"{number:024x}" for number in (1, 2)]
            for name in names:
                worker.private_json(requests / f"{name}.json", {"operation": "check_session", "args": {}})
            adapter = Mock()
            adapter.__enter__ = Mock(return_value=adapter)
            adapter.__exit__ = Mock(return_value=False)
            def execute(*args, **kwargs):
                self.assertTrue(background.stop(session)["stop_requested"])
                return {"authenticated": True}
            with patch.object(worker, "TapasAdapter", return_value=adapter), \
                    patch.object(worker, "execute", side_effect=execute) as task, \
                    patch("comic_archive.daily.start_scheduler"), patch("comic_archive.daily.stop_scheduler"), \
                    patch("builtins.print"):
                worker.run_worker(session, headless=True, reload_adapter=False)
            self.assertEqual(task.call_count, 1)
            self.assertTrue((root / "responses" / f"{names[0]}.json").is_file())
            self.assertFalse((requests / f"{names[0]}.json").exists())
            self.assertTrue((requests / f"{names[1]}.json").is_file())
            self.assertEqual(background.status(session)["state"], "stopped")
            self.assertFalse((root / "drain.json").exists())

    def test_startup_failure_has_safe_status_and_releases_runtime_lock(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / ".comic-session"
            adapter = Mock()
            adapter.__enter__ = Mock(side_effect=ArchiveError("Safe startup failure"))
            adapter.__exit__ = Mock()
            with patch.object(worker, "TapasAdapter", return_value=adapter), \
                    patch("comic_archive.daily.stop_scheduler"):
                with self.assertRaises(ArchiveError):
                    worker.run_worker(session, headless=True)
            result = background.status(session)
            self.assertFalse(result["running"])
            self.assertEqual(result["last_error_code"], "startup_failed")

    def test_real_process_lock_and_cooperative_stop_without_browser_or_source_requests(self):
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from comic_archive import worker
class OfflineAdapter:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
worker.TapasAdapter = OfflineAdapter
worker.run_worker(Path(sys.argv[1]), headless=True, reload_adapter=False)
'''
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            session = Path(work) / ".comic-session"
            process = subprocess.Popen([sys.executable, "-c", code, str(session),
                str(Path(__file__).resolve().parents[1])], cwd=work,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            try:
                # Match background.start's bounded 30-second startup window;
                # a shared builder can acquire the runtime lock before its
                # initial status fsync completes. Readiness remains required.
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    observed = background.status(session)
                    if observed["running"] and observed["state"] == "ready":
                        break
                    time.sleep(.05)
                self.assertTrue(observed["running"])
                self.assertEqual(observed["state"], "ready")
                # Use the documented bounded drain window. The shared builders
                # can stall on fsync; five seconds was an incidental test SLA.
                stopped = background.stop(session, wait_seconds=30)
                if stopped.get('stop_wait_expired'):
                    log.seek(0)
                    self.fail('Idle worker did not drain within 30 seconds. Synthetic worker log: '+log.read().decode(errors='replace')[-2000:])
                self.assertEqual(process.wait(timeout=30), 0)
                self.assertFalse(background.status(session)["running"])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=30)

    def test_background_probe_rejects_credentials_paths_and_non_episode_urls_before_queueing(self):
        from comic_archive.queue_client import submit_inspection
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            requests = session / 'worker/requests'
            requests.mkdir(parents=True)
            for options in ({'url':'https://example.com/episode/81'},
                    {'url':'https://tapas.io/episode/81?token=PRIVATE'},
                    {'url':'https://user:PRIVATE@tapas.io/episode/81'},
                    {'url':'https://tapas.io/episode/81','cookies':'PRIVATE'},
                    {'url':'https://tapas.io/episode/81','output':'/tmp/outside'}):
                with self.subTest(options=options), self.assertRaises(ArchiveError):
                    submit_inspection(session,'diagnose_headless',options)
            self.assertEqual(list(requests.iterdir()),[])
            adapter = Mock()
            with self.assertRaises(ArchiveError):
                worker.execute(adapter,{'operation':'diagnose_headless','args':{'url':'https://example.com/'}})
            adapter.diagnose_headless.assert_not_called()

    def test_saved_backlog_resume_uses_fixed_resume_command_and_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            checkpoint = root / 'backlog.json'
            checkpoint.write_text('ORIGINAL BUDGETS')
            process = Mock()
            process.poll.return_value = None
            controller = LibraryLock(root / '.backlog-runtime')
            def launch_controller(*args, **kwargs):
                controller.__enter__()
                return process
            try:
                with LibraryLock(root / '.runtime'), \
                        patch.object(background.subprocess,'Popen',side_effect=launch_controller) as launch, \
                        patch('comic_archive.batches.status',return_value={'configured':True}):
                    result = background.start(session,resume=True)
            finally:
                controller.__exit__(None,None,None)
            self.assertTrue(result['backlog_resume_started'])
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0],[sys.executable,'-m','comic_archive',
                '--session-dir',str(session),'run-backlog','--resume'])
            self.assertEqual(launch.call_args.kwargs, launch.call_args.kwargs | osdep.detached())
            self.assertEqual(checkpoint.read_text(),'ORIGINAL BUDGETS')

    def test_failed_controller_launch_is_not_reported_as_a_successful_resume(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            process = Mock()
            process.poll.return_value = 1
            with LibraryLock(root / '.runtime'), \
                    patch.object(background.subprocess,'Popen',return_value=process), \
                    patch('comic_archive.batches.status',return_value={'configured':True}):
                result = background.start(session,resume=True)
            self.assertTrue(result['running'])
            self.assertTrue(result['backlog_resume_failed'])
            self.assertNotIn('backlog_resume_started',result)

    def test_real_detached_controller_accepts_project_absolute_session_and_keeps_expired_budget(self):
        from datetime import datetime,timedelta,timezone
        from comic_archive import batches
        with tempfile.TemporaryDirectory() as work:
            project = Path(work)
            session = project / '.comic-session'
            root = self.record(session,worker_version=36)
            for name in ('requests','responses'):
                (root / name).mkdir()
            state = batches._start(root,None,False)
            expired = datetime.now(timezone.utc)-timedelta(minutes=1)
            state['submit_before'] = expired.isoformat()
            state['started_at'] = (expired-timedelta(seconds=state['budgets']['max_seconds'])).isoformat()
            state['status'] = 'stopped'
            state['stop_requested'] = True
            batches._save(root,state)
            processes = []
            popen = subprocess.Popen
            def launch(*args, **kwargs):
                child = popen(*args,**kwargs)
                processes.append(child)
                return child
            try:
                with LibraryLock(root / '.runtime'), \
                        patch.object(background.subprocess,'Popen',side_effect=launch):
                    result = background.start(session,resume=True)
                    self.assertTrue(result.get('backlog_resume_started') or result.get('backlog_resume_finished'))
                    self.assertEqual(processes[0].wait(timeout=5),0)
                saved = batches.status(session,project / 'Library')
                self.assertEqual(saved['status'],'budget_reached')
                self.assertEqual(saved['started_at'],state['started_at'])
                self.assertEqual(saved['submit_before'],state['submit_before'])
                self.assertEqual(saved['budgets'],state['budgets'])
                self.assertEqual(saved['submitted_batches'],0)
                self.assertEqual(list((root / 'requests').iterdir()),[])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)

    def test_resume_does_not_create_a_second_backlog_controller(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            with LibraryLock(root / '.runtime'), LibraryLock(root / '.backlog-runtime'), \
                    patch.object(background.subprocess,'Popen') as launch, \
                    patch('comic_archive.batches.status',return_value={'configured':True}):
                result = background.start(session,resume=True)
            self.assertTrue(result['backlog_controller_already_running'])
            launch.assert_not_called()

    def test_real_worker_restart_waits_for_the_old_lock_and_launches_a_new_headless_process(self):
        code='''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from comic_archive import worker
class OfflineAdapter:
    def __init__(self,*args,**kwargs): pass
    def __enter__(self): return self
    def __exit__(self,*args): pass
worker.TapasAdapter=OfflineAdapter
worker.run_worker(Path(sys.argv[1]),headless=True,reload_adapter=False)
'''
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'.comic-session'
            popen=subprocess.Popen
            processes=[]
            def launch(*args,**kwargs):
                child=popen([sys.executable,'-c',code,str(session),
                    str(Path(__file__).resolve().parents[1])],cwd=work,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                processes.append(child)
                return child
            old=launch()
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if background.status(session)['state']=='ready':break
                    time.sleep(.05)
                self.assertTrue(background.status(session)['running'])
                checkpoint=session/'worker/preserved.json'
                checkpoint.write_text('UNCHANGED CHECKPOINT')
                with patch('comic_archive.session.load_session'), \
                        patch.object(background.subprocess,'Popen',side_effect=launch) as start:
                    result=background.start(session,restart=True)
                self.assertEqual(old.wait(timeout=5),0)
                self.assertEqual(len(processes),2)
                self.assertTrue(result['running'])
                self.assertTrue(result['headless'])
                self.assertFalse(result['already_running'])
                self.assertIn('--stable-code',start.call_args.args[0])
                self.assertEqual(checkpoint.read_text(),'UNCHANGED CHECKPOINT')
                observed=json.loads((session/'worker/status.json').read_text())
                self.assertEqual(observed['pid'],processes[1].pid)
                self.assertNotEqual(observed['pid'],old.pid)
                background.stop(session)
                self.assertEqual(processes[1].wait(timeout=5),0)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)

    def test_start_does_not_resume_backlog_while_a_stop_is_draining_the_worker(self):
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'.comic-session'
            root=self.record(session,worker_version=37)
            with LibraryLock(root/'.runtime'):
                background.stop(session)
                with patch.object(background.subprocess,'Popen') as launch, \
                        patch('comic_archive.batches.status',return_value={'configured':True}):
                    result=background.start(session,resume=True)
            self.assertTrue(result['start_deferred'])
            self.assertEqual(result['reason'],'worker_draining')
            launch.assert_not_called()


    def test_unusual_session_or_worker_directories_are_refused(self):
        with tempfile.TemporaryDirectory() as work:
            project = Path(work)
            elsewhere = project / 'elsewhere'
            elsewhere.mkdir()
            (project / 'other-name').mkdir()
            with self.assertRaises(ArchiveError):
                background.status(project / 'other-name')
            (project / 'linked').mkdir()
            (project / 'linked/.comic-session').symlink_to(elsewhere)
            with self.assertRaises(ArchiveError):
                background.status(project / 'linked/.comic-session')
            session = project / 'real/.comic-session'
            session.mkdir(parents=True)
            (session / 'worker').symlink_to(elsewhere)
            for action in (background.status, background.stop, background.start):
                with self.subTest(action=action.__name__), self.assertRaises(ArchiveError):
                    action(session)
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unreadable_worker_state_is_refused_and_no_stop_marker_is_written(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            status = root / 'status.json'
            outside = Path(work) / 'outside.json'
            outside.write_text(json.dumps({'state': 'ready', 'worker_version': 71, 'worker_id': 'a' * 32}))
            contents = {'broken': '{broken', 'not an object': '[]',
                        'oversized': json.dumps({'state': 'ready', 'padding': 'x' * 17000})}
            with LibraryLock(root / '.runtime'):
                for name, text in contents.items():
                    with self.subTest(name):
                        status.write_text(text)
                        for action in (background.status, background.stop):
                            with self.assertRaises(ArchiveError):
                                action(session)
                with self.subTest('symlink'):
                    status.unlink()
                    status.symlink_to(outside)
                    for action in (background.status, background.stop):
                        with self.assertRaises(ArchiveError):
                            action(session)
            self.assertFalse((root / 'drain.json').exists())

    def test_symlinked_runtime_folder_is_not_followed_to_decide_whether_a_worker_runs(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            outside = Path(work) / 'elsewhere'
            (root / '.runtime').symlink_to(outside)
            with LibraryLock(outside):
                with self.assertRaises(ArchiveError):
                    background.status(session)

    def test_stop_that_loses_a_race_with_the_worker_leaving_writes_no_marker(self):
        from comic_archive.task_cancellation import admission_lock
        for event in ('worker exits', 'older worker replaces it'):
            with self.subTest(event), tempfile.TemporaryDirectory() as work:
                session = Path(work) / '.comic-session'
                root = self.record(session)
                runtime = LibraryLock(root / '.runtime')
                runtime.__enter__()
                self.addCleanup(runtime.__exit__, None, None, None)
                @contextmanager
                def racing(session_directory):
                    with admission_lock(session_directory):
                        # Between the first check and taking the admission lock.
                        if event == 'worker exits':
                            runtime.__exit__(None, None, None)
                        else:
                            (root / 'status.json').write_text(json.dumps({'state': 'busy', 'worker_version': 34}))
                        yield
                with patch('comic_archive.task_cancellation.admission_lock', racing):
                    if event == 'worker exits':
                        self.assertIs(background.stop(session)['running'], False)
                    else:
                        with self.assertRaisesRegex(ArchiveError, 'older worker'):
                            background.stop(session)
                self.assertFalse((root / 'drain.json').exists())
                runtime.__exit__(None, None, None)  # Before the folder goes: Windows keeps a locked file in place.

    def test_resume_needs_a_saved_backlog_and_starts_nothing_without_one(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            (root / 'requests').mkdir()
            (root / 'responses').mkdir()
            with patch.object(background.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ArchiveError, 'No saved backlog'):
                    background.start(session, resume=True)
            launch.assert_not_called()

    def test_restart_that_cannot_see_the_old_worker_leave_reports_it_as_draining(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            with LibraryLock(root / '.runtime'), patch.object(background, 'time', JumpingClock()), \
                    patch.object(background.subprocess, 'Popen') as launch:
                result = background.start(session, restart=True)
            self.assertTrue(result['start_deferred'])
            self.assertEqual(result['reason'], 'worker_draining')
            self.assertTrue(result['running'])
            self.assertEqual(json.loads((root / 'drain.json').read_text())['worker_id'], 'a' * 32)
            launch.assert_not_called()

    def test_start_never_writes_through_a_symlinked_log(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session, state='stopped')
            outside = Path(work) / 'outside.log'
            outside.write_text('KEEP')
            (root / 'background.log').symlink_to(outside)
            with patch('comic_archive.session.load_session'), \
                    patch.object(background.subprocess, 'Popen') as launch:
                with self.assertRaises(ArchiveError):
                    background.start(session)
            launch.assert_not_called()
            self.assertEqual(outside.read_text(), 'KEEP')

    def test_start_reports_a_worker_that_cannot_launch_or_exits_during_startup(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            self.record(session, state='stopped')
            with patch('comic_archive.session.load_session'):
                with patch.object(background.subprocess, 'Popen', side_effect=OSError('PRIVATE')):
                    with self.assertRaises(ArchiveError) as caught:
                        background.start(session)
                    self.assertNotIn('PRIVATE', str(caught.exception))
                crashed = Mock(pid=456)
                crashed.poll.return_value = 3
                with patch.object(background.subprocess, 'Popen', return_value=crashed):
                    result = background.start(session)
                self.assertTrue(result['start_failed'])
                self.assertIs(result['running'], False)

    def test_start_that_never_sees_the_worker_ready_reports_startup_pending(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            self.record(session, state='stopped')
            slow = Mock(pid=456)
            slow.poll.return_value = None
            with patch('comic_archive.session.load_session'), patch.object(background, 'time', JumpingClock()), \
                    patch.object(background.subprocess, 'Popen', return_value=slow):
                result = background.start(session)
            self.assertTrue(result['startup_pending'])
            self.assertIs(result['running'], False)
            self.assertNotIn('already_running', result)

    def test_backlog_resume_is_only_attempted_for_a_running_worker_and_never_through_a_symlinked_log(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            stopped = {'running': False, 'state': 'stopped'}
            with patch.object(background.subprocess, 'Popen') as launch:
                self.assertEqual(background.resume_backlog(root, stopped), stopped)
                outside = Path(work) / 'outside.log'
                outside.write_text('KEEP')
                (root / 'background.log').symlink_to(outside)
                with self.assertRaises(ArchiveError):
                    background.resume_backlog(root, {'running': True})
            launch.assert_not_called()
            self.assertEqual(outside.read_text(), 'KEEP')

    def test_backlog_controller_that_cannot_launch_or_stays_silent_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as work:
            session = Path(work) / '.comic-session'
            root = self.record(session)
            with patch.object(background.subprocess, 'Popen', side_effect=OSError('PRIVATE')):
                with self.assertRaises(ArchiveError) as caught:
                    background.resume_backlog(root, {'running': True})
            self.assertNotIn('PRIVATE', str(caught.exception))
            silent = Mock()
            silent.poll.return_value = None
            # A small step lets the wait loop run before the window closes.
            with patch.object(background, 'time', JumpingClock(2)), \
                    patch.object(background.subprocess, 'Popen', return_value=silent):
                result = background.resume_backlog(root, {'running': True})
            silent.poll.assert_called()
            self.assertEqual(result, {'running': True, 'backlog_startup_pending': True})


if __name__ == "__main__":
    unittest.main()
