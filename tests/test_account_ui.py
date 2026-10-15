from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import account_export, account_ui, batches, completion
from comic_archive.cancellation import RunCancellation
from comic_archive.inventory import Inventory
from comic_archive.jobs import job_progress
from comic_archive.local_web import LOCAL_OWNER, LocalApplication, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError
from comic_archive.worker import private_json


class AccountUITests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.registry=LocalWorkspaceRegistry(self.root)
        self.workspace=self.registry.resolve(LOCAL_OWNER,self.registry.workspace_id)
        self.queue=self.workspace.session/'worker'
        (self.queue/'requests').mkdir(parents=True);(self.queue/'responses').mkdir()
        private_json(self.queue/'status.json',{'state':'ready'})
        private_json(self.workspace.session/'state.json',{'cookies':[],'origins':[]})
        self.app=LocalApplication(self.registry,8765)

    def call(self,path,method='GET',body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else b''
        env={'REMOTE_ADDR':'127.0.0.1','HTTP_HOST':'127.0.0.1:8765','wsgi.url_scheme':'http',
             'REQUEST_METHOD':method,'PATH_INFO':path,'QUERY_STRING':'','CONTENT_TYPE':'application/json',
             'CONTENT_LENGTH':str(len(data)),'wsgi.input':io.BytesIO(data),**(headers or {})}
        result=[];response=self.app(env,lambda status,values,exc_info=None:result.append((status,dict(values))))
        try:return *result[0],b''.join(response)
        finally:
            if hasattr(response,'close'):response.close()

    def browser_headers(self):
        _,headers,_=self.call('/')
        cookie={'HTTP_COOKIE':headers['Set-Cookie'].split(';',1)[0]}
        token=json.loads(self.call('/api/v1/context',headers=cookie)[2])['csrf_token']
        return cookie|{'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':token}

    def test_account_and_signin_routes_require_local_owner_cookie_csrf_and_empty_payload(self):
        headers=self.browser_headers();base='/api/v1/workspaces/'+self.workspace.id
        for path,method_name in [(base+'/account','start_account'),(base+'/session/signin','start_tapas_signin'),
                                 (base+'/account/pause','pause_account')]:
            with patch.object(self.registry,method_name,return_value={'status':'running'}) as action:
                self.assertEqual(self.call(path,'POST',{})[0],'401 Unauthorized')
                self.assertEqual(self.call(path,'POST',{},headers={'HTTP_COOKIE':headers['HTTP_COOKIE']})[0],'403 Forbidden')
                for body in ({'limit':1},{'url':'https://tapas.io/series/one/info'},{'password':'PRIVATE'}):
                    self.assertEqual(self.call(path,'POST',body,headers=headers)[0],'400 Bad Request')
                action.assert_not_called()
                self.assertEqual(self.call(path,'POST',{},headers=headers)[0],'200 OK')
                action.assert_called_once_with(LOCAL_OWNER,self.workspace.id)
        foreign='/api/v1/workspaces/'+'0'*32+'/account'
        self.assertEqual(self.call(foreign,'POST',{},headers=headers)[0],'404 Not Found')
        self.assertFalse(list((self.queue/'requests').iterdir()))

    def test_go_is_durable_account_wide_and_duplicate_go_does_not_start_another_run(self):
        before=self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertFalse(before['configured']);self.assertFalse((self.queue/'account-export.json').exists())
        result=self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['status'],'running');self.assertEqual(result['phase'],'lists')
        checkpoint=(self.queue/'account-export.json').read_bytes()
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual((self.queue/'account-export.json').read_bytes(),checkpoint)
        value=json.loads(checkpoint)
        self.assertFalse(value['window_budgets']['free_only'])
        self.assertFalse(list((self.queue/'requests').glob('*.json')))
        account_export.worker_tick(self.workspace.session,self.workspace.library)
        account_export.worker_tick(self.workspace.session,self.workspace.library)
        request=json.loads(next((self.queue/'requests').glob('*.json')).read_text())
        self.assertEqual(request['args']['categories'],['subscribed'])
        self.assertNotIn('url',request['args']);self.assertNotIn('id',request['args'])

    def pace(self,budgets):
        return budgets['speed'],budgets['download_workers'],budgets['request_interval'],budgets['batch_bytes']

    def test_go_runs_at_the_fast_speed_until_the_person_chooses_another(self):
        self.assertEqual((account_ui.DESKTOP_REQUEST_INTERVAL,account_ui.DESKTOP_BROWSER),(1.0,'qt'))
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        budgets=json.loads((self.queue/'account-export.json').read_bytes())['window_budgets']
        # The pace the earlier Go ran at against Tapas.
        self.assertEqual(self.pace(budgets),('fast',3,0.25,8*1024**3))
        self.assertTrue(budgets['archive_only'])

    def test_a_chosen_speed_is_kept_and_go_continues_the_saved_download_at_it(self):
        path=self.queue/'account-export.json'
        for chosen,pace in (('slow',('normal',1,1.0,batches.DEFAULTS['batch_bytes'])),
                            ('medium',('normal',4,0.25,batches.DEFAULTS['batch_bytes'])),
                            ('fast',('fast',3,0.25,8*1024**3))):
            self.assertEqual(self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,chosen)['speed'],chosen)
            # Kept with the library, so the next start of the app finds it.
            again=LocalWorkspaceRegistry(self.root)
            self.assertEqual(again.account_status(LOCAL_OWNER,again.workspace_id)['speed'],chosen)
            self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
            saved=json.loads(path.read_bytes())
            self.assertEqual(self.pace(saved['window_budgets']),pace)
            self.assertTrue(saved['window_budgets']['archive_only']);self.assertEqual(saved['phase'],'lists')
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)

    def test_a_speed_cannot_be_changed_under_a_running_download_and_an_unknown_one_is_refused(self):
        from comic_archive.errors import LibraryBusy
        with self.assertRaises(ArchiveError):self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'ludicrous')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        with self.assertRaises(LibraryBusy):self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'slow')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')

    def test_a_speed_file_this_app_cannot_read_means_the_fast_speed_and_go_still_works(self):
        for content in (b'ludicrous\n',b'\xff\xfe'):
            (self.workspace.session/account_ui.SPEED_FILE).write_bytes(content)
            self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(self.pace(json.loads((self.queue/'account-export.json').read_bytes())['window_budgets'])[0],'fast')

    def test_a_speed_that_cannot_be_saved_is_reported_and_the_earlier_choice_stands(self):
        self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'medium')
        with patch.object(account_ui.os,'replace',side_effect=OSError):
            with self.assertRaisesRegex(ArchiveError,'could not save that speed'):
                self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'slow')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'medium')

    def test_the_speed_route_takes_one_known_speed_from_the_signed_page_only(self):
        headers=self.browser_headers();path='/api/v1/workspaces/'+self.workspace.id+'/account/speed'
        self.assertEqual(self.call(path,'POST',{'speed':'slow'})[0],'401 Unauthorized')
        self.assertEqual(self.call(path,'POST',{'speed':'slow'},headers={'HTTP_COOKIE':headers['HTTP_COOKIE']})[0],'403 Forbidden')
        for body in ({},{'speed':'slow','limit':1},{'speed':['slow']},{'speed':'ludicrous'}):
            self.assertEqual(self.call(path,'POST',body,headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call(path,headers=headers)[0],'405 Method Not Allowed')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        status,_,body=self.call(path,'POST',{'speed':'slow'},headers=headers)
        self.assertEqual((status,json.loads(body)['speed']),('200 OK','slow'))
        self.assertEqual(json.loads(self.call('/api/v1/workspaces/'+self.workspace.id+'/account',headers=headers)[2])['speed'],'slow')

    def test_pause_then_go_keeps_the_same_pending_discovery_task(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        for _ in range(2):account_export.worker_tick(self.workspace.session,self.workspace.library)
        before=account_export.status(self.workspace.session,self.workspace.library)['pending_task_id']
        self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(account_export.status(self.workspace.session,self.workspace.library)['pending_task_id'],before)
        self.assertEqual(len(list((self.queue/'requests').glob('*.json'))),1)

    def test_first_signin_and_go_work_before_any_worker_has_reported(self):
        (self.queue/'status.json').unlink();(self.workspace.session/'state.json').unlink()
        with patch.object(self.registry.tapas_signin,'start',return_value={'status':'running'}) as start:
            self.assertEqual(self.registry.start_tapas_signin(LOCAL_OWNER,self.workspace.id),{'status':'running'})
        start.assert_called_once_with()
        private_json(self.workspace.session/'state.json',{})
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)

    def test_go_refuses_missing_session_busy_worker_or_signin_without_dispatch(self):
        (self.workspace.session/'state.json').unlink()
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        private_json(self.workspace.session/'state.json',{})
        private_json(self.queue/'status.json',{'state':'busy'})
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        private_json(self.queue/'status.json',{'state':'ready'})
        self.registry.tapas_signin._state={'status':'running'}
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.registry.tapas_signin._state={'status':'idle'}
        self.registry.cloud_signin._state={'status':'running'}
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertFalse((self.queue/'account-export.json').exists())

    def test_public_account_status_never_projects_private_receipts_or_unknown_errors(self):
        value={'configured':True,'status':'held','phase':'verify','code':'PRIVATE_RAW_MESSAGE',
               'pending_task_id':'PRIVATE_TOKEN','audit':{'verified_inventory_episode_count':7,
                'missing_recorded_accessible_episode_count':0,'episode_id':'PRIVATE_SOURCE_ID',
                'inventory_index_problem_count':True,'unindexed_archive_count':-1}}
        with patch.object(account_export,'status',return_value=value):result=account_ui.status(self.workspace)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('code',result)
        self.assertEqual(result['audit'],{'verified_inventory_episode_count':7,'missing_recorded_accessible_episode_count':0})
        self.assertFalse(result['coverage_verified'])

    def test_list_progress_and_activity_come_from_existing_checkpoints_without_dispatch(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        checkpoint=self.queue/'account-export.json'
        value=json.loads(checkpoint.read_text())
        started=account_ui.batches._instant(value['started_at']).timestamp()
        value['list_checks']=[{'category':'subscribed','complete':False,'boundary_observed':False,'observed_series_count':45}]
        value['category_index']=1
        private_json(checkpoint,value)
        path=self.workspace.library/'.archive-state/progress.json'
        private_json(path,{'status':'running','phase':'account_list','category':'updated',
                           'reading_list_page_count':2,'message':'PRIVATE_COOKIE','source_slug':'PRIVATE_SOURCE'})
        os.utime(path,(started+20,started+20))
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            result=self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['lists'],{'checked':1,'total':4,'current':'updated'})
        self.assertEqual(result['elapsed_seconds'],90)
        self.assertEqual(result['progress_age_seconds'],70)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertFalse(list((self.queue/'requests').glob('*.json')))
        os.utime(path,(started-1,started-1))
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))
        path.unlink();outside=self.root/'private-progress';outside.write_text('{"status":"running"}')
        path.symlink_to(outside)
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))

    def test_the_clock_counts_the_present_stretch_of_work_not_the_time_since_the_run_first_began(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        checkpoint=self.queue/'account-export.json'
        value=json.loads(checkpoint.read_text())
        began=account_ui.batches._instant(value['started_at'])-timedelta(hours=14)
        value['started_at']=began.isoformat()
        private_json(checkpoint,value)
        path=self.workspace.library/'.archive-state/progress.json'
        private_json(path,{'status':'running','phase':'account_list','category':'subscribed'})
        # The program was closed an hour into the run and is opened again now.
        os.utime(path,(began.timestamp()+3600,began.timestamp()+3600))
        reopened=LocalWorkspaceRegistry(self.root);opened=reopened.working_since
        self.assertGreater(opened,began.timestamp()+3600)
        with patch('comic_archive.account_ui.time.time',return_value=opened+30):
            result=reopened.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['elapsed_seconds'],30)
        self.assertNotIn('progress_age_seconds',result)
        os.utime(path,(opened+10,opened+10))
        with patch('comic_archive.account_ui.time.time',return_value=opened+30):
            self.assertEqual(reopened.account_status(LOCAL_OWNER,self.workspace.id)['progress_age_seconds'],20)
        # Go after a pause begins a new stretch.
        reopened.working_since=opened-5000
        self.assertEqual(reopened.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(reopened.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertGreaterEqual(reopened.working_since,opened)
        with patch('comic_archive.account_ui.time.time',return_value=reopened.working_since+7):
            self.assertEqual(reopened.account_status(LOCAL_OWNER,self.workspace.id)['elapsed_seconds'],7)

    def test_a_step_that_ended_and_a_wait_before_trying_again_are_not_reported_as_a_stall(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        path=self.workspace.library/'.archive-state/progress.json'
        started=account_ui.batches._instant(json.loads((self.queue/'account-export.json').read_text())['started_at']).timestamp()
        private_json(path,{'status':'paused','pause_reason':'network_unavailable'});os.utime(path,(started+10,started+10))
        at=lambda seconds:datetime.fromtimestamp(started+seconds,timezone.utc).isoformat()
        status=lambda:self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        with patch('comic_archive.account_ui.time.time',return_value=started+20):
            result=status()
            self.assertEqual(result['between_steps_seconds'],10)
            self.assertNotIn('progress_age_seconds',result);self.assertNotIn('retry',result)
            for window,retry in (({'network_retry_after':at(79.5)},{'reason':'connection','seconds':60}),
                                 ({'network_retry_after':None,'source_recovery_wait':at(320)},{'reason':'source','seconds':300}),
                                 # A wait that is over, one that cannot be read, and no window at all.
                                 ({'network_retry_after':at(20)},None),({'network_retry_after':'PRIVATE'},None),
                                 ({'network_retry_after':7},None),(None,None)):
                with patch('comic_archive.account_ui.batches._load',return_value=window):
                    self.assertEqual(status().get('retry'),retry,window)
            with patch('comic_archive.account_ui.batches._load',side_effect=ArchiveError('PRIVATE')):
                self.assertNotIn('retry',status())
            # A download the person paused is not about to try anything.
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
            with patch('comic_archive.account_ui.batches._load',return_value={'network_retry_after':at(79.5)}) as load:
                self.assertNotIn('retry',status())
            load.assert_not_called()

    def test_activity_rejects_future_malformed_and_finished_progress(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        path=self.workspace.library/'.archive-state/progress.json'
        started=account_ui.batches._instant(json.loads((self.queue/'account-export.json').read_text())['started_at']).timestamp()
        for content in ('{bad','{}','{"status":"finished"}'):
            path.write_text(content);os.utime(path,(started+10,started+10))
            with patch('comic_archive.account_ui.time.time',return_value=started+20):
                self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))
        private_json(path,{'status':'running'})
        os.utime(path,(started+30,started+30))
        with patch('comic_archive.account_ui.time.time',return_value=started+20):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))

    def test_go_resumes_stopped_completion_without_resetting_window_budgets_or_usage(self):
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('42','Fixture',[{'id':'81','title':'One','order':1,'access_state':'tapas_free'}],'fixture')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        completion.continue_discovered(self.workspace.session,self.workspace.library)
        root=batches._root(self.workspace.session,self.workspace.library)
        for _ in range(2):completion.worker_tick(self.workspace.session,self.workspace.library)
        original=batches.status(self.workspace.session,self.workspace.library)
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        resumed=batches.status(self.workspace.session,self.workspace.library)
        # The batch that is still out was asked for at the window's pace; later windows follow the chosen speed.
        self.assertEqual(resumed['budgets'],original['budgets'])
        self.assertEqual(resumed['budgets']['speed'],'normal')
        self.assertEqual(self.pace(completion.status(self.workspace.session,self.workspace.library)['window_budgets'])[0],'fast')
        self.assertEqual(resumed['downloaded_bytes'],original['downloaded_bytes'])
        self.assertEqual(completion.status(self.workspace.session,self.workspace.library)['status'],'running')

    def running_batch(self):
        """A download batch the downloader has started: published request, busy worker, recorded run."""
        session,library=self.workspace.session,self.workspace.library
        with Inventory(library) as inventory:
            inventory.observe('42','Fixture',[{'id':'81','title':'One','order':1,'access_state':'tapas_free'}],'fixture')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        completion.continue_discovered(session,library)
        for _ in range(5):
            completion.worker_tick(session,library)
            task=batches.status(session,library).get('pending_task_id')
            if task and (self.queue/'requests'/(task+'.json')).is_file():break
        else:self.fail('no batch was published')
        private_json(self.queue/'status.json',{'state':'busy','task_id':task})
        with Inventory(library) as inventory:run=inventory.start_run()
        job_progress(library,run,phase='archive')
        return task,run

    def end_batch(self,task,value,request=True):
        private_json(self.queue/'responses'/(task+'.json'),{'task_id':task,**value})
        if request:(self.queue/'requests'/(task+'.json')).unlink()
        private_json(self.queue/'status.json',{'state':'ready'})

    def test_pause_stops_the_batch_in_progress_instead_of_letting_it_run_out(self):
        _,run=self.running_batch()
        with patch.object(account_ui.time,'sleep') as nap:
            self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        nap.assert_not_called()
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())
        self.assertTrue(batches.status(self.workspace.session,self.workspace.library)['stop_requested'])

    def test_pause_asks_again_while_a_batch_that_just_started_cannot_be_stopped_yet(self):
        _,run=self.running_batch()
        (self.workspace.library/'.archive-state/progress.json').unlink()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0),patch.object(account_ui.time,'sleep') as nap:
            self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        nap.assert_not_called()
        self.assertFalse(RunCancellation(self.workspace.library,run).requested())
        naps=[]
        def nap(seconds):
            naps.append(seconds);job_progress(self.workspace.library,run,phase='archive')
        with patch.object(account_ui.time,'sleep',nap):
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(naps,[0.25])
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())

    def test_go_after_a_batch_stopped_by_pause_continues_without_a_hold(self):
        session,library=self.workspace.session,self.workspace.library
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled','archived_episode_ids':[],
            'downloaded_pages':3,'downloaded_bytes':30,'pending_episode_count':1,
            'inventory':{'scan_remaining_series_count':0}}})
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        window=batches.status(session,library)
        self.assertEqual((window['status'],window['code'],window['pending_task_id'],window['downloaded_pages'],
                          window['stop_requested']),('running',None,None,3,False))
        for _ in range(3):
            completion.worker_tick(session,library);account_export.worker_tick(session,library)
        self.assertEqual(completion.status(session,library)['status'],'running')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertNotIn(batches.status(session,library)['pending_task_id'],(None,task))

    def test_go_after_pause_carries_a_changed_speed_into_the_saved_plan_and_its_window(self):
        session,library=self.workspace.session,self.workspace.library
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled','archived_episode_ids':[],
            'downloaded_pages':3,'downloaded_bytes':30,'pending_episode_count':1,
            'inventory':{'scan_remaining_series_count':0}}})
        before=batches.status(session,library)['budgets']
        self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'medium')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        medium=('normal',4,0.25,batches.DEFAULTS['batch_bytes'])
        window=batches.status(session,library)
        self.assertEqual(self.pace(window['budgets']),medium)
        self.assertEqual(self.pace(completion.status(session,library)['window_budgets']),medium)
        # Only the pace changed: the window keeps its limits and what it has used.
        self.assertEqual(window['budgets'],before|account_ui.SPEEDS['medium'])
        self.assertEqual(window['downloaded_pages'],3)
        for _ in range(3):
            completion.worker_tick(session,library);account_export.worker_tick(session,library)
        request=json.loads((self.queue/'requests'/(batches.status(session,library)['pending_task_id']+'.json')).read_text())
        self.assertEqual((request['args']['download_workers'],request['args']['request_interval']),(4,0.25))
        self.assertNotIn('speed',request['args'])

    def test_go_continues_a_paused_download_of_another_site_past_that_sites_own_lists(self):
        session,library=self.workspace.session,self.workspace.library
        (session/'site').write_text('tappytoon\n')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        # Tappytoon's three lists were read, then a chapter stopped the download.
        root=batches._root(session,library)
        value=json.loads((root/'account-export.json').read_text())
        value.update(status='held',code='task_failed',phase='series',category_index=3,
                     list_checks=[{'category':name,'complete':True,'boundary_observed':True,'observed_series_count':1}
                                  for name in ('unlocked','subscribed','recent')])
        private_json(root/'account-export.json',value)
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'held')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(account_export.status(session,library)['list_checks'],value['list_checks'])

    def test_go_still_meets_a_hold_that_arrived_after_pause(self):
        session,library=self.workspace.session,self.workspace.library
        task,_=self.running_batch()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0):self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.end_batch(task,{'status':'error','error':'Sign in again.','error_code':'login_required'})
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(batches.status(session,library)['pending_task_id'],task)
        completion.worker_tick(session,library)
        window=batches.status(session,library)
        self.assertEqual((window['status'],window['code']),('paused','login_required'))

    def test_closing_interrupts_whatever_the_downloader_is_on_and_leaves_the_saved_download_running(self):
        session,library=self.workspace.session,self.workspace.library
        account_ui.interrupt(self.workspace)  # Nothing is running and nothing is configured.
        _,run=self.running_batch()
        before=[(self.queue/name).read_bytes() for name in ('account-export.json','completion.json','backlog.json')]
        with patch.object(account_ui.time,'sleep') as nap:account_ui.interrupt(self.workspace)
        nap.assert_not_called()
        self.assertTrue(RunCancellation(library,run).requested())
        self.assertEqual([(self.queue/name).read_bytes() for name in ('account-export.json','completion.json','backlog.json')],before)
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')

    CANCELLED={'archived_episode_ids':[],'downloaded_pages':3,'downloaded_bytes':30,'pending_episode_count':1,
               'inventory':{'scan_remaining_series_count':0}}

    def ticks(self):
        for _ in range(3):
            completion.worker_tick(self.workspace.session,self.workspace.library)
            account_export.worker_tick(self.workspace.session,self.workspace.library)

    def waiting_for_go(self):
        """What the page shows, and whether anything is queued for a downloader to run."""
        self.ticks()
        return (self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],
                [path.name for path in (self.queue/'requests').iterdir()])

    def test_a_start_of_the_app_sets_a_download_that_was_running_to_wait_for_go_and_go_continues_it(self):
        session,library=self.workspace.session,self.workspace.library
        self.assertFalse(account_ui.reopen(self.workspace))  # A library that has never downloaded.
        task,run=self.running_batch()
        account_ui.interrupt(self.workspace)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.assertTrue(account_ui.reopen(self.workspace))
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.assertEqual(completion.status(session,library)['status'],'stopped')
        self.assertFalse(account_ui.reopen(self.workspace))  # It waits already.
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.ticks()
        window=batches.status(session,library)
        self.assertEqual((window['status'],window['code'],window['downloaded_pages']),('running',None,3))
        self.assertNotIn(window['pending_task_id'],(None,task))
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')

    def test_a_start_of_the_app_sets_an_interrupted_check_of_the_lists_to_wait_for_go_which_queues_it_again(self):
        session,library=self.workspace.session,self.workspace.library
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        for _ in range(2):account_export.worker_tick(session,library)  # Reserved, then published.
        task=account_export.status(session,library)['pending_task_id']
        self.end_batch(task,{'status':'ok','result':{'status':'cancelled'}})
        self.assertTrue(account_ui.reopen(self.workspace))
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        state=account_export.status(session,library)
        self.assertEqual((state['status'],state['last_failed_task_id']),('running',task))
        again=state['pending_task_id']
        self.assertNotIn(again,(None,task))
        account_export.worker_tick(session,library)
        self.assertEqual(json.loads((self.queue/'requests'/(again+'.json')).read_text())['operation'],'sync')

    def test_a_start_of_the_app_ends_unrun_the_task_a_power_cut_left_queued_so_no_downloader_works_through_it(self):
        session,library=self.workspace.session,self.workspace.library
        task,_=self.running_batch()  # No receipt, and the downloader's last word is that it was busy with it.
        request=self.queue/'requests'/(task+'.json');before=request.read_bytes()
        with patch.object(account_ui.time,'sleep') as nap:self.assertTrue(account_ui.reopen(self.workspace))
        nap.assert_not_called()
        receipt=self.queue/'responses'/(task+'.json');value=json.loads(receipt.read_bytes())
        self.assertEqual((value['task_id'],value['status'],value['result']['status'],value['result']['cancelled_before_start'],
                          value['result']['downloaded_pages'],value['result']['pending_episode_count']),(task,'ok','cancelled',True,0,1))
        self.assertEqual((receipt.stat().st_mode&0o777,request.read_bytes()),(PRIVATE_FILE,before))
        # The downloader that starts next removes a request that has its receipt, without running it.
        request.unlink();private_json(self.queue/'status.json',{'state':'ready'})
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.ticks()
        window=batches.status(session,library)
        self.assertEqual((window['status'],window['code']),('running',None))
        self.assertNotIn(window['pending_task_id'],(None,task))

    def test_a_start_of_the_app_that_finds_a_downloader_still_running_stops_what_it_is_on_like_pause(self):
        task,run=self.running_batch()
        self.assertTrue(account_ui.reopen(self.workspace,idle=False))
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())
        self.assertFalse((self.queue/'responses'/(task+'.json')).exists())  # Its receipt is the downloader's to write.
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'stopped')

    def test_a_start_of_the_app_leaves_a_paused_download_as_it_is_and_still_ends_its_unrun_task(self):
        session,library=self.workspace.session,self.workspace.library
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        saved=[(self.queue/name).read_bytes() for name in ('account-export.json','completion.json','backlog.json')]
        self.assertFalse(account_ui.reopen(self.workspace,idle=False))
        self.assertFalse((self.queue/'responses'/(task+'.json')).exists())
        self.assertFalse(account_ui.reopen(self.workspace))  # Paused by its owner; the downloader died with the task.
        self.assertEqual(json.loads((self.queue/'responses'/(task+'.json')).read_bytes())['result']['status'],'cancelled')
        self.assertEqual([(self.queue/name).read_bytes() for name in ('account-export.json','completion.json','backlog.json')],saved)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.assertFalse(account_ui.reopen(self.workspace))  # A receipt that is there is never written over.
        self.assertEqual(json.loads((self.queue/'responses'/(task+'.json')).read_bytes())['result']['downloaded_pages'],3)

    def test_go_after_pause_resumes_a_plan_whose_first_batch_never_started(self):
        session,library=self.workspace.session,self.workspace.library
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        completion.continue_discovered(session,library)
        self.assertFalse(batches.status(session,library)['configured'])
        self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(completion.status(session,library)['status'],'stopped')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(completion.status(session,library)['status'],'running')
        self.assertFalse(batches.status(session,library)['configured'])

    def test_closing_asks_again_while_a_batch_that_just_started_cannot_be_stopped_yet(self):
        _,run=self.running_batch()
        (self.workspace.library/'.archive-state/progress.json').unlink()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0),patch.object(account_ui.time,'sleep') as nap:
            account_ui.interrupt(self.workspace)
        nap.assert_not_called()
        self.assertFalse(RunCancellation(self.workspace.library,run).requested())
        naps=[]
        def nap(seconds):
            naps.append(seconds);job_progress(self.workspace.library,run,phase='archive')
        with patch.object(account_ui.time,'sleep',nap):account_ui.interrupt(self.workspace)
        self.assertEqual(naps,[0.25])
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())

    def test_go_after_a_start_of_the_app_holds_a_download_whose_interrupted_batch_left_a_damaged_receipt(self):
        session,library=self.workspace.session,self.workspace.library
        task,run=self.running_batch()
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED,'downloaded_pages':10**9}})
        self.assertTrue(account_ui.reopen(self.workspace))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        completion.worker_tick(session,library)  # Go asks again; the receipt is still not one to count from.
        window=batches.status(session,library)
        self.assertEqual((window['status'],window['code'],window['pending_task_id'],window['downloaded_pages']),
                         ('paused','invalid_result',task,0))

