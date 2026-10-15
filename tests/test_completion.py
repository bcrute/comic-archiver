from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import batches, completion
from comic_archive.archive import atomic_json
from comic_archive.cli import parser,run
from comic_archive.locking import LibraryLock
from comic_archive.models import ArchiveError


class CompletionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.project=Path(tmp.name);self.library=self.project/'Library';self.library.mkdir()
        self.session=self.project/'.comic-session';self.root=self.session/'worker'
        (self.root/'requests').mkdir(parents=True);(self.root/'responses').mkdir()
        atomic_json(self.root/'status.json',{'state':'ready','cookie':'PRIVATE'})
        self.window={'max_batches':1,'max_pages':2,'max_bytes':20,'batch_pages':2,'batch_bytes':20,
                     'recover_network':False,'archive_only':True,'refresh_lists':False}

    def configure(self,values=None,**options):
        return completion.configure(self.session,self.library,values,window_budgets=self.window,**options)

    def tick(self):return completion.tick(self.session,self.library)

    def publish(self):
        for _ in range(12):
            self.tick()
            requests=list((self.root/'requests').glob('*.json'))
            if requests:
                self.assertEqual(len(requests),1)
                return requests[0].stem,json.loads(requests[0].read_text())
        self.fail('No bounded job was published')

    def finish(self,task,*,status='limited',pending=5,error=None):
        result={'status':status,'downloaded_pages':1,'downloaded_bytes':10,'pending_episode_count':pending,
                'archived_episode_ids':['81'],'inventory':{'scan_remaining_series_count':0},'cookie':'PRIVATE'}
        value={'task_id':task,'status':'ok','result':result}
        if error:value={'task_id':task,'status':'error','error_code':error,'error':'PRIVATE'}
        atomic_json(self.root/'responses'/f'{task}.json',value)
        (self.root/'requests'/f'{task}.json').unlink()
        return self.tick()

    def test_two_windows_cap_total_usage_without_resetting_or_new_discovery(self):
        self.configure({'max_windows':2,'max_pages':3,'max_bytes':30})
        first,request=self.publish();self.assertEqual(request['operation'],'archive_discovered')
        self.finish(first)
        second,request=self.publish();self.assertNotEqual(second,first)
        self.assertEqual(request['args']['max_pages'],2);self.assertEqual(request['args']['max_bytes'],20)
        self.finish(second)
        for _ in range(4):value=self.tick()
        self.assertEqual(value['status'],'budget_reached');self.assertEqual(value['window_count'],2)
        self.assertEqual(value['downloaded_pages'],2);self.assertEqual(value['downloaded_bytes'],20)
        self.assertEqual(value['archived_episodes'],2);self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        self.assertNotIn('PRIVATE',(self.root/'completion.json').read_text())
        self.assertEqual((self.root/'completion.json').stat().st_mode & 0o777,PRIVATE_FILE)

    def test_until_drained_renews_past_aggregate_caps_and_stops_on_empty_queue(self):
        self.configure({'max_windows':1,'max_days':1,'max_pages':1,'max_bytes':10},until_drained=True)
        original=completion.status(self.session,self.library)
        self.assertTrue(original['until_drained'])
        self.assertIsNone(original['submit_before'])
        for key in completion.AGGREGATE_LIMITS:self.assertIsNone(original['limits'][key])
        for index in range(3):
            task,request=self.publish()
            self.assertEqual(request['args']['max_pages'],2)
            self.assertEqual(request['args']['max_bytes'],20)
            self.assertLessEqual(request['args']['max_seconds'],self.window.get('batch_seconds',batches.DEFAULTS['batch_seconds']))
            self.finish(task,pending=0 if index==2 else 5,status='finished' if index==2 else 'limited')
        value=self.tick()
        self.assertEqual(value['status'],'known_backlog_drained')
        self.assertEqual(value['window_count'],3)
        self.assertEqual(value['downloaded_pages'],3)
        self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_removing_limits_preserves_spent_usage_reservations_and_recoveries(self):
        self.configure({'max_windows':1});task,_=self.publish();self.finish(task)
        for _ in range(4):value=self.tick()
        self.assertEqual(value['status'],'budget_reached')
        window=batches._load(self.root)
        window.update(network_recovery_attempts=2,source_recovery_attempts=3,
                      source_recovery_delay=120,unreported_reserved_pages=2000,
                      unreported_reserved_bytes=1024**3)
        batches._save(self.root,window)
        plan=completion._load(self.root)
        plan['windows'][-1]=completion._record(window)
        prior={'started_at':(batches._now()-timedelta(days=1)).isoformat(),
               **{key:0 for key in completion.METRICS}}
        prior['network_recovery_attempts']=2
        plan['windows'].insert(0,prior);plan['limits']['max_windows']=2
        completion._save(self.root,plan)
        self.tick()
        before=completion._totals(completion._load(self.root))
        value=self.configure(until_drained=True)
        self.assertEqual(completion._totals(completion._load(self.root)),before)
        self.assertEqual(value['network_recovery_attempts'],4)
        self.assertEqual(value['source_recovery_attempts'],3)
        next_task,request=self.publish()
        self.assertNotEqual(next_task,task)
        successor=batches._load(self.root)
        self.assertFalse(successor['budgets']['recover_network'])
        self.assertEqual(successor['source_recovery_delay'],120)
        self.assertEqual(completion.status(self.session,self.library)['network_recovery_attempts'],4)

    def test_until_drained_does_not_clear_holds_or_ignore_malformed_policy(self):
        self.configure(until_drained=True);task,_=self.publish()
        self.finish(task,error='login_required')
        self.assertEqual(self.tick()['status'],'paused')
        with self.assertRaises(ArchiveError):self.configure(until_drained=True)
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        plan=completion._load(self.root);plan['until_drained']='PRIVATE'
        completion._save(self.root,plan)
        with self.assertRaises(ArchiveError):completion.status(self.session,self.library)

    def test_crash_after_new_window_publication_recovers_same_timestamp_and_task(self):
        self.configure();self.tick()
        plan=completion._load(self.root);reserved=plan['reserved_window']
        # Crash immediately after backlog replacement, before adding the window
        # to the completion ledger. Re-entry must adopt, never replace it.
        batches._start(self.root,reserved['budgets'],False,now=batches._instant(reserved['started_at']))
        initial=batches.step(self.session,self.library);task=initial['pending_task_id']
        self.tick()
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        self.assertEqual(completion.status(self.session,self.library)['window_count'],1)
        self.assertEqual([p.stem for p in (self.root/'requests').glob('*.json')],[task])
        self.finish(task)
        self.assertEqual(self.tick()['archived_episodes'],1)

    def test_adopt_external_controller_without_overlapping_its_runtime(self):
        batches._start(self.root,self.window,False)
        self.configure();ready=threading.Event();release=threading.Event()
        def hold():
            with LibraryLock(self.root/'.backlog-runtime'):
                ready.set();release.wait(5)
        thread=threading.Thread(target=hold);thread.start();ready.wait(5)
        try:
            for _ in range(3):self.tick()
            self.assertIsNone(batches.status(self.session,self.library)['pending_task_id'])
            batches.step(self.session,self.library);batches.step(self.session,self.library)
            task=batches.status(self.session,self.library)['pending_task_id']
            self.assertEqual(self.tick()['window_count'],1)
            self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        finally:release.set();thread.join(5)
        self.finish(task)
        self.assertEqual(self.tick()['archived_episodes'],1)

    def test_expired_pending_window_observes_exact_receipt_before_renewal(self):
        self.configure();task,_=self.publish()
        now=batches._now()
        window=batches._load(self.root);window.update(status='budget_reached',
            started_at=(now-timedelta(hours=3)).isoformat(),
            submit_before=(now-timedelta(hours=1)).isoformat())
        # Preserve its matching identity in the test ledger.
        plan=completion._load(self.root);plan['windows'][0]['started_at']=window['started_at']
        batches._save(self.root,window);completion._save(self.root,plan)
        self.assertEqual(self.tick()['window_count'],1)
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        self.finish(task)
        new,request=self.publish();self.assertNotEqual(new,task)
        self.assertEqual(self.tick()['archived_episodes'],1)

    def test_overall_expiry_never_publishes_an_unpublished_reservation(self):
        self.configure();self.tick();self.tick()
        self.assertIsNotNone(batches.status(self.session,self.library)['pending_task_id'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        now=batches._now()
        plan=completion._load(self.root);plan.update(started_at=(now-timedelta(days=8)).isoformat(),
                                                   submit_before=(now-timedelta(days=1)).isoformat())
        completion._save(self.root,plan)
        self.assertEqual(self.tick()['status'],'budget_reached')
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        self.assertIsNone(batches.status(self.session,self.library)['pending_task_id'])

    def test_login_cancel_and_refusal_holds_are_never_window_renewed(self):
        for error in ('login_required','job_failed'):
            with self.subTest(error=error):
                self.configure();task,_=self.publish();self.finish(task,error=error)
                value=self.tick();self.assertEqual(value['status'],'paused')
                self.assertEqual(value['window_count'],1);self.assertEqual(list((self.root/'requests').iterdir()),[])
                with self.assertRaises(ArchiveError):self.configure(resume=True)
                # Isolate the next deliberate fault, preserving ordinary files.
                (self.root/'completion.json').unlink();(self.root/'backlog.json').unlink()
        self.configure();task,_=self.publish();self.finish(task,status='cancelled')
        self.assertEqual(self.tick()['code'],'cancelled')

    def test_unknown_legacy_usage_counts_against_all_windows(self):
        self.configure({'max_pages':2,'max_bytes':20});task,_=self.publish()
        self.finish(task,error='job_failed');value=self.tick()
        self.assertEqual(value['unreported_reserved_pages'],2);self.assertEqual(value['unreported_reserved_bytes'],20)
        self.assertEqual(value['status'],'paused');self.assertEqual(value['archived_episodes'],0)
        self.assertEqual(value['window_count'],1)

    def test_network_recovery_limit_is_not_reset_at_window_boundary(self):
        self.window['recover_network']=True
        self.configure({'max_network_recoveries':1});task,_=self.publish()
        window=batches._load(self.root);window['network_recovery_attempts']=1;batches._save(self.root,window)
        result={'status':'paused','pause_reason':'network_unavailable','source_http_status':503,
            'downloaded_pages':1,'downloaded_bytes':10,'pending_episode_count':5,'archived_episode_ids':[],
            'inventory':{'scan_remaining_series_count':0}}
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        self.tick();value=self.tick()
        self.assertEqual(value['code'],'network_recovery_limit')
        self.assertEqual(value['network_recovery_attempts'],1)
        self.assertIsNone(batches.status(self.session,self.library)['network_retry_after'])

    def test_recoveries_given_back_to_the_window_no_longer_count_against_the_plan(self):
        self.window.update(recover_network=True,max_batches=3)
        self.configure();task,_=self.publish()
        window=batches._load(self.root);window['network_recovery_attempts']=2;batches._save(self.root,window)
        plan=completion._load(self.root)
        plan['windows'].insert(0,{'started_at':(batches._now()-timedelta(days=1)).isoformat(),
                                  **{key:0 for key in completion.METRICS},'network_recovery_attempts':1})
        completion._save(self.root,plan)
        self.assertEqual(self.tick()['network_recovery_attempts'],3)
        # Progress, or the person resuming the window, gives it its recoveries back.
        window=batches._load(self.root);window['network_recovery_attempts']=0;batches._save(self.root,window)
        value=self.tick()
        self.assertEqual((value['status'],value['code'],value['network_recovery_attempts']),('running',None,0))
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)

    def test_the_last_plan_wide_network_recovery_is_cancelled_right_after_it_is_scheduled(self):
        # Present behaviour: a plan allowed N recoveries carries out N-1. Whether that is wanted is open (see the handoff).
        self.window.update(recover_network=True,max_batches=2)
        self.configure({'max_network_recoveries':1});task,_=self.publish()
        result={'status':'paused','pause_reason':'network_unavailable','source_http_status':503,
            'downloaded_pages':1,'downloaded_bytes':10,'pending_episode_count':5,'archived_episode_ids':[],
            'inventory':{'scan_remaining_series_count':0}}
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        self.tick()
        window=batches._load(self.root)
        self.assertIsNotNone(window['network_retry_after']);self.assertEqual(window['network_recovery_attempts'],1)
        value=self.tick()
        self.assertEqual((value['status'],value['code']),('paused','network_recovery_limit'))
        self.assertIsNone(batches._load(self.root)['network_retry_after'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_due_daily_job_gets_priority_over_backlog_submission(self):
        from comic_archive.daily import configure,tick
        self.configure();configure(self.session,run_now=True)
        self.tick();self.assertIsNone(batches._load(self.root))
        daily=tick(self.session);self.assertIsNotNone(daily['pending_task_id'])
        for _ in range(3):self.tick()
        self.assertEqual([p.stem for p in (self.root/'requests').glob('*.json')],[daily['pending_task_id']])

    def test_stop_keeps_pending_task_and_old_limits_on_explicit_resume(self):
        self.configure();task,_=self.publish();before=completion.status(self.session,self.library)
        completion.request_stop(self.session,self.library)
        self.assertEqual(self.tick()['status'],'stopped')
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        # The account holder explicitly resumes the underlying stopped window.
        batches._start(self.root,None,True);self.configure(resume=True)
        after=self.tick();self.assertEqual(after['submit_before'],before['submit_before'])
        self.assertEqual(after['limits'],before['limits'])
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)

    def test_drained_queue_is_distinct_from_complete_account(self):
        self.configure();task,_=self.publish();self.finish(task,status='finished',pending=0)
        value=self.tick();self.assertEqual(value['status'],'known_backlog_drained')
        self.assertFalse(value['account_inventory_complete']);self.assertEqual(value['window_count'],1)

    def test_changed_or_regressed_window_holds_without_replacement(self):
        self.configure();task,_=self.publish();self.finish(task)
        window=batches._load(self.root);window['downloaded_pages']=0;batches._save(self.root,window)
        self.assertEqual(self.tick()['code'],'accounting_changed')
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_unknown_window_or_deleted_checkpoint_is_not_silently_adopted(self):
        self.configure();self.tick();self.tick()
        (self.root/'backlog.json').unlink()
        self.assertEqual(self.tick()['code'],'window_changed')

    def test_limits_state_and_symlinks_fail_closed(self):
        for values in ({'max_windows':True},{'max_days':8},{'max_network_recoveries':-1},{'password':'PRIVATE'}):
            with self.assertRaises(ArchiveError):self.configure(values)
        self.configure();original=(self.root/'completion.json').read_bytes()
        (self.root/'completion.json').write_text('{bad json')
        self.assertIsNone(completion.worker_tick(self.session,self.library))
        self.assertIsNone(batches._load(self.root))
        (self.root/'completion.json').unlink()
        other=self.project/'other.json';other.write_bytes(original)
        (self.root/'completion.json').symlink_to(other)
        with self.assertRaises(ArchiveError):self.tick()

    @patch('builtins.print')
    def test_cli_defaults_and_resume_preserve_current_bounds(self,output):
        self.assertEqual(parser().parse_args(['complete-backlog']).max_windows,7)
        with patch('comic_archive.completion.configure',return_value={'status':'running'}) as configure:
            self.assertEqual(run(parser().parse_args(['--request-interval','0.5','complete-backlog','--resume'])),0)
            self.assertTrue(configure.call_args.kwargs['resume'])
            self.assertEqual(configure.call_args.kwargs['window_budgets'],{'request_interval':.5})
            self.assertFalse(configure.call_args.kwargs['until_drained'])
            self.assertEqual(run(parser().parse_args(['complete-backlog','--until-drained','--speed','fast'])),0)
            self.assertTrue(configure.call_args.kwargs['until_drained'])


    def test_a_damaged_or_tampered_plan_is_refused_and_left_exactly_as_it_was(self):
        self.configure();path=self.root/'completion.json';original=path.read_bytes()
        def stamp(day):return datetime(2026,10,day,tzinfo=timezone.utc).isoformat()
        def record(day,**counters):return {'started_at':stamp(day),**{key:0 for key in completion.METRICS},**counters}
        def no_aggregate_caps(plan):
            for key in completion.AGGREGATE_LIMITS:plan['limits'][key]=None
        def drained_with_deadline(plan):plan.update(until_drained=True);no_aggregate_caps(plan)
        def deadline_after(days):
            return lambda plan:plan.update(submit_before=(datetime.fromisoformat(plan['started_at'])+timedelta(days=days)).isoformat())
        reserved_budgets=batches.budgets(completion.WINDOW_DEFAULTS)
        cases={
            'schema version':lambda p:p.update(schema_version=2),
            'unknown status':lambda p:p.update(status='bogus'),
            'unknown reason':lambda p:p.update(code='bogus'),
            'aggregate caps left on an until-drained plan':lambda p:p.update(until_drained=True,submit_before=None),
            'unlimited caps on a timed plan':no_aggregate_caps,
            'one unlimited cap on a timed plan':lambda p:p['limits'].update(max_pages=None),
            'deadline left on an until-drained plan':drained_with_deadline,
            'window budgets that no longer archive only':lambda p:p['window_budgets'].update(archive_only=False),
            'deadline further away than the day limit':deadline_after(8),
            'deadline before the start':deadline_after(-1),
            'windows that are not a list':lambda p:p.update(windows='x'),
            'more windows than allowed':lambda p:p.update(windows=[record(day) for day in range(1,9)]),
            'window without its counters':lambda p:p.update(windows=[{'started_at':stamp(1)}]),
            'two windows with the same start':lambda p:p.update(windows=[record(1),record(1)]),
            'negative counter':lambda p:p.update(windows=[record(1,downloaded_pages=-1)]),
            'reserved window without budgets':lambda p:p.update(reserved_window={'started_at':stamp(1)}),
            'reserved window that would discover':lambda p:p.update(reserved_window={'started_at':stamp(1),
                'budgets':batches.budgets({'archive_only':False})}),
            'reserved window already in the ledger':lambda p:p.update(windows=[record(1)],
                reserved_window={'started_at':stamp(1),'budgets':reserved_budgets}),
            'reserved window beyond the window limit':lambda p:p.update(windows=[record(day) for day in range(1,8)],
                reserved_window={'started_at':stamp(9),'budgets':reserved_budgets})}
        for name,change in cases.items():
            with self.subTest(name):
                plan=json.loads(original);change(plan);atomic_json(path,plan);damaged=path.read_bytes()
                with self.assertRaises(ArchiveError):completion.status(self.session,self.library)
                self.assertIsNone(completion.worker_tick(self.session,self.library))
                self.assertEqual(path.read_bytes(),damaged)
                self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_without_a_plan_nothing_can_be_stopped_or_resumed_and_the_worker_stays_idle(self):
        with self.assertRaises(ArchiveError):completion.request_stop(self.session,self.library)
        with self.assertRaises(ArchiveError):completion.configure(self.session,self.library,resume=True)
        for until_drained in ('yes',1):
            with self.assertRaises(ArchiveError):completion.configure(self.session,self.library,until_drained=until_drained)
        self.assertEqual(self.tick(),{'configured':False,'account_inventory_complete':False})
        self.assertEqual(completion.worker_tick(self.session,self.library),{'configured':False,'account_inventory_complete':False})
        self.assertFalse((self.root/'completion.json').exists());self.assertFalse((self.root/'backlog.json').exists())
        self.configure()
        with self.assertRaises(ArchiveError):completion.configure(self.session,self.library,resume=True,until_drained=True)

    def test_a_running_plan_is_not_replaced_and_windows_that_would_discover_are_refused(self):
        with self.assertRaises(ArchiveError):
            completion.configure(self.session,self.library,window_budgets=self.window|{'archive_only':False})
        self.assertFalse((self.root/'completion.json').exists())
        self.configure();before=(self.root/'completion.json').read_bytes()
        with self.assertRaises(ArchiveError):self.configure({'max_windows':3})
        self.assertEqual((self.root/'completion.json').read_bytes(),before)

    def test_an_existing_window_that_exceeds_the_new_limits_is_not_adopted(self):
        batches._start(self.root,self.window,False)
        with self.assertRaises(ArchiveError):self.configure({'max_pages':1})
        self.assertFalse((self.root/'completion.json').exists())
        window=batches._load(self.root);window['network_recovery_attempts']=1;batches._save(self.root,window)
        saved=(self.root/'backlog.json').read_bytes()
        with self.assertRaises(ArchiveError):self.configure({'max_network_recoveries':0})
        self.assertFalse((self.root/'completion.json').exists())
        self.assertEqual((self.root/'backlog.json').read_bytes(),saved)

    def test_until_drained_is_refused_while_the_previous_plan_is_held(self):
        self.configure();self.tick();self.tick()
        (self.root/'backlog.json').unlink()
        self.assertEqual(self.tick()['code'],'window_changed')
        held=(self.root/'completion.json').read_bytes()
        with self.assertRaises(ArchiveError):self.configure(until_drained=True)
        self.assertEqual((self.root/'completion.json').read_bytes(),held)
        self.assertFalse(completion.status(self.session,self.library)['until_drained'])

    def test_discovery_cannot_renew_a_drained_backlog_with_windows_that_would_discover(self):
        self.configure();task,_=self.publish();self.finish(task,status='finished',pending=0)
        self.assertEqual(self.tick()['status'],'known_backlog_drained')
        plan,window=(self.root/'completion.json').read_bytes(),(self.root/'backlog.json').read_bytes()
        with self.assertRaises(ArchiveError):
            completion.continue_discovered(self.session,self.library,window_budgets=self.window|{'archive_only':False})
        self.assertEqual(((self.root/'completion.json').read_bytes(),(self.root/'backlog.json').read_bytes()),(plan,window))

    def test_a_window_started_by_someone_else_holds_the_plan_and_is_left_alone(self):
        self.configure();task,_=self.publish();self.finish(task)
        batches._start(self.root,self.window,False,now=batches._now()+timedelta(seconds=5))
        foreign=(self.root/'backlog.json').read_bytes()
        value=self.tick()
        self.assertEqual((value['status'],value['code'],value['window_count']),('paused','window_changed',1))
        self.assertEqual((self.root/'backlog.json').read_bytes(),foreign)
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_stopping_the_window_directly_stops_the_plan_and_keeps_its_task(self):
        self.configure();task,_=self.publish()
        batches.request_stop(self.session,self.library)
        value=self.tick()
        self.assertEqual((value['status'],value['code']),('stopped',None))
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        self.assertEqual([p.stem for p in (self.root/'requests').glob('*.json')],[task])
        self.assertEqual(self.tick()['status'],'stopped')

    def test_plan_wide_recovery_limits_lower_those_of_the_live_window(self):
        self.window['recover_network']=True
        self.configure();task,_=self.publish()
        before=batches._load(self.root)['budgets']
        self.assertEqual((before['network_recovery_limit'],before['source_recovery_limit']),(2,4))
        plan=completion._load(self.root)
        plan['windows'].insert(0,{'started_at':(batches._now()-timedelta(days=1)).isoformat(),
            **{key:0 for key in completion.METRICS},'network_recovery_attempts':3,'source_recovery_attempts':7})
        completion._save(self.root,plan)
        self.tick()
        after=batches._load(self.root)
        self.assertEqual((after['budgets']['network_recovery_limit'],after['budgets']['source_recovery_limit'],
                          after['budgets']['recover_network']),(1,1,True))
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)

    def test_a_window_is_not_started_when_less_than_a_second_of_the_plan_is_left(self):
        start=datetime(2026,10,1,tzinfo=timezone.utc)
        with patch('comic_archive.batches._now',return_value=start):self.configure({'max_days':1})
        with patch('comic_archive.batches._now',return_value=start+timedelta(days=1)-timedelta(seconds=.5)):
            value=self.tick()
        self.assertEqual((value['status'],value['window_count'],value['window_reserved']),('budget_reached',0,False))
        self.assertFalse((self.root/'backlog.json').exists())

    def test_an_expired_plan_drops_a_window_reserved_but_never_started(self):
        self.configure();self.assertTrue(self.tick()['window_reserved'])
        self.assertFalse((self.root/'backlog.json').exists())
        now=batches._now()
        plan=completion._load(self.root);plan.update(started_at=(now-timedelta(days=8)).isoformat(),
                                                   submit_before=(now-timedelta(days=1)).isoformat())
        completion._save(self.root,plan)
        value=self.tick()
        self.assertEqual((value['status'],value['window_reserved'],value['window_count']),('budget_reached',False,0))
        self.assertFalse((self.root/'backlog.json').exists())

    def test_a_reserved_window_waits_for_an_unfinished_or_regressed_previous_window(self):
        self.configure();task,_=self.publish()
        plan=completion._load(self.root)
        plan['reserved_window']={'started_at':(batches._now()+timedelta(seconds=1)).isoformat(),
                                 'budgets':batches.budgets(completion.WINDOW_DEFAULTS|self.window)}
        completion._save(self.root,plan);path=self.root/'completion.json';reserved=path.read_bytes()
        value=self.tick()
        self.assertEqual((value['status'],value['code'],value['window_count']),('paused','window_changed',1))
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)
        path.write_bytes(reserved);plan=completion._load(self.root);plan['windows'][0]['downloaded_pages']=5
        completion._save(self.root,plan)
        self.assertEqual(self.tick()['code'],'accounting_changed')
        self.assertEqual(batches.status(self.session,self.library)['pending_task_id'],task)

    def test_a_damaged_window_checkpoint_holds_the_plan_without_stopping_the_worker(self):
        self.configure();(self.root/'backlog.json').write_text('{bad')
        self.assertIsNone(completion.worker_tick(self.session,self.library))
        value=completion.status(self.session,self.library)
        self.assertEqual((value['status'],value['code']),('paused','controller_failed'))
        self.assertEqual((self.root/'backlog.json').read_text(),'{bad')
        self.assertEqual(completion.worker_tick(self.session,self.library)['code'],'controller_failed')


    def test_reaching_the_page_cap_ends_the_plan_and_stops_the_idle_window_without_another_job(self):
        self.configure({'max_pages':1});task,request=self.publish()
        self.assertEqual(request['args']['max_pages'],1)
        self.finish(task)
        for _ in range(3):value=self.tick()
        self.assertEqual((value['status'],value['downloaded_pages'],value['window_count']),('budget_reached',1,1))
        window=batches.status(self.session,self.library)
        self.assertEqual((window['stop_requested'],window['pending_task_id']),(True,None))
        self.assertEqual(list((self.root/'requests').iterdir()),[])


if __name__=='__main__':unittest.main()
