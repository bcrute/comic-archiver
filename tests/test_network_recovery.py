from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import batches
from comic_archive.archive import atomic_json
from comic_archive.models import ArchiveError


class NetworkRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        project=Path(temp.name)
        self.session=project/'.comic-session';self.library=project/'Library'
        self.root=self.session/'worker'
        for name in ('requests','responses'):(self.root/name).mkdir(parents=True)
        atomic_json(self.root/'status.json',{'state':'ready'})
        self.now=datetime(2026,10,4,5,0,tzinfo=timezone.utc)
        clock=patch('comic_archive.batches._now',side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        self.start()

    def start(self,**values):
        return batches._start(self.root,{'recover_network':True,'archive_only':True,'refresh_lists':False,
            'max_pages':20,'batch_pages':4,'max_bytes':100,'batch_bytes':20,
            'max_seconds':600,'max_batches':4,**values},False)

    def step(self):return batches.step(self.session,self.library)

    def publish(self):
        first=self.step();task=first['pending_task_id']
        self.assertIsNotNone(task)
        self.step()
        return task,json.loads((self.root/'requests'/f'{task}.json').read_text())

    def complete(self,task,*,error=None,source_status=None,retry_after=None,archived=()):
        if error:
            value={'task_id':task,'status':'error','error_code':'job_failed','error':error}
        else:
            result={'status':'paused','pause_reason':'network_unavailable','downloaded_pages':1,
                'downloaded_bytes':2,'pending_episode_count':10,'archived_episode_ids':list(archived),
                'inventory':{'scan_remaining_series_count':0}}
            if source_status is not None:result['source_http_status']=source_status
            if retry_after is not None:result['retry_after_seconds']=retry_after
            value={'task_id':task,'status':'ok','result':result}
        atomic_json(self.root/'responses'/f'{task}.json',value)
        (self.root/'requests'/f'{task}.json').unlink()
        return self.step()

    def test_cooldown_survives_controller_resume_with_exact_accounting_and_original_deadline(self):
        initial=batches.status(self.session,self.library)
        task,_=self.publish();paused=self.complete(task,source_status=503)
        self.assertEqual(paused['downloaded_pages'],1)
        self.assertEqual(paused['completed_batches'],1)
        self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=60)).isoformat())
        self.assertEqual(paused['network_recovery_attempts'],1)
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        batches._start(self.root,None,True)
        self.assertEqual(self.step()['network_retry_after'],paused['network_retry_after'])
        self.assertEqual(self.step()['submit_before'],initial['submit_before'])
        self.now+=timedelta(seconds=59)
        self.assertIsNone(self.step()['pending_task_id'])
        self.now+=timedelta(seconds=1)
        new,request=self.publish()
        self.assertNotEqual(new,task)
        self.assertEqual(request['args']['max_pages'],4)
        self.assertEqual(self.step()['downloaded_pages'],1)
        self.assertEqual(self.step()['network_recovery_attempts'],1)
        self.assertNotIn('recover_network',request['args'])

    def test_two_exponential_recoveries_then_pause_without_another_submission(self):
        for attempt,delay in ((1,60),(2,120)):
            task,_=self.publish();paused=self.complete(task)
            self.assertEqual(paused['network_recovery_attempts'],attempt)
            self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=delay)).isoformat())
            self.now+=timedelta(seconds=delay)
        task,_=self.publish();paused=self.complete(task)
        self.assertEqual(paused['network_recovery_attempts'],2)
        self.assertIsNone(paused['network_retry_after'])
        self.now+=timedelta(seconds=180)
        self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(self.step()['submitted_batches'],3)
        self.assertEqual(self.step()['downloaded_pages'],3)

    def test_a_batch_that_saved_an_episode_gives_the_window_its_recoveries_back(self):
        self.start(max_batches=8)
        task,_=self.publish();paused=self.complete(task)
        self.assertEqual(paused['network_recovery_attempts'],1)
        self.now+=timedelta(seconds=60)
        # The connection failed again, but only after an episode got through: this is a first failure once more.
        task,_=self.publish();paused=self.complete(task,archived=['81'])
        self.assertEqual((paused['status'],paused['code'],paused['network_recovery_attempts']),('paused','network_unavailable',1))
        self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=60)).isoformat())
        self.now+=timedelta(seconds=60)
        # Nothing gets through from here on: the second recovery waits longer and the third failure holds.
        task,_=self.publish();paused=self.complete(task)
        self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=120)).isoformat())
        self.now+=timedelta(seconds=120)
        task,_=self.publish();paused=self.complete(task)
        self.assertEqual((paused['status'],paused['network_recovery_attempts'],paused['network_retry_after']),('paused',2,None))

    def test_resuming_a_window_held_after_its_recoveries_gives_them_back(self):
        self.start(max_batches=8)
        for delay in (60,120):
            task,_=self.publish();self.complete(task);self.now+=timedelta(seconds=delay)
        task,_=self.publish();held=self.complete(task)
        self.assertEqual((held['status'],held['network_recovery_attempts'],held['network_retry_after']),('paused',2,None))
        batches._start(self.root,None,True)
        resumed=batches.status(self.session,self.library)
        self.assertEqual((resumed['status'],resumed['code'],resumed['network_recovery_attempts']),('running',None,0))
        task,_=self.publish();paused=self.complete(task)
        self.assertEqual(paused['network_recovery_attempts'],1)
        self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=60)).isoformat())

    def test_server_wait_survives_restart_and_explicit_resume_cannot_shorten_it(self):
        task,_=self.publish();initial=self.step()['submit_before']
        paused=self.complete(task,source_status=503,retry_after=300)
        self.assertEqual(paused['network_retry_after'],(self.now+timedelta(seconds=300)).isoformat())
        self.assertEqual(paused['source_retry_after'],paused['network_retry_after'])
        with self.assertRaises(ArchiveError):batches._start(self.root,None,True)
        self.now+=timedelta(seconds=299)
        self.assertIsNone(self.step()['pending_task_id'])
        self.now+=timedelta(seconds=1);self.publish()
        self.assertEqual(self.step()['submit_before'],initial)
        self.assertEqual(self.step()['network_recovery_attempts'],1)

    def test_server_wait_beyond_window_keeps_hold_without_falling_back_to_short_retry(self):
        task,_=self.publish();paused=self.complete(task,source_status=503,retry_after=900)
        self.assertIsNone(paused['network_retry_after'])
        self.now+=timedelta(seconds=120)
        for _ in range(2):self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(self.step()['network_recovery_attempts'],0)
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_stop_and_expired_deadline_cancel_waiting_recovery_without_source_jobs(self):
        task,_=self.publish();paused=self.complete(task)
        batches.request_stop(self.session,self.library)
        self.assertEqual(self.step()['status'],'stopped')
        self.assertIsNone(self.step()['network_retry_after'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        self.start()
        task,_=self.publish();paused=self.complete(task)
        self.now=datetime.fromisoformat(paused['submit_before'])
        self.assertEqual(self.step()['status'],'budget_reached')
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_legacy_network_error_reserves_the_full_allocation_and_never_reads_unrelated_progress(self):
        self.start(max_pages=6)
        task,_=self.publish()
        self.library.joinpath('.archive-state').mkdir(exist_ok=True)
        atomic_json(self.library/'.archive-state/last-run.json',{'downloaded_pages':999,'downloaded_bytes':999})
        paused=self.complete(task,error='Page network request failed; resume later.')
        self.assertEqual(paused['code'],'network_unavailable')
        self.assertEqual(paused['unreported_reserved_pages'],4)
        self.assertEqual(paused['downloaded_pages'],0)
        self.now+=timedelta(seconds=60)
        _,request=self.publish()
        self.assertEqual(request['args']['max_pages'],2)
        self.assertEqual(self.step()['unreported_reserved_bytes'],20)

    def test_explicit_refusals_and_unrecognized_errors_do_not_schedule_recovery(self):
        for error in ('Normal browser metadata GET returned HTTP 403.',
                'Server requests a long pause; resume later.',
                'Page network request failed; resume later. PRIVATE',
                'Schema mismatch; stop.'):
            with self.subTest(error=error):
                self.start()
                task,_=self.publish();paused=self.complete(task,error=error)
                self.assertEqual(paused['code'],'task_failed')
                self.assertIsNone(paused['network_retry_after'])
                self.assertEqual(self.step()['submitted_batches'],1)
                self.assertNotIn('PRIVATE',json.dumps(paused))
        for status in (401,403,429,500,True):
            with self.subTest(status=status):
                self.start()
                task,_=self.publish();paused=self.complete(task,source_status=status)
                self.assertEqual(paused['code'],'task_failed')
                self.assertIsNone(paused['network_retry_after'])

    def test_recovery_does_not_extend_budget_or_schedule_when_there_is_no_time_to_wait(self):
        self.start(max_pages=1)
        task,_=self.publish();value=self.complete(task)
        self.assertEqual(value['status'],'budget_reached')
        self.assertIsNone(value['network_retry_after'])
        self.start(max_seconds=59)
        task,_=self.publish();value=self.complete(task)
        self.assertEqual(value['status'],'paused')
        self.assertIsNone(value['network_retry_after'])
        self.assertEqual(value['network_recovery_attempts'],0)

    def test_malformed_persistent_recovery_fields_cannot_publish_any_request(self):
        original=batches._load(self.root)
        for fields in ({'network_recovery_attempts':True},{'network_recovery_attempts':-1},
                {'network_recovery_attempts':3},{'network_retry_after':'invalid'},
                {'network_retry_after':original['submit_before']},
                {'network_retry_after':self.now.isoformat()}):
            with self.subTest(fields=fields):
                atomic_json(self.root/'backlog.json',{**original,**fields})
                with self.assertRaises(ArchiveError):self.step()
                self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_cli_persists_recovery_option_without_adding_source_request_options(self):
        from comic_archive.cli import parser,run
        args=parser().parse_args(['run-backlog','--recover-network','--archive-only'])
        with patch('comic_archive.batches.run',return_value={'status':'budget_reached'}) as runner,patch('builtins.print'):
            self.assertEqual(run(args),0)
        self.assertTrue(runner.call_args.kwargs['values']['recover_network'])

    def test_real_controller_loop_waits_then_consumes_a_new_receipt_and_finishes(self):
        values=batches._load(self.root)['budgets']
        started=self.now
        tasks=[];progress=[]
        def tick(seconds):
            self.now+=timedelta(seconds=seconds)
            for path in (self.root/'requests').glob('*.json'):
                task=path.stem;tasks.append(task)
                first=len(tasks)==1
                result={'status':'paused' if first else 'finished','downloaded_pages':1,
                    'downloaded_bytes':2,'archived_episode_ids':[] if first else ['81'],
                    'pending_episode_count':1 if first else 0,'inventory':{'scan_remaining_series_count':0}}
                if first:result.update(pause_reason='network_unavailable',source_http_status=503)
                atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
                path.unlink()
        with patch('comic_archive.batches.time.sleep',side_effect=tick):
            result=batches.run(self.session,self.library,values,progress=progress.append)
        self.assertEqual(result['status'],'known_backlog_drained')
        self.assertEqual(result['completed_batches'],2)
        self.assertEqual(result['downloaded_pages'],2)
        # The batch after the wait saved an episode, so the recovery it took no longer counts.
        self.assertEqual(result['network_recovery_attempts'],0)
        self.assertEqual(len(set(tasks)),2)
        self.assertGreaterEqual((self.now-started).total_seconds(),60)
        self.assertEqual(result['submit_before'],(started+timedelta(seconds=600)).isoformat())
        self.assertTrue(any(json.loads(line)['network_retry_after'] for line in progress))


if __name__=='__main__':unittest.main()
