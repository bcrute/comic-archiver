"""Durable recovery from a reader refusal, never from an HTTP status alone."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import batches, completion
from comic_archive.archive import atomic_json
from comic_archive.models import ArchiveError


class SourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        project = Path(temporary.name)
        self.session = project / '.comic-session'; self.library = project / 'Library'
        self.library.mkdir(); self.root = self.session / 'worker'
        for name in ('requests', 'responses'): (self.root / name).mkdir(parents=True)
        atomic_json(self.root / 'status.json', {'state':'ready'})
        self.now = datetime(2026,10,8,16,0,tzinfo=timezone.utc)
        clock = patch('comic_archive.batches._now', side_effect=lambda:self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.start()

    def start(self, **changes):
        return batches._start(self.root, {'archive_only':True,'refresh_lists':False,
            'max_batches':10,'max_pages':100,'batch_pages':4,'max_bytes':1000,'batch_bytes':20,
            'max_seconds':3600, **changes}, False)

    def step(self): return batches.step(self.session, self.library)

    def publish(self):
        value = self.step(); task = value['pending_task_id']; self.assertIsNotNone(task)
        self.step()
        return task, json.loads((self.root / 'requests' / (task+'.json')).read_text())

    def refuse(self, task, **changes):
        result = {'run_id':'a'*24,'status':'paused','pause_reason':'source_refused',
                  'source_http_status':403,'source_response_kind':'html','source_retry_episode_id':'82',
                  'downloaded_pages':1,'downloaded_bytes':2,'pending_episode_count':10,
                  'archived_episode_ids':[],'inventory':{'scan_remaining_series_count':0}, **changes}
        self.receipt(task, {'task_id':task,'status':'error','error_code':'source_refused','result':result})
        return self.step()

    def receipt(self, task, value):
        atomic_json(self.root / 'responses' / (task+'.json'), value)
        (self.root / 'requests' / (task+'.json')).unlink()

    def succeed(self, task, **changes):
        result = {'status':'limited','downloaded_pages':1,'downloaded_bytes':2,
                  'pending_episode_count':9,'archived_episode_ids':['82'],
                  'inventory':{'scan_remaining_series_count':0},
                  'reader_probe_episode_id':'82','reader_probe_succeeded':True, **changes}
        self.receipt(task, {'task_id':task,'status':'ok','result':result})
        return self.step()

    def test_first_refusal_waits_30_seconds_then_probes_exact_episode_and_credits_once(self):
        original = batches.status(self.session,self.library)
        task,_ = self.publish(); waiting = self.refuse(task)
        expected = (self.now+timedelta(seconds=30)).isoformat()
        self.assertEqual(waiting['source_recovery_wait'], expected)
        self.assertEqual(waiting['source_recovery_attempts'], 1)
        self.assertEqual(waiting['downloaded_pages'], 1)
        batches._start(self.root,None,True)
        self.assertEqual(self.step()['source_recovery_wait'],expected)
        self.now += timedelta(seconds=29)
        self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(list((self.root/'requests').glob('*.json')), [])
        self.now += timedelta(seconds=1)
        retry,args = self.publish()
        self.assertEqual(args['args']['retry_episode_id'],'82')
        self.assertEqual(args['args']['limit'],1)
        self.assertNotEqual(task,retry)
        recovered = self.succeed(retry)
        self.assertEqual(recovered['status'],'running')
        self.assertEqual(recovered['completed_batches'],2)
        self.assertEqual(recovered['downloaded_pages'],2)
        self.assertEqual(recovered['submit_before'],original['submit_before'])
        self.assertIsNone(recovered['source_recovery_episode'])
        _,args = self.publish()
        self.assertNotIn('retry_episode_id',args['args'])
        self.assertEqual(self.step()['downloaded_pages'],2)

    def test_failed_probes_double_waits_and_stop_at_four_attempts(self):
        for delay in (30,60,120,240):
            task,_ = self.publish(); waiting = self.refuse(task)
            self.assertEqual(waiting['source_recovery_delay'],delay)
            self.assertEqual(waiting['source_recovery_wait'],(self.now+timedelta(seconds=delay)).isoformat())
            self.now += timedelta(seconds=delay)
        task,_ = self.publish(); held = self.refuse(task)
        self.assertEqual(held['source_recovery_attempts'],4)
        self.assertEqual(held['source_recovery_delay'],480)
        self.assertIsNone(held['source_recovery_wait'])
        self.now += timedelta(seconds=500)
        self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(self.step()['submitted_batches'],5)
        # Go is then the person's own retry: the refused episode is tried again, alone, with the waits already learned.
        resumed=batches._start(self.root,None,True)
        self.assertEqual((resumed['status'],resumed['code'],resumed['source_recovery_attempts'],resumed['source_recovery_delay']),
                         ('running',None,0,480))

    def test_status_without_verified_reader_gate_challenge_and_invalid_receipt_never_retry(self):
        for fields in ({'source_retry_episode_id':None},{'source_response_kind':'challenge'},
                       {'source_response_kind':'json'},{'source_retry_episode_id':True},
                       {'source_http_status':429},{'downloaded_pages':1000}):
            with self.subTest(fields=fields):
                self.start(); task,_ = self.publish(); held = self.refuse(task,**fields)
                self.assertIsNone(held['source_recovery_wait'])
                self.assertEqual(held['source_recovery_attempts'],0)
                self.assertIsNone(self.step()['pending_task_id'])

    def test_server_wait_is_retained_and_explicit_resume_cannot_shorten_it(self):
        task,_ = self.publish(); waiting = self.refuse(task,retry_after_seconds=300)
        self.assertEqual(waiting['source_recovery_delay'],300)
        self.assertEqual(waiting['source_recovery_wait'],(self.now+timedelta(seconds=300)).isoformat())
        with self.assertRaises(ArchiveError): batches._start(self.root,None,True)
        self.now += timedelta(seconds=299)
        self.assertIsNone(self.step()['pending_task_id'])
        self.now += timedelta(seconds=1); retry,_ = self.publish(); recovered = self.succeed(retry)
        self.assertEqual(recovered['source_recovery_delay'],300)

    def test_success_for_other_episode_or_changed_access_cannot_clear_refusal(self):
        task,_ = self.publish(); self.refuse(task); self.now += timedelta(seconds=30)
        retry,_ = self.publish()
        held = self.succeed(retry, reader_probe_episode_id='83', archived_episode_ids=['83'])
        self.assertEqual(held['code'],'source_access_changed')
        self.assertIsNone(self.step()['pending_task_id'])

    def test_stop_expired_deadline_and_full_batch_budget_prevent_recovery(self):
        task,_ = self.publish(); self.refuse(task); batches.request_stop(self.session,self.library)
        self.assertEqual(self.step()['status'],'stopped')
        self.assertIsNone(self.step()['source_recovery_wait'])
        self.start(max_seconds=29); task,_ = self.publish(); held = self.refuse(task)
        self.assertIsNone(held['source_recovery_wait'])
        self.start(max_batches=1); task,_ = self.publish(); held = self.refuse(task)
        self.assertEqual(held['status'],'budget_reached')
        self.assertIsNone(held['source_recovery_wait'])

    def test_malformed_learning_state_is_rejected_before_submission(self):
        original = batches._load(self.root)
        for changes in ({'source_recovery_attempts':True},{'source_recovery_attempts':5},
                        {'source_recovery_delay':29},{'source_recovery_delay':True},
                        {'source_recovery_episode':'PRIVATE'},{'source_recovery_wait':self.now.isoformat()}):
            with self.subTest(changes=changes):
                atomic_json(self.root/'backlog.json',{**original,**changes})
                with self.assertRaises(ArchiveError): self.step()
                self.assertEqual(list((self.root/'requests').glob('*.json')),[])

    def test_overall_limit_retains_final_scheduled_probe_and_does_not_reset_network_budget(self):
        completion.configure(self.session,self.library,{'max_source_recoveries':1,'max_network_recoveries':0})
        completion.tick(self.session,self.library)
        task,_ = self.publish(); self.refuse(task)
        self.assertEqual(completion.tick(self.session,self.library)['status'],'running')
        self.now += timedelta(seconds=30); retry,_ = self.publish(); self.refuse(retry)
        held = completion.tick(self.session,self.library)
        self.assertEqual(held['code'],'source_recovery_limit')
        self.assertEqual(held['source_recovery_attempts'],1)
        self.assertEqual(held['network_recovery_attempts'],0)
        self.assertIsNone(self.step()['pending_task_id'])

    def test_learned_wait_and_global_attempts_survive_window_rollover(self):
        self.start(max_batches=3)
        completion.configure(self.session,self.library,{'max_source_recoveries':3})
        completion.tick(self.session,self.library)
        task,_ = self.publish(); self.refuse(task); self.now += timedelta(seconds=30)
        retry,_ = self.publish(); self.refuse(retry); self.now += timedelta(seconds=60)
        retry,_ = self.publish(); self.succeed(retry)
        for _ in range(3): completion.tick(self.session,self.library)
        window = batches.status(self.session,self.library)
        self.assertEqual(window['source_recovery_delay'],60)
        self.assertEqual(window['budgets']['source_recovery_limit'],1)
        self.assertEqual(completion.status(self.session,self.library)['source_recovery_attempts'],2)

    @patch('builtins.print')
    def test_probe_uses_observed_pending_identity_and_rechecks_current_access(self, output):
        from comic_archive.inventory import Inventory
        from comic_archive.jobs import archive_discovered
        from test_jobs import AccountAdapter
        adapter=AccountAdapter();adapter.access={'81':'tapas_free','82':'tapas_free'}
        with Inventory(self.library) as inventory:
            inventory.observe('42','Series',[{'id':key,'title':'Episode','order':int(key)-80,
                'access_state':'tapas_free'} for key in ('81','82')],'fixture')
        result=archive_discovered(adapter,self.library,limit=1,retry_episode_id='82')
        self.assertEqual(result['archived_episode_ids'],['82'])
        self.assertTrue(result['reader_probe_succeeded'])
        self.assertEqual(result['reader_probe_episode_id'],'82')
        with Inventory(self.library,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])
        adapter.denied.add('81')
        result=archive_discovered(adapter,self.library,limit=1,retry_episode_id='81')
        self.assertFalse(result['reader_probe_succeeded'])
        self.assertEqual(result['unavailable_episode_ids'],['81'])
        before=list(adapter.downloads)
        result=archive_discovered(adapter,self.library,limit=1,retry_episode_id='999')
        self.assertFalse(result['reader_probe_succeeded'])
        self.assertEqual(adapter.downloads,before)

    def test_typed_failure_receipt_retains_only_gated_numeric_identity(self):
        from comic_archive.run_receipts import FailureReceipt,failure_result
        for kind,key,expected in (('html','82','82'),('challenge','82',None),('html','PRIVATE',None),('html',True,None)):
            error=ArchiveError('PRIVATE')
            error._comic_failure_receipt=FailureReceipt('a'*24,'paused',1,2,3,(),0,'source_refused',403,None,kind,key)
            value=failure_result(error)
            self.assertEqual(value.get('source_retry_episode_id'),expected)
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_monitor_exposes_saved_retry_and_controller_liveness_without_mutation_or_private_data(self):
        from comic_archive.inventory import Inventory
        from comic_archive.monitoring import library_status
        with Inventory(self.library):pass
        completion.configure(self.session,self.library)
        task,_=self.publish();waiting=self.refuse(task)
        state=batches._load(self.root);state['headers']={'Cookie':'PRIVATE'};batches._save(self.root,state)
        before=(self.root/'backlog.json').read_bytes()
        with patch('comic_archive.background.status',return_value={'running':True}):
            shown=library_status(self.library,self.session)
        self.assertEqual(shown['source_recovery']['retry_at'],waiting['source_recovery_wait'])
        self.assertTrue(shown['source_recovery']['automatic'])
        self.assertEqual(shown['source_recovery']['episode_id'],'82')
        self.assertNotIn('PRIVATE',json.dumps(shown))
        self.assertEqual((self.root/'backlog.json').read_bytes(),before)

if __name__ == '__main__': unittest.main()
