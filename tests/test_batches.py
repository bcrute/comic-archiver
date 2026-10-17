from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive import batches
from comic_archive.archive import atomic_json
from comic_archive.cli import parser,run
from comic_archive.errors import LibraryBusy
from comic_archive.locking import LibraryLock
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_job
from comic_archive.archive import verify
from comic_archive.inventory import Inventory
from comic_archive.jobs import sync, archive_discovered
from comic_archive.cancellation import request_stop
from test_jobs import AccountAdapter


class BatchTests(unittest.TestCase):
    def test_queued_stop_receipt_accounts_zero_requests_and_pauses_controller(self):
        from comic_archive.task_cancellation import cancelled_result
        with Inventory(self.library) as inventory:
            inventory.observe('42','S',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
            inventory.scan_candidates()
        self.start({'refresh_lists':False});task,_=self.publish()
        result=cancelled_result(self.library,'sync')
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['code'],'cancelled');self.assertEqual(value['completed_batches'],1)
        self.assertEqual(value['archived_episodes'],0);self.assertEqual(value['downloaded_pages'],0)
        self.assertEqual(value['downloaded_bytes'],0);self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.project=Path(temporary.name)
        self.session=self.project/'.comic-session';self.library=self.project/'Library';self.library.mkdir()
        self.root=self.session/'worker'
        (self.root/'requests').mkdir(parents=True);(self.root/'responses').mkdir()
        atomic_json(self.root/'status.json',{'state':'ready','cookie':'PRIVATE'})

    def start(self,values=None):
        return batches._start(self.root,values,False)

    def step(self):return batches.step(self.session,self.library)

    def publish(self):
        value=self.step();task=value['pending_task_id']
        self.assertIsNotNone(task)
        self.step()
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)
        return task,json.loads((self.root/'requests'/f'{task}.json').read_text())

    def finish(self,task,*,pages=1,byte_count=2,remaining=50,pending=10,status='limited',error=None,archive_only=False):
        value={'task_id':task,'status':'ok','result':{'status':status,'downloaded_pages':pages,
            'downloaded_bytes':byte_count,'pending_episode_count':pending,'archived_episode_ids':['81'] if pages else [],
            'refresh':{'state':'partial','remaining_series_count':remaining},'cookie':'PRIVATE'}}
        if archive_only:
            value['result'].pop('refresh')
            value['result']['inventory']={'scan_remaining_series_count':remaining}
        if error:value={'task_id':task,'status':'error','error_code':error,'error':'PRIVATE'}
        atomic_json(self.root/'responses'/f'{task}.json',value)
        (self.root/'requests'/f'{task}.json').unlink(missing_ok=True)
        atomic_json(self.root/'status.json',{'state':'ready'})
        return self.step()

    @patch('builtins.print')
    def test_durable_asset_cooldown_waits_without_empty_jobs_or_resetting_limits(self,output):
        now=datetime(2026,10,8,14,tzinfo=timezone.utc)
        with patch('comic_archive.batches._now',side_effect=lambda:now), \
             patch('comic_archive.inventory.timestamp',side_effect=lambda:now.isoformat()):
            with Inventory(self.library) as inventory:
                inventory.observe('42','Series',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
                inventory.mark('81','queued','asset_read_unavailable')
            original=self.start({'archive_only':True,'refresh_lists':False,'max_pages':5,'max_bytes':10000,'recover_network':True})
            state=batches._load(self.root)
            state.update(previous_pending_count=1,previous_scan_remaining=1,network_recovery_attempts=2)
            batches._save(self.root,state)
            task,request=self.publish()
            options=request['args'].copy();options.pop('request_interval')
            adapter=AccountAdapter();adapter.access={'81':'tapas_free'}
            result=archive_discovered(adapter,self.library,**options)
            self.assertEqual(result['asset_retry_after_seconds'],300)
            atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
            (self.root/'requests'/f'{task}.json').unlink()
            waiting=self.step()
            self.assertEqual(waiting['status'],'running')
            self.assertIsNone(waiting['code'])
            self.assertEqual(waiting['completed_batches'],1)
            after=waiting['asset_retry_after']
            for _ in range(3):
                waiting=self.step()
                self.assertEqual(waiting['submitted_batches'],1)
                self.assertEqual(waiting['completed_batches'],1)
                self.assertEqual(waiting['asset_retry_after'],after)
                self.assertEqual(waiting['network_recovery_attempts'],2)
                self.assertEqual(waiting['budgets'],original['budgets'])
                self.assertEqual(waiting['submit_before'],original['submit_before'])
                self.assertIsNone(waiting['pending_task_id'])
            now += timedelta(seconds=300)
            task,request=self.publish()
            self.assertEqual(request['args']['max_pages'],5)
            self.assertIsNone(self.step()['asset_retry_after'])
            self.assertEqual(self.step()['network_recovery_attempts'],2)

    def test_asset_cooldown_respects_stop_and_original_deadline(self):
        for action in ('stop','deadline'):
            with self.subTest(action=action):
                now=datetime(2026,10,8,14,tzinfo=timezone.utc)
                with patch('comic_archive.batches._now',side_effect=lambda:now):
                    self.start({'max_seconds':60})
                    state=batches._load(self.root)
                    state['asset_retry_after']=(now+timedelta(seconds=300)).isoformat()
                    batches._save(self.root,state)
                    if action=='stop':batches.request_stop(self.session,self.library)
                    else:now += timedelta(seconds=60)
                    result=self.step()
                    self.assertEqual(result['status'],'stopped' if action=='stop' else 'budget_reached')
                    self.assertEqual(result['submitted_batches'],0)
                    self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_one_pending_task_and_total_budgets_bound_every_batch(self):
        self.start({'max_batches':3,'max_pages':5,'max_bytes':10,'batch_pages':4})
        task,request=self.publish()
        self.assertEqual(request['operation'],'sync');self.assertTrue(request['args']['refresh_lists'])
        self.assertEqual(request['args']['max_pages'],4);self.assertEqual(request['args']['max_bytes'],10)
        for _ in range(3):self.assertEqual(self.step()['pending_task_id'],task)
        value=self.finish(task,pages=4,byte_count=6)
        self.assertEqual(value['completed_batches'],1)
        task,request=self.publish()
        self.assertFalse(request['args']['refresh_lists'])
        self.assertEqual(request['args']['max_pages'],1);self.assertEqual(request['args']['max_bytes'],4)
        self.finish(task,pages=1,byte_count=4,remaining=0)
        value=self.step()
        self.assertEqual(value['status'],'budget_reached')
        self.assertEqual(value['downloaded_pages'],5);self.assertEqual(value['downloaded_bytes'],10)
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_two_image_workers_are_checkpointed_and_preserved_on_resume(self):
        self.start({'download_workers':2,'archive_only':True,'refresh_lists':False})
        task,request=self.publish()
        self.assertEqual(request['operation'],'archive_discovered')
        self.assertEqual(request['args']['download_workers'],2)
        self.assertEqual(self.step()['budgets']['download_workers'],2)
        deadline=self.step()['submit_before']
        resumed=batches._start(self.root,{'download_workers':1},True)
        self.assertEqual(resumed['budgets']['download_workers'],2)
        self.assertEqual(resumed['submit_before'],deadline)
        self.finish(task,pending=10,remaining=0,archive_only=True)
        _,request=self.publish()
        self.assertEqual(request['args']['download_workers'],2)

    def test_legacy_single_worker_checkpoint_keeps_pending_request_unchanged(self):
        self.start()
        task,request=self.publish()
        self.assertNotIn('download_workers',request['args'])
        state=json.loads((self.root/'backlog.json').read_text())
        state['budgets'].pop('download_workers')
        atomic_json(self.root/'backlog.json',state)
        self.assertEqual(self.step()['pending_task_id'],task)
        self.assertEqual(self.step()['budgets']['download_workers'],1)

    def test_reserved_identity_survives_crash_before_or_after_publication(self):
        self.start()
        first=self.step();task=first['pending_task_id']
        original=batches._load(self.root)['pending']['options']
        # Simulate a crash after publication but before the checkpoint's phase
        # update. Reserved submission must observe the same existing request.
        submit_job(self.session,'sync',original,_task_id=task)
        batches._start(self.root,None,True)
        self.assertEqual(self.step()['pending_task_id'],task)
        self.assertEqual([p.stem for p in (self.root/'requests').glob('*.json')],[task])
        self.assertEqual(batches.status(self.session,self.library)['submitted_batches'],1)
        self.finish(task)
        self.assertEqual(batches.status(self.session,self.library)['completed_batches'],1)
        self.assertEqual(self.step()['completed_batches'],1)

    def test_missing_task_is_preserved_without_replacement_or_completion(self):
        self.start();task,_=self.publish()
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['status'],'paused');self.assertEqual(value['code'],'task_unavailable')
        self.assertEqual(value['pending_task_id'],task)
        batches._start(self.root,None,True)
        self.assertEqual(self.step()['pending_task_id'],task)
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        with self.assertRaises(ArchiveError):batches._start(self.root,{},False)

    def test_login_failure_and_cancelled_run_pause_without_next_submission(self):
        self.start();task,_=self.publish()
        value=self.finish(task,error='login_required')
        self.assertEqual(value['code'],'login_required');self.assertEqual(value['status'],'paused')
        self.assertNotIn('PRIVATE',json.dumps(value));self.assertIsNone(self.step()['pending_task_id'])
        batches._start(self.root,None,True)
        task,_=self.publish();value=self.finish(task,status='cancelled')
        self.assertEqual(value['code'],'cancelled');self.assertEqual(value['completed_batches'],1)
        self.assertIsNone(self.step()['pending_task_id'])

    def test_error_without_partial_counters_reserves_budget_before_resume(self):
        self.start({'max_batches':5,'max_pages':1,'max_bytes':10})
        task,_=self.publish();value=self.finish(task,error='job_failed')
        self.assertEqual(value['downloaded_pages'],0);self.assertEqual(value['downloaded_bytes'],0)
        self.assertEqual(value['unreported_reserved_pages'],1);self.assertEqual(value['unreported_reserved_bytes'],10)
        # Older checkpoints did not record unreported allocations. Their
        # unaccounted terminal submissions receive the same conservative bound.
        legacy=batches._load(self.root)
        legacy.pop('unreported_reserved_pages');legacy.pop('unreported_reserved_bytes')
        batches._save(self.root,legacy)
        batches._start(self.root,None,True)
        value=self.step()
        self.assertEqual(value['status'],'budget_reached');self.assertIsNone(value['pending_task_id'])
        self.assertEqual(value['submitted_batches'],1);self.assertEqual(list((self.root/'requests').iterdir()),[])

    @patch('builtins.print')
    def test_cancelled_sync_before_metadata_refresh_pauses_the_controller_cleanly(self,output):
        library=self.library
        class StopDuringAuthentication(AccountAdapter):
            def account_identity(self):
                identity=super().account_identity()
                with Inventory(library,read_only=True) as inventory:
                    row=inventory.db.execute('SELECT id FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
                request_stop(library,row['id'])
                return identity
        with Inventory(library) as inventory:inventory.observe('42','S',[],'fixture')
        adapter=StopDuringAuthentication();self.start({'refresh_lists':False})
        task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
        result=sync(adapter,library,**options)
        self.assertEqual(result['status'],'cancelled');self.assertNotIn('refresh',result)
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['status'],'paused');self.assertEqual(value['code'],'cancelled')
        self.assertIsNone(value['pending_task_id']);self.assertEqual(value['completed_batches'],1)
        self.assertEqual(adapter.calls,['identity']);self.assertEqual(adapter.downloads,[])
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    @patch('builtins.print')
    def test_network_pause_accounts_cached_pages_and_resumes_with_fresh_access(self,output):
        from comic_archive.errors import NetworkUnavailable
        adapter=AccountAdapter();adapter.access={'81':'tapas_free'}
        with Inventory(self.library) as inventory:inventory.observe('42','S',[],'fixture')
        self.start({'refresh_lists':False,'max_pages':5,'batch_pages':4})
        task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
        retrieve=adapter.retrieve
        def fail_second_page(page,max_bytes=None):
            if page.order==2:raise NetworkUnavailable('PRIVATE_CONNECTION_DETAILS')
            return retrieve(page,max_bytes=max_bytes)
        adapter.retrieve=fail_second_page
        result=sync(adapter,self.library,**options)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['downloaded_pages'],1)
        self.assertNotIn('PRIVATE',json.dumps(result))
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['code'],'network_unavailable');self.assertEqual(value['status'],'paused')
        self.assertEqual(value['downloaded_pages'],1);self.assertEqual(value['completed_batches'],1)
        self.assertIsNone(self.step()['pending_task_id'])
        adapter.retrieve=retrieve;adapter.calls.clear();batches._start(self.root,None,True)
        task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
        result=archive_discovered(adapter,self.library,**options)
        self.assertEqual(result['archived_episode_ids'],['81']);self.assertEqual(result['downloaded_pages'],1)
        self.assertEqual(adapter.downloads,[1,2]);self.assertIn('identity',adapter.calls)
        self.assertIn(('capture','81'),adapter.calls)
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['downloaded_pages'],2);self.assertEqual(value['archived_episodes'],1)

    @patch('builtins.print')
    def test_metadata_gateway_pause_retains_exact_completed_counters_and_no_successor(self,output):
        from comic_archive.errors import NetworkUnavailable
        adapter=AccountAdapter();adapter.access={'81':'tapas_free','82':'tapas_free'}
        with Inventory(self.library) as inventory:inventory.observe('42','S',[],'fixture')
        self.start({'refresh_lists':False,'max_pages':5,'batch_pages':4})
        task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
        capture=adapter.capture_automatic
        def fail_next_episode(url):
            if url.endswith('/82'):
                failure=NetworkUnavailable('PRIVATE_METADATA_RESPONSE')
                failure.source_http_status=503
                raise failure
            return capture(url)
        adapter.capture_automatic=fail_next_episode
        result=sync(adapter,self.library,**options)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['source_http_status'],503)
        self.assertEqual(result['archived_episode_ids'],['81']);self.assertEqual(result['downloaded_pages'],2)
        self.assertNotIn('PRIVATE',json.dumps(result))
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual(value['status'],'paused');self.assertEqual(value['code'],'network_unavailable')
        self.assertEqual(value['archived_episodes'],1);self.assertEqual(value['downloaded_pages'],2)
        self.assertEqual(value['completed_batches'],1);self.assertIsNone(self.step()['pending_task_id'])
        self.assertEqual(adapter.downloads,[1,2])
        with Inventory(self.library,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['82'])


    def test_stop_before_publication_and_while_queued_never_loses_an_existing_task(self):
        self.start();self.step()
        batches.request_stop(self.session,self.library)
        self.assertEqual(self.step()['status'],'stopped')
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        self.start();task,_=self.publish()
        batches.request_stop(self.session,self.library)
        value=self.step();self.assertEqual(value['status'],'stopped');self.assertEqual(value['pending_task_id'],task)
        with self.assertRaises(ArchiveError):batches._start(self.root,{},False)
        batches._start(self.root,None,True)
        self.finish(task)
        self.assertEqual(batches.status(self.session,self.library)['completed_batches'],1)

    def test_expired_window_preserves_queued_task_and_later_accounts_for_its_result(self):
        now=datetime(2026,10,3,tzinfo=timezone.utc)
        with patch('comic_archive.batches._now',return_value=now):
            self.start({'max_seconds':10});task,_=self.publish()
        with patch('comic_archive.batches._now',return_value=now+timedelta(seconds=20)):
            value=self.step();self.assertEqual(value['status'],'budget_reached');self.assertEqual(value['pending_task_id'],task)
            batches._start(self.root,None,True)
            self.finish(task)
            value=self.step();self.assertEqual(value['status'],'budget_reached')
            self.assertIsNone(value['pending_task_id']);self.assertEqual(value['downloaded_pages'],1)

    def test_empty_known_snapshot_never_claims_account_completion(self):
        self.start();task,_=self.publish()
        value=self.finish(task,pages=0,byte_count=0,pending=0,remaining=0)
        self.assertEqual(value['status'],'known_backlog_drained')
        self.assertFalse(value['account_inventory_complete'])
        self.assertIsNone(self.step()['pending_task_id'])

    def test_metadata_progress_can_continue_but_a_repeated_stall_pauses(self):
        self.start();task,_=self.publish();self.finish(task,pages=0,byte_count=0,remaining=100)
        task,_=self.publish();self.assertEqual(self.finish(task,pages=0,byte_count=0,remaining=50)['status'],'running')
        task,_=self.publish();value=self.finish(task,pages=0,byte_count=0,remaining=50)
        self.assertEqual(value['status'],'paused');self.assertEqual(value['code'],'no_progress')

    def test_completed_scan_switches_to_archiving_until_new_series_are_reported(self):
        self.start({'max_batches':4});task,_=self.publish();self.finish(task,remaining=0)
        task,request=self.publish()
        self.assertEqual(request['operation'],'archive_discovered')
        self.assertNotIn('scan_limit',request['args']);self.assertNotIn('refresh_lists',request['args'])
        value={'task_id':task,'status':'ok','result':{'status':'limited','downloaded_pages':1,
            'downloaded_bytes':2,'pending_episode_count':9,'archived_episode_ids':['82'],
            'inventory':{'scan_remaining_series_count':5}}}
        atomic_json(self.root/'responses'/f'{task}.json',value)
        (self.root/'requests'/f'{task}.json').unlink()
        self.assertEqual(self.step()['completed_batches'],2)
        task,request=self.publish()
        self.assertEqual(request['operation'],'sync')
        self.assertFalse(request['args']['refresh_lists'])

    def test_archive_only_starts_without_scan_and_resumes_exact_task_with_remaining_refreshes(self):
        self.start({'archive_only':True,'refresh_lists':False})
        state=batches._load(self.root);state['previous_scan_remaining']=612;batches._save(self.root,state)
        task,request=self.publish()
        self.assertEqual(request['operation'],'archive_discovered')
        self.assertNotIn('scan_limit',request['args']);self.assertNotIn('refresh_lists',request['args'])
        batches._start(self.root,None,True)
        self.assertEqual(self.step()['pending_task_id'],task)
        self.assertEqual(len(list((self.root/'requests').glob('*.json'))),1)

    @patch('builtins.print')
    def test_explicit_archive_only_rechecks_denial_without_series_enumeration_and_keeps_scan_gap(self,output):
        with Inventory(self.library) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'Unlocked first','order':1,'access_state':'tapas_unlocked'},
                {'id':'82','title':'Unlocked','order':2,'access_state':'tapas_unlocked'}],'fixture')
            inventory.scan_candidates()
        adapter=AccountAdapter();adapter.access['81']='tapas_unlocked'
        self.start({'max_batches':2,'batch_limit':1,'archive_only':True,'refresh_lists':False})
        for index in range(2):
            task,request=self.publish();self.assertEqual(request['operation'],'archive_discovered')
            options=request['args'].copy();options.pop('request_interval')
            if index:adapter.denied.add('82')
            result=archive_discovered(adapter,self.library,**options)
            atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
            (self.root/'requests'/f'{task}.json').unlink();value=self.step()
        self.assertEqual(result['unavailable_episode_ids'],['82']);self.assertEqual(adapter.downloads,[1,2])
        self.assertFalse(any(isinstance(c,tuple) and c[0] in {'library','series'} for c in adapter.calls))
        self.assertEqual([c for c in adapter.calls if isinstance(c,tuple) and c[0]=='capture'],[('capture','81'),('capture','82')])
        self.assertEqual(value['status'],'known_backlog_drained');self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(batches._load(self.root)['previous_scan_remaining'],1);self.assertEqual(verify(self.library),[])

    def test_archive_only_invalid_configuration_and_legacy_budget_are_safe(self):
        for values in ({'archive_only':'yes'},{'archive_only':True},{'archive_only':True,'refresh_lists':True}):
            with self.assertRaises(ArchiveError):self.start(values)
        self.start();state=batches._load(self.root);state['budgets'].pop('archive_only');batches._save(self.root,state)
        self.assertFalse(batches._load(self.root)['budgets']['archive_only'])
        _,request=self.publish();self.assertEqual(request['operation'],'sync')

    def test_archive_only_cli_disables_discovery_without_starting_another_browser(self):
        args=parser().parse_args(['run-backlog','--archive-only'])
        with patch('comic_archive.batches.run',return_value={'status':'budget_reached'}) as runner,patch('builtins.print'):
            self.assertEqual(run(args),0)
        values=runner.call_args.kwargs['values']
        self.assertTrue(values['archive_only']);self.assertFalse(values['refresh_lists'])

    def test_older_sync_checkpoint_resumes_without_an_operation_field(self):
        self.start();reserved=self.step()
        state=batches._load(self.root);state['pending'].pop('operation');batches._save(self.root,state)
        batches._start(self.root,None,True)
        value=self.step();self.assertEqual(value['pending_task_id'],reserved['pending_task_id'])
        request=json.loads((self.root/'requests'/(value['pending_task_id']+'.json')).read_text())
        self.assertEqual(request['operation'],'sync')

    @patch('builtins.print')
    def test_finishing_a_scan_never_grants_access_to_later_archive_only_batches(self,output):
        with Inventory(self.library) as inventory:inventory.observe('42','Series',[],'fixture')
        adapter=AccountAdapter();adapter.access['81']='tapas_unlocked'
        self.start({'max_batches':2,'batch_limit':1,'refresh_lists':False})
        for index in range(2):
            task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
            if index:
                self.assertEqual(request['operation'],'archive_discovered');adapter.denied.add('82')
            result={'sync':sync,'archive_discovered':archive_discovered}[request['operation']](adapter,self.library,**options)
            atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
            (self.root/'requests'/f'{task}.json').unlink();value=self.step()
        self.assertEqual(result['unavailable_episode_ids'],['82'])
        self.assertEqual(adapter.downloads,[1,2])
        self.assertEqual(value['archived_episodes'],1);self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(verify(self.library),[])

    def test_invalid_result_or_over_budget_metrics_retains_task_for_inspection(self):
        self.start({'batch_pages':1});task,_=self.publish()
        value=self.finish(task,pages=2)
        self.assertEqual(value['code'],'invalid_result');self.assertEqual(value['pending_task_id'],task)
        self.assertEqual(value['downloaded_pages'],0)

    def test_busy_worker_other_requests_and_audit_lock_delay_publication(self):
        self.start()
        atomic_json(self.root/'status.json',{'state':'busy','task_id':'job-'+'a'*24})
        self.assertIsNone(self.step()['pending_task_id'])
        atomic_json(self.root/'status.json',{'state':'ready'})
        other=self.root/'requests'/('job-'+'b'*24+'.json');atomic_json(other,{'operation':'check_session','args':{}})
        self.assertIsNone(self.step()['pending_task_id']);other.unlink()
        with patch('comic_archive.batches.LibraryLock.__enter__',side_effect=LibraryBusy('Busy')):
            with self.assertRaises(LibraryBusy):self.step()
        self.assertEqual(batches.status(self.session,self.library)['submitted_batches'],0)

    def test_unsafe_state_scope_and_budget_values_are_rejected_without_source_requests(self):
        for values in ({'max_batches':0},{'max_bytes':True},{'request_interval':float('nan')},
                {'max_seconds':86401},{'free_only':'yes'},{'url':'PRIVATE'}):
            with self.assertRaises(ArchiveError):batches.budgets(values)
        with self.assertRaises(ArchiveError):batches.status(self.session,self.project/'Other')
        self.start()
        state=batches._load(self.root);state['cookie']='PRIVATE';batches._save(self.root,state)
        self.assertNotIn('PRIVATE',json.dumps(batches.status(self.session,self.library)))
        saved=self.root/'backlog.json';saved.unlink();saved.symlink_to(self.project/'outside')
        with self.assertRaises(ArchiveError):batches.status(self.session,self.library)
        self.assertFalse((self.project/'outside').exists())

    def test_only_one_runtime_consumes_each_controller_and_ctrl_c_checkpoint_remains(self):
        entered=threading.Event();result=[]
        thread=threading.Thread(target=lambda:result.append(batches.run(self.session,self.library,
            progress=lambda _:entered.set())))
        thread.start();self.addCleanup(lambda:thread.join(5))
        self.assertTrue(entered.wait(5))
        with self.assertRaises(LibraryBusy):batches.run(self.session,self.library,progress=lambda _:None)
        batches.request_stop(self.session,self.library);thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'stopped')
        with patch('comic_archive.batches.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):batches.run(self.session,self.library,progress=lambda _:None)
        self.assertEqual(batches.status(self.session,self.library)['status'],'interrupted')

    def test_symlinked_queue_directories_are_rejected_before_reading_or_writing(self):
        for name in ('requests','responses'):
            with self.subTest(directory=name):
                target=self.project/('outside-'+name);target.mkdir()
                original=self.root/name;original.rmdir();original.symlink_to(target,target_is_directory=True)
                try:
                    with self.assertRaises(ArchiveError):batches.status(self.session,self.library)
                    with self.assertRaises(ArchiveError):batches.run(self.session,self.library,progress=lambda _:None)
                    self.assertEqual(list(target.iterdir()),[])
                finally:original.unlink();original.mkdir()

    def test_cli_routes_to_controller_without_starting_another_browser(self):
        args=parser().parse_args(['--request-interval','.5','run-backlog','--max-batches','2','--watched-only'])
        with patch('comic_archive.batches.run',return_value={'status':'budget_reached'}) as runner,patch('builtins.print'):
            self.assertEqual(run(args),0)
        self.assertEqual(runner.call_args.kwargs['values']['request_interval'],.5)
        self.assertFalse(runner.call_args.kwargs['values']['refresh_lists'])
        self.assertEqual(runner.call_args.kwargs['values']['max_batches'],2)
        with self.assertRaises(ArchiveError):run(parser().parse_args(['--session-dir','Other','backlog-status']))

    def test_new_bounded_window_preserves_previous_counters_deadline_and_sanitized_history(self):
        self.start({'refresh_lists':False})
        task,_=self.publish()
        self.finish(task,pages=3,byte_count=900)
        previous=batches._load(self.root)
        previous.update(status='paused',code='network_unavailable',cookie='PRIVATE')
        batches._save(self.root,previous)
        batches._start(self.root,None,True)
        self.assertFalse((self.root/'backlog-history').exists())
        self.assertEqual(batches._load(self.root)['submit_before'],previous['submit_before'])
        fresh=batches._start(self.root,{'max_pages':7,'refresh_lists':False},False)
        files=list((self.root/'backlog-history').glob('*.json'))
        self.assertEqual(len(files),1)
        saved=json.loads(files[0].read_text())
        self.assertEqual(saved['started_at'],previous['started_at'])
        self.assertEqual(saved['submit_before'],previous['submit_before'])
        self.assertEqual(saved['downloaded_pages'],3)
        self.assertEqual(saved['downloaded_bytes'],900)
        self.assertNotIn('PRIVATE',json.dumps(saved))
        self.assertEqual(files[0].stat().st_mode&0o777,PRIVATE_FILE)
        self.assertEqual(files[0].parent.stat().st_mode&0o777,PRIVATE_FOLDER)
        self.assertEqual(fresh['downloaded_pages'],0)
        self.assertEqual(fresh['submitted_batches'],0)
        self.assertEqual(fresh['budgets']['max_pages'],7)

    def test_history_symlink_does_not_overwrite_the_previous_checkpoint(self):
        self.start()
        before=(self.root/'backlog.json').read_bytes()
        outside=self.project/'outside-history';outside.mkdir()
        (self.root/'backlog-history').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ArchiveError):batches._start(self.root,None,False)
        self.assertEqual((self.root/'backlog.json').read_bytes(),before)
        self.assertEqual(list(outside.iterdir()),[])

    def test_paused_status_exposes_expired_submission_window_without_resetting_it(self):
        state=self.start({'max_seconds':10})
        state.update(status='paused',code='network_unavailable')
        batches._save(self.root,state)
        before=(self.root/'backlog.json').read_bytes()
        deadline=datetime.fromisoformat(state['submit_before'])
        with patch('comic_archive.batches._now',return_value=deadline-timedelta(seconds=1)):
            self.assertTrue(batches.status(self.session,self.library)['submission_window_open'])
        with patch('comic_archive.batches._now',return_value=deadline):
            self.assertFalse(batches.status(self.session,self.library)['submission_window_open'])
        self.assertEqual((self.root/'backlog.json').read_bytes(),before)

    @patch('builtins.print')
    def test_automatic_recovery_rechecks_authorization_before_using_cached_pages(self,output):
        from comic_archive.errors import NetworkUnavailable
        adapter=AccountAdapter();adapter.access={'81':'tapas_free'}
        with Inventory(self.library) as inventory:inventory.observe('42','S',[],'fixture')
        self.start({'refresh_lists':False,'recover_network':True,'max_pages':5,'batch_pages':4})
        task,request=self.publish();options=request['args'].copy();options.pop('request_interval')
        retrieve=adapter.retrieve
        def fail_second_page(page,max_bytes=None):
            if page.order==2:raise NetworkUnavailable('PRIVATE_CONNECTION_DETAILS')
            return retrieve(page,max_bytes=max_bytes)
        adapter.retrieve=fail_second_page
        result=sync(adapter,self.library,**options)
        self.assertEqual(result['downloaded_pages'],1)
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        paused=self.step()
        self.assertIsNotNone(paused['network_retry_after'])
        adapter.retrieve=retrieve;adapter.calls.clear();adapter.denied.add('81')
        with patch('comic_archive.batches._now',return_value=datetime.fromisoformat(paused['network_retry_after'])):
            task,request=self.publish()
        options=request['args'].copy();options.pop('request_interval')
        result=archive_discovered(adapter,self.library,**options)
        self.assertIn('identity',adapter.calls)
        self.assertIn(('capture','81'),adapter.calls)
        self.assertEqual(adapter.downloads,[1])
        self.assertEqual(result['downloaded_pages'],0)
        self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(result['unavailable_episode_ids'],['81'])
        self.assertEqual(list(self.library.rglob('*.cbz')),[])

    @patch('builtins.print')
    def test_consecutive_real_archive_engine_batches_preserve_two_portable_episodes(self,output):
        with Inventory(self.library) as inventory:inventory.observe('42','Series',[],'fixture')
        adapter=AccountAdapter()
        self.start({'max_batches':2,'batch_limit':1,'refresh_lists':False})
        for _ in range(2):
            task,request=self.publish()
            options=request['args'].copy();options.pop('request_interval')
            operation={'sync':sync,'archive_discovered':archive_discovered}[request['operation']]
            result=operation(adapter,self.library,**options)
            atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
            (self.root/'requests'/f'{task}.json').unlink()
            value=self.step()
        self.assertEqual(value['status'],'known_backlog_drained')
        self.assertEqual(value['archived_episodes'],2);self.assertEqual(value['downloaded_pages'],4)
        self.assertEqual(len(list(self.library.glob('*/*.cbz'))),2)
        self.assertEqual(verify(self.library),[])
        self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(adapter.calls.count(('series','42')),1)
        self.assertEqual(adapter.calls.count('identity'),2)
        self.assertEqual([call for call in adapter.calls if isinstance(call,tuple) and call[0]=='capture'],[('capture','82'),('capture','81')])


    def test_recovery_limits_outside_the_supported_range_are_refused(self):
        for values in ({'network_recovery_limit':3},{'network_recovery_limit':-1},{'network_recovery_limit':True},
                       {'source_recovery_limit':5},{'source_recovery_limit':-1},{'source_recovery_limit':1.0}):
            with self.subTest(values=values),self.assertRaises(ArchiveError):batches.budgets(values)
        limits=batches.budgets({'network_recovery_limit':0,'source_recovery_limit':4})
        self.assertEqual((limits['network_recovery_limit'],limits['source_recovery_limit']),(0,4))

    def edit(self,change):
        path=self.root/'backlog.json';state=json.loads(path.read_text());change(state);atomic_json(path,state)

    def test_a_damaged_or_tampered_checkpoint_is_refused_and_left_exactly_as_it_was(self):
        self.start();self.step()  # A reserved batch is pending, so its options are checked too.
        self.assertIsNotNone(batches.status(self.session,self.library)['pending_task_id'])
        def window_too_long(state):
            start=datetime.fromisoformat(state['started_at'])
            state['submit_before']=(start+timedelta(seconds=state['budgets']['max_seconds']+1)).isoformat()
        def discovered_while_scanning(state):
            state['pending'].update(operation='archive_discovered')
            for option in ('scan_limit','refresh_lists'):state['pending']['options'].pop(option)
        cases={
            'schema version':lambda s:s.update(schema_version=2),
            'unknown status':lambda s:s.update(status='bogus'),
            'unknown reason':lambda s:s.update(code='bogus'),
            'stop flag that is not a boolean':lambda s:s.update(stop_requested='no'),
            'recovery delay below the minimum':lambda s:s.update(source_recovery_delay=1),
            'recovery attempts above the limit':lambda s:s.update(source_recovery_attempts=5),
            'recovery episode that is not a source id':lambda s:s.update(source_recovery_episode='abc'),
            'reader wait without a reader refusal':lambda s:s.update(source_recovery_wait=s['started_at'],
                source_recovery_attempts=1,source_recovery_episode='81'),
            'asset wait from before the window began':lambda s:s.update(asset_retry_after='2000-01-01T00:00:00+00:00',pending=None),
            'asset wait beside a pending batch':lambda s:s.update(asset_retry_after=s['submit_before']),
            'source wait from before the window began':lambda s:s.update(source_retry_after='2000-01-01T00:00:00+00:00'),
            'network attempts above the limit':lambda s:s.update(network_recovery_attempts=3),
            'network wait without a network pause':lambda s:s.update(network_retry_after=s['started_at']),
            'negative counter':lambda s:s.update(downloaded_pages=-1),
            'negative reservation':lambda s:s.update(unreported_reserved_pages=-1),
            'more completed than submitted batches':lambda s:s.update(completed_batches=5),
            'window longer than its budget':window_too_long,
            'negative previous count':lambda s:s.update(previous_pending_count=-1),
            'timestamp without a time zone':lambda s:s.update(
                started_at=datetime.fromisoformat(s['started_at']).astimezone().replace(tzinfo=None).isoformat()),
            'timestamp that is not text':lambda s:s.update(started_at=5),
            'task id that is not a job id':lambda s:s['pending'].update(task_id='job-x'),
            'phase that does not exist':lambda s:s['pending'].update(phase='done'),
            'unknown operation':lambda s:s['pending'].update(operation='other'),
            'archive-only budgets beside a sync batch':lambda s:s['budgets'].update(archive_only=True,refresh_lists=False),
            'retry episode on a sync batch':lambda s:s['pending']['options'].update(retry_episode_id='81'),
            'speed that differs from the budgets':lambda s:s['pending']['options'].update(speed='fast'),
            'image workers that differ from the budgets':lambda s:s['pending']['options'].update(download_workers=2),
            'discovered-only batch while the scan is incomplete':discovered_while_scanning,
            'option that is not part of a batch':lambda s:s['pending']['options'].update(url='https://tapas.io/PRIVATE'),
            'batch larger than its budget':lambda s:s['pending']['options'].update(limit=10**6),
            'scan limit of zero':lambda s:s['pending']['options'].update(scan_limit=0),
            'batch time longer than its budget':lambda s:s['pending']['options'].update(max_seconds=10**6),
            'access filter that differs from the budgets':lambda s:s['pending']['options'].update(free_only=True),
            'request spacing that differs from the budgets':lambda s:s['pending']['options'].update(request_interval=7)}
        path=self.root/'backlog.json';original=path.read_bytes()
        for name,change in cases.items():
            with self.subTest(name):
                path.write_bytes(original);self.edit(change);damaged=path.read_bytes()
                with self.assertRaises(ArchiveError):batches.status(self.session,self.library)
                with self.assertRaises(ArchiveError):self.step()
                self.assertEqual(path.read_bytes(),damaged)
                self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_checkpoint_files_that_are_oversized_not_an_object_or_not_a_file_are_refused(self):
        self.start();path=self.root/'backlog.json';original=path.read_bytes()
        for name,content in (('oversized',json.dumps({'pad':'x'*20000})),('a list','[]'),('not json','{bad'),('a folder',None)):
            with self.subTest(name):
                path.unlink()
                if content is None:path.mkdir()
                else:path.write_text(content)
                try:
                    with self.assertRaises(ArchiveError):batches.status(self.session,self.library)
                    with self.assertRaises(ArchiveError):self.step()
                finally:
                    if content is None:path.rmdir()
                    else:path.unlink()
                    path.write_bytes(original)
        self.assertEqual(batches.status(self.session,self.library)['status'],'running')

    def test_nothing_can_be_stopped_resumed_or_advanced_before_a_run_was_started(self):
        with self.assertRaises(ArchiveError):batches.request_stop(self.session,self.library)
        with self.assertRaises(ArchiveError):batches._start(self.root,None,True)
        with self.assertRaises(ArchiveError):self.step()
        self.assertEqual(batches.status(self.session,self.library),{'configured':False,'account_inventory_complete':False})
        self.assertFalse((self.root/'backlog.json').exists())
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_observing_without_a_pending_batch_neither_submits_nor_changes_the_run(self):
        self.start();before=(self.root/'backlog.json').read_bytes()
        value=batches.step(self.session,self.library,observe_only=True)
        self.assertEqual((value['status'],value['submitted_batches'],value['pending_task_id']),('running',0,None))
        self.assertEqual((self.root/'backlog.json').read_bytes(),before)
        self.assertEqual(list((self.root/'requests').iterdir()),[])

    def test_stop_asked_after_a_batch_reached_the_queue_but_before_its_checkpoint_never_loses_it(self):
        self.start();task=self.step()['pending_task_id']
        original=batches._load(self.root)['pending']['options']
        # The crash came after publication and before the phase was recorded as queued.
        submit_job(self.session,'sync',original,_task_id=task)
        batches.request_stop(self.session,self.library)
        value=self.step()
        self.assertEqual((value['status'],value['pending_task_id']),('stopped',task))
        self.assertEqual(batches._load(self.root)['pending']['phase'],'queued')
        self.assertEqual([p.stem for p in (self.root/'requests').glob('*.json')],[task])
        batches._start(self.root,None,True)
        value=self.finish(task)
        self.assertEqual((value['status'],value['completed_batches'],value['downloaded_pages'],value['pending_task_id']),
                         ('running',1,1,None))

    def test_a_batch_the_worker_finished_before_the_checkpoint_caught_up_is_accounted_even_when_stop_was_asked(self):
        self.start();task=self.step()['pending_task_id']
        submit_job(self.session,'sync',batches._load(self.root)['pending']['options'],_task_id=task)
        batches.request_stop(self.session,self.library)
        value=self.finish(task,pages=2,byte_count=7)
        self.assertEqual((value['status'],value['completed_batches'],value['downloaded_pages'],value['downloaded_bytes'],
                          value['pending_task_id']),('stopped',1,2,7,None))

    def test_a_receipt_without_the_metadata_count_is_rejected_and_the_task_is_kept(self):
        self.start();task,_=self.publish()
        result={'status':'limited','downloaded_pages':0,'downloaded_bytes':0,'pending_episode_count':3,'archived_episode_ids':[]}
        atomic_json(self.root/'responses'/f'{task}.json',{'task_id':task,'status':'ok','result':result})
        (self.root/'requests'/f'{task}.json').unlink()
        value=self.step()
        self.assertEqual((value['status'],value['code'],value['pending_task_id'],value['completed_batches']),
                         ('paused','invalid_result',task,0))

    def test_a_library_write_in_progress_delays_publication_until_it_ends(self):
        self.start();held=threading.Event();release=threading.Event()
        def hold():
            with LibraryLock(self.library):
                held.set();release.wait(5)
        thread=threading.Thread(target=hold);thread.start()
        self.addCleanup(thread.join,5);self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        value=self.step()
        self.assertEqual((value['status'],value['pending_task_id'],value['submitted_batches']),('running',None,0))
        self.assertEqual(list((self.root/'requests').iterdir()),[])
        release.set();thread.join(5)
        value=self.step()
        self.assertIsNotNone(value['pending_task_id']);self.assertEqual(value['submitted_batches'],1)

    def test_go_after_the_automatic_reader_retries_ran_out_tries_the_refused_episode_alone_first(self):
        self.start()
        self.edit(lambda s:s.update(status='paused',code='source_refused',source_recovery_episode='81',
                                    source_recovery_attempts=4,source_recovery_delay=240))
        value=batches._start(self.root,None,True)
        self.assertEqual((value['status'],value['code'],value['source_recovery_episode'],value['source_recovery_attempts'],
                          value['source_recovery_delay']),('running',None,'81',0,240))
        self.step()
        options=batches._load(self.root)['pending']['options']
        self.assertEqual((options['limit'],options['retry_episode_id']),(1,'81'))

    def test_stop_while_reader_recovery_is_held_ends_the_recovery_instead_of_probing_later(self):
        self.start()
        self.edit(lambda s:s.update(status='paused',code='source_refused',stop_requested=True,source_recovery_episode='81'))
        value=self.step()
        self.assertEqual((value['status'],value['code'],value['source_recovery_episode'],value['source_recovery_wait']),
                         ('stopped',None,None,None))
        self.assertEqual(self.step()['status'],'stopped')
        self.assertEqual(list((self.root/'requests').iterdir()),[])


if __name__=='__main__':unittest.main()
