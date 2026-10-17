import json
import tempfile
import threading
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import daily
from comic_archive.daily import (configure, next_occurrence, start_scheduler, status, stop_scheduler, tick,
                                 validate_configuration)
from comic_archive.errors import LibraryBusy
from comic_archive.inventory import Inventory
from comic_archive.jobs import sync
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


def instant(value):return datetime.fromisoformat(value).astimezone(timezone.utc)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory();self.addCleanup(self.work.cleanup)
        self.session=Path(self.work.name)/'session'
        self.root=self.session/'worker'
        (self.root/'requests').mkdir(parents=True);(self.root/'responses').mkdir()
        self.worker('ready')
        self.now=instant('2026-10-02T19:00:00+00:00')
        self.addCleanup(stop_scheduler,self.session)

    def worker(self,state):
        (self.root/'status.json').write_text(json.dumps({'state':state}))

    def complete(self,task,result=None,error_code=None):
        value={'task_id':task,'status':'ok','result':result or {'status':'finished','archived_episode_ids':[],'downloaded_pages':0,'downloaded_bytes':0}}
        if error_code:value={'task_id':task,'status':'error','error_code':error_code,'error':'cookie=PRIVATE_SESSION'}
        (self.root/'responses'/(task+'.json')).write_text(json.dumps(value))
        (self.root/'requests'/(task+'.json')).unlink(missing_ok=True)
        self.worker('ready')

    def test_local_time_tracks_dst_and_runs_fold_once(self):
        self.assertEqual(next_occurrence(instant('2026-10-31T13:00:00+00:00'),'08:00','America/Chicago'),instant('2026-11-01T14:00:00+00:00'))
        self.assertEqual(next_occurrence(instant('2026-03-07T14:00:00+00:00'),'08:00','America/Chicago'),instant('2026-03-08T13:00:00+00:00'))
        self.assertEqual(next_occurrence(instant('2026-11-01T06:45:00+00:00'),'01:30','America/Chicago'),instant('2026-11-02T07:30:00+00:00'))
        self.assertEqual(next_occurrence(instant('2026-03-08T07:00:00+00:00'),'02:30','America/Chicago'),instant('2026-03-08T08:30:00+00:00'))

    def test_missed_days_coalesce_and_restart_retains_the_same_task(self):
        configure(self.session,now=self.now)
        future=instant('2026-10-06T19:00:00+00:00')
        first=tick(self.session,now=future);task=first['pending_task_id']
        self.assertIsNotNone(task)
        self.assertEqual(first['next_due'],'2026-10-07T13:00:00+00:00')
        self.assertEqual(tick(self.session,now=future)['pending_task_id'],task)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)
        self.complete(task)
        done=tick(self.session,now=future)
        self.assertIsNone(done['pending_task_id'])
        self.assertEqual(done['last_result']['status'],'finished')
        self.assertEqual(list((self.root/'requests').glob('*.json')),[])
        self.assertEqual((self.root/'daily.json').stat().st_mode&0o777,PRIVATE_FILE)

    def test_crash_between_reservation_and_publication_recovers_without_duplicate(self):
        configure(self.session,run_now=True,now=self.now)
        with patch('comic_archive.daily.submit_job',side_effect=OSError('crash')):
            with self.assertRaises(OSError):tick(self.session,now=self.now)
        reserved=status(self.session)['pending_task_id']
        self.assertIsNotNone(reserved)
        self.assertEqual(list((self.root/'requests').glob('*.json')),[])
        self.assertEqual(tick(self.session,now=self.now)['pending_task_id'],reserved)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)

    def test_crash_after_publication_and_configuration_changes_preserve_reserved_request(self):
        from comic_archive.queue_client import submit_job
        configure(self.session,run_now=True,options={'limit':1},now=self.now)
        def publish_then_crash(*args,**kwargs):
            submit_job(*args,**kwargs);raise OSError('crash')
        with patch('comic_archive.daily.submit_job',side_effect=publish_then_crash):
            with self.assertRaises(OSError):tick(self.session,now=self.now)
        reserved=status(self.session)['pending_task_id']
        configure(self.session,options={'limit':2},now=self.now)
        self.assertEqual(tick(self.session,now=self.now)['pending_task_id'],reserved)
        request=json.loads((self.root/'requests'/(reserved+'.json')).read_text())
        self.assertEqual(request['args']['limit'],1)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)

    def test_busy_worker_and_existing_manual_jobs_defer_the_due_run(self):
        configure(self.session,run_now=True,now=self.now)
        self.worker('busy')
        self.assertIsNone(tick(self.session,now=self.now)['pending_task_id'])
        self.worker('ready')
        manual=self.root/'requests/manual.json';manual.write_text('{}')
        self.assertIsNone(tick(self.session,now=self.now)['pending_task_id'])
        manual.unlink()
        self.assertIsNotNone(tick(self.session,now=self.now)['pending_task_id'])

    def test_two_controllers_reserve_at_most_one_due_job(self):
        configure(self.session,run_now=True,now=self.now)
        barrier=threading.Barrier(2);errors=[]
        def run():
            try:
                barrier.wait(timeout=2);tick(self.session,now=self.now)
            except LibraryBusy:pass
            except BaseException as error:errors.append(error)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=3);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)

    def test_session_failure_pauses_future_jobs_until_reconfigured(self):
        configure(self.session,run_now=True,now=self.now)
        task=tick(self.session,now=self.now)['pending_task_id']
        self.complete(task,error_code='login_required')
        later=instant('2026-10-06T19:00:00+00:00')
        paused=tick(self.session,now=later)
        self.assertEqual(paused['pause_reason'],'login_required')
        self.assertNotIn('PRIVATE',json.dumps(paused))
        self.assertIsNone(tick(self.session,now=later)['pending_task_id'])
        configure(self.session,run_now=True,now=later)
        self.assertIsNotNone(tick(self.session,now=later)['pending_task_id'])

    def test_disable_retains_queued_work_and_prevents_new_runs(self):
        configure(self.session,run_now=True,now=self.now)
        task=tick(self.session,now=self.now)['pending_task_id']
        configure(self.session,enabled=False,now=self.now)
        self.assertTrue((self.root/'requests'/(task+'.json')).is_file())
        self.complete(task)
        value=tick(self.session,now=instant('2026-10-10T19:00:00+00:00'))
        self.assertFalse(value['enabled']);self.assertIsNone(value['pending_task_id'])
        self.assertFalse(start_scheduler(self.session))

    def test_invalid_options_and_private_values_fail_closed(self):
        for args in ({'options':{'free_only':False}},{'options':{'unlock':True}},{'output':'/tmp/outside'},
                     {'timezone':'UTC\nPRIVATE'},{'at':'29:00'},{'options':{'max_seconds':10**1000}},
                     {'options':{'request_interval':float('nan')}},{'enabled':'yes'},{'enabled':False,'run_now':True}):
            with self.subTest(args=args),self.assertRaises(ArchiveError):validate_configuration(args)
        configure(self.session,now=self.now)
        value=json.loads((self.root/'daily.json').read_text())
        value['last_result']={'task_id':'PRIVATE','status':'PRIVATE','completed_at':'PRIVATE'}
        (self.root/'daily.json').write_text(json.dumps(value))
        with self.assertRaises(ArchiveError):status(self.session)

    def test_scheduler_thread_enqueues_without_browser_calls_and_stops(self):
        configure(self.session,run_now=True,now=self.now)
        queued=threading.Event()
        real_tick=tick
        def observe(*args,**kwargs):
            value=real_tick(*args,now=self.now)
            if value['pending_task_id']:queued.set()
            return value
        with patch('comic_archive.daily.tick',side_effect=observe):
            self.assertTrue(start_scheduler(self.session))
            self.assertTrue(queued.wait(timeout=3))
            self.assertTrue(start_scheduler(self.session))
            self.assertTrue(status(self.session)['controller_running'])
            stop_scheduler(self.session)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)
        self.assertIsNone(status(self.session)['controller_running'])

    def test_ticking_an_unconfigured_session_reserves_and_creates_nothing(self):
        self.assertEqual(tick(self.session,now=self.now),{'configured':False,'enabled':False,'scope':'free_only'})
        self.assertFalse((self.root/'daily.json').exists());self.assertFalse((self.root/'.daily-state').exists())
        self.assertEqual(list((self.root/'requests').glob('*.json')),[])

    def test_unknown_timezone_and_out_of_range_scan_limits_are_refused_without_a_schedule(self):
        with self.assertRaisesRegex(ArchiveError,'unavailable'):configure(self.session,zone='Mars/Phobos',now=self.now)
        for limit in (0,1001,True,'5'):
            with self.subTest(limit=limit),self.assertRaises(ArchiveError):configure(self.session,options={'scan_limit':limit},now=self.now)
        self.assertFalse((self.root/'daily.json').exists())
        self.assertEqual(configure(self.session,options={'scan_limit':5},now=self.now)['options']['scan_limit'],5)
        self.assertNotIn('scan_limit',configure(self.session,options={'scan_limit':None},now=self.now)['options'])

    def test_damaged_schedule_files_fail_closed_and_are_never_rewritten(self):
        configure(self.session,run_now=True,now=self.now)
        good=json.loads((self.root/'daily.json').read_text())
        task='job-'+'a'*24
        pending={'task_id':task,'due':good['next_due'],'options':good['options']}
        last={'task_id':task,'status':'finished','completed_at':good['next_due']}
        def mutated(**changes):return json.dumps(good|changes)
        cases={'not json':'{nope','a list':'[1, 2]','too large':json.dumps({'padding':'x'*16400}),
               'future schema':mutated(schema_version=2),'unknown pause':mutated(pause_reason='whatever'),
               'unparseable due':mutated(next_due='tomorrow'),'non-string due':mutated(next_due=5),
               'zone-less due':mutated(next_due='2026-10-02T19:00:00'),
               'bad task id':mutated(pending=dict(pending,task_id='../../escape')),'pending not a dict':mutated(pending='job'),
               'pending without options':mutated(pending={'task_id':task,'due':good['next_due']}),
               'pending with unfixed options':mutated(pending=dict(pending,options={'free_only':False})),
               'pending bad due':mutated(pending=dict(pending,due='soon')),
               'negative metric':mutated(last_result=dict(last,downloaded_pages=-1)),
               'result bad status':mutated(last_result=dict(last,status='PRIVATE')),
               'result bad id':mutated(last_result=dict(last,task_id='PRIVATE'))}
        path=self.root/'daily.json'
        for name,text in cases.items():
            with self.subTest(name):
                path.write_text(text)
                for action in (lambda:status(self.session),lambda:tick(self.session,now=self.now),lambda:configure(self.session,now=self.now)):
                    with self.assertRaises(ArchiveError):action()
                self.assertEqual(path.read_text(),text)
        self.assertEqual(list((self.root/'requests').glob('*.json')),[])
        for name in ('directory','symlink'):
            with self.subTest(name):
                path.unlink(missing_ok=True)
                if name=='directory':path.mkdir()
                else:
                    other=Path(self.work.name)/'elsewhere.json';other.write_text(mutated());path.symlink_to(other)
                for action in (lambda:status(self.session),lambda:tick(self.session,now=self.now),lambda:configure(self.session,now=self.now)):
                    with self.assertRaises(ArchiveError):action()
                if name=='directory':self.assertEqual(list(path.iterdir()),[])
                else:self.assertEqual(other.read_text(),mutated())
                if name=='directory':path.rmdir()

    def test_disabling_before_the_reserved_job_was_published_drops_the_reservation(self):
        configure(self.session,run_now=True,now=self.now)
        with patch('comic_archive.daily.submit_job',side_effect=OSError('crash')):
            with self.assertRaises(OSError):tick(self.session,now=self.now)
        self.assertIsNotNone(status(self.session)['pending_task_id'])
        configure(self.session,enabled=False,now=self.now)
        self.assertIsNotNone(status(self.session)['pending_task_id'])
        with patch('comic_archive.daily.submit_job') as submit:
            value=tick(self.session,now=self.now)
        submit.assert_not_called()
        self.assertIsNone(value['pending_task_id']);self.assertIsNone(status(self.session)['pending_task_id'])
        self.assertEqual(list((self.root/'requests').glob('*.json')),[])

    def scheduler_thread(self):
        return daily._controllers[str(self.session.absolute())][0]

    def test_scheduler_stops_by_itself_when_its_worker_has_stopped(self):
        configure(self.session,run_now=True,now=self.now);self.worker('stopped')
        with patch('comic_archive.daily.tick') as ticked:
            self.assertTrue(start_scheduler(self.session))
            self.scheduler_thread().join(timeout=5)
            self.assertFalse(self.scheduler_thread().is_alive())
        ticked.assert_not_called();self.assertEqual(list((self.root/'requests').glob('*.json')),[])
        self.assertFalse(status(self.session)['controller_running'])

    def test_scheduler_survives_a_busy_state_lock_and_enqueues_on_the_next_pass(self):
        class QuickEvent(threading.Event):
            def wait(self,timeout=None):return super().wait(0.01)
        configure(self.session,run_now=True,now=self.now)
        calls=[];queued=threading.Event();real=daily.tick
        def flaky(session):
            calls.append(1)
            if len(calls)==1:raise LibraryBusy('held by a configuration writer')
            value=real(session,now=self.now)
            if value['pending_task_id']:queued.set()
            return value
        with patch('comic_archive.daily.threading',SimpleNamespace(Event=QuickEvent,Thread=threading.Thread)),patch('comic_archive.daily.tick',side_effect=flaky):
            self.assertTrue(start_scheduler(self.session))
            self.assertTrue(queued.wait(timeout=5))
            stop_scheduler(self.session)
        self.assertGreaterEqual(len(calls),2)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)
        self.assertIsNone(status(self.session)['pause_reason'])

    def test_scheduler_failure_pauses_the_schedule_and_stops_until_reconfigured(self):
        configure(self.session,now=self.now)
        with patch('comic_archive.daily.tick',side_effect=OSError('PRIVATE disk detail')):
            self.assertTrue(start_scheduler(self.session))
            self.scheduler_thread().join(timeout=5)
            self.assertFalse(self.scheduler_thread().is_alive())
        value=status(self.session)
        self.assertEqual(value['pause_reason'],'scheduler_failed');self.assertFalse(value['controller_running'])
        self.assertNotIn('PRIVATE',json.dumps(value))
        paused=tick(self.session,now=instant('2026-10-09T19:00:00+00:00'))
        self.assertIsNone(paused['pending_task_id'])
        self.assertIsNone(configure(self.session,now=self.now)['pause_reason'])

    @patch('builtins.print')
    def test_daily_jobs_preserve_new_free_episodes_keep_expired_archives_and_skip_repeat_images(self,output):
        library=Path(self.work.name)/'Library';adapter=AccountAdapter()
        configure(self.session,run_now=True,now=self.now)
        first=tick(self.session,now=self.now)['pending_task_id']
        request=json.loads((self.root/'requests'/(first+'.json')).read_text())
        options=dict(request['args']);options.pop('request_interval')
        result=sync(adapter,library,**options)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.complete(first,result);tick(self.session,now=self.now)
        adapter.access['81']='unavailable_or_unverified';adapter.access['83']='tapas_free'
        second_time=instant('2026-10-03T13:00:01+00:00')
        second=tick(self.session,now=second_time)['pending_task_id']
        result=sync(adapter,library,**options)
        self.assertEqual(result['archived_episode_ids'],['83'])
        self.complete(second,result);tick(self.session,now=second_time)
        third_time=instant('2026-10-04T13:00:01+00:00')
        third=tick(self.session,now=third_time)['pending_task_id']
        result=sync(adapter,library,**options)
        self.assertEqual(result['downloaded_pages'],0)
        self.assertEqual(result['archived_episode_ids'],[])
        self.complete(third,result);tick(self.session,now=third_time)
        self.assertEqual(len(list(library.glob('*/*.cbz'))),2)
        self.assertNotIn(('capture','82'),adapter.calls)
        with Inventory(library,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status']['archived'],2)

    @patch('builtins.print')
    def test_limited_daily_jobs_resume_cached_pages_on_the_next_day(self,output):
        library=Path(self.work.name)/'Library';adapter=AccountAdapter()
        configure(self.session,run_now=True,options={'max_pages':1},now=self.now)
        first=tick(self.session,now=self.now)['pending_task_id']
        options=json.loads((self.root/'requests'/(first+'.json')).read_text())['args'];options.pop('request_interval')
        result=sync(adapter,library,**options)
        self.assertEqual(result['status'],'limited');self.assertEqual(adapter.downloads,[1])
        self.complete(first,result);tick(self.session,now=self.now)
        later=instant('2026-10-03T13:00:01+00:00')
        second=tick(self.session,now=later)['pending_task_id']
        result=sync(adapter,library,**options)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertEqual(adapter.downloads,[1,2])
        self.complete(second,result);tick(self.session,now=later)


if __name__=='__main__':unittest.main()
