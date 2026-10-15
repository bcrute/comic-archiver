import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from support import replaces_open_files
from comic_archive.errors import LibraryBusy
from comic_archive.hosted import WorkspaceRegistry
from comic_archive.jobs import archive_discovered
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.web import HostedApplication
from test_jobs import AccountAdapter


class WebTests(unittest.TestCase):
    def test_hosted_exports_never_launch_an_app_on_the_library_server(self):
        route=f'/api/v1/workspaces/{self.alice.id}/exports/'+'a'*64
        with patch('comic_archive.local_web.subprocess.Popen') as launch:
            self.assertEqual(self.call(route,method='POST',body={'action':'open'},headers=self.mutation_headers())[0],'400 Bad Request')
            self.assertEqual(self.call(route,owner='bob',method='POST',body={'action':'open'},headers=self.mutation_headers('bob'))[0],'404 Not Found')
            launch.assert_not_called()

    def test_storage_and_cloud_routes_reject_cross_owner_credentials_and_hosted_uploads(self):
        route = '/api/v1/workspaces/'+self.alice.id
        for suffix in ('/storage','/storage/purge','/cloud','/cloud/upload'):
            self.assertEqual(self.call(route+suffix,owner='bob')[0],'404 Not Found')
            self.assertEqual(self.call(route+suffix,method='POST',body={})[0],'403 Forbidden')
        result = json.loads(self.call(route+'/storage')[2])
        self.assertEqual(result['storage_location'],'server')
        self.assertEqual(json.loads(self.call(route+'/cloud')[2]),{'supported_on_this_interface':False})
        self.assertEqual(json.loads(self.call(route+'/network')[2]),{'supported_on_this_interface':False})
        headers = self.mutation_headers()
        for provider in ('google','dropbox','proton'):
            self.assertEqual(self.call(route+'/cloud/signin',method='POST',body={'provider':provider},headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call(route+'/cloud/signin')[0],'400 Bad Request')
        self.assertEqual(self.call(route+'/network',method='POST',body={'wifi_only':False},headers=headers)[0],'400 Bad Request')
        for body in ({'provider':'google','enabled':True,'password':'SECRET'}, {'config':'/private/config'}):
            self.assertEqual(self.call(route+'/cloud',method='POST',body=body,headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call(route+'/cloud',method='POST',body={'provider':'google','enabled':True},headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call(route+'/cloud/upload',method='POST',body={},headers=headers)[0],'400 Bad Request')
        for body in ({'image_profile':'unknown'},{'image_profile':'original','path':'/outside'}):
            self.assertEqual(self.call(route+'/storage',method='POST',body=body,headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call(route+'/storage',method='POST',body={'image_profile':'original'},headers=headers)[0],'200 OK')
        self.queue(self.alice)
        self.assertEqual(self.call(route+'/storage/purge',method='POST',body={'path':'/outside'},headers=headers)[0],'400 Bad Request')
        self.assertEqual(list((self.alice.session/'worker/requests').iterdir()),[])
        result = self.call(route+'/storage/purge',method='POST',body={},headers=headers)
        self.assertEqual(result[0],'200 OK')
        task = json.loads(result[2])['task_id']
        request = json.loads((self.alice.session/'worker/requests'/(task+'.json')).read_text())
        self.assertEqual(request,{'operation':'purge_cache','args':{}})

    def test_configured_origin_is_normalized_to_the_origin_a_browser_sends(self):
        for configured,expected in (('https://ARCHIVE.EXAMPLE:443/','https://archive.example'),
                ('http://LOCALHOST:80/','http://localhost'),('http://[::1]:80/','http://[::1]'),
                ('https://archive.example:8443','https://archive.example:8443')):
            self.app=HostedApplication(self.registry,lambda env:'alice',configured)
            self.assertEqual(self.app.public_origin,expected)
            headers=self.mutation_headers()|{'HTTP_ORIGIN':expected}
            status,_,body=self.call('/api/v1/workspaces',method='POST',body={'unknown':True},headers=headers)
            self.assertEqual(status,'400 Bad Request')
            self.assertEqual(json.loads(body),{'code':'unknown_options'})

    def test_dashboard_assets_require_website_authentication_and_have_self_only_policy(self):
        for path,media in (('/','text/html'),('/library','text/html'),('/assets/library.js','text/javascript'),('/assets/archive.js','text/javascript'),('/assets/archive.css','text/css')):
            self.assertEqual(self.call(path,owner=None)[0],'401 Unauthorized')
            status,headers,body=self.call(path)
            self.assertEqual(status,'200 OK');self.assertTrue(headers['Content-Type'].startswith(media))
            self.assertEqual(int(headers['Content-Length']),len(body))
            self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
            self.assertNotIn("unsafe-inline",headers['Content-Security-Policy'])
            self.assertEqual(self.call(path,method='HEAD')[2],b'')
            self.assertEqual(self.call(path,method='POST',body={},headers=self.mutation_headers())[0],'405 Method Not Allowed')
        self.assertEqual(self.call('/assets/../../PRIVATE')[0],'404 Not Found')
        self.assertNotIn(self.alice.id.encode(),self.call('/')[2])
        self.assertEqual(list(self.alice.session.glob('worker/requests/*')),[])

    def test_saved_file_pages_are_owner_scoped_bounded_read_only_and_head_safe(self):
        route='/api/v1/workspaces/'+self.alice.id+'/saved-files'
        self.assertEqual(self.call(route,owner='bob',headers={'QUERY_STRING':'summary=1'})[0], '404 Not Found')
        status,_,body=self.call(route,headers={'QUERY_STRING':'summary=1'})
        self.assertEqual(status,'200 OK'); self.assertEqual(json.loads(body)['file_count'],0)
        self.assertEqual(self.call(route,headers={'QUERY_STRING':'limit=201'})[0], '400 Bad Request')
        self.assertEqual(self.call(route,headers={'QUERY_STRING':'owner=bob'})[0], '400 Bad Request')
        self.assertEqual(self.call(route,method='POST',body={},headers=self.mutation_headers())[0], '405 Method Not Allowed')
        self.assertEqual(self.call(route,method='HEAD',headers={'QUERY_STRING':'limit=50'})[2],b'')

    def setUp(self):
        work=tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.registry=WorkspaceRegistry(Path(work.name)/'hosted')
        self.alice=self.registry.create('alice')
        self.bob=self.registry.create('bob')
        self.app=HostedApplication(self.registry,lambda env:env.get('website.authenticated_subject'),'https://archive.example')

    def call(self,path,owner='alice',method='GET',body=None,headers=None,consume=True):
        data=json.dumps(body).encode() if body is not None else b''
        env={'REQUEST_METHOD':method,'PATH_INFO':path,'QUERY_STRING':'','CONTENT_TYPE':'application/json',
             'CONTENT_LENGTH':str(len(data)),'wsgi.input':io.BytesIO(data)}
        if owner is not None:env['website.authenticated_subject']=owner
        env.update(headers or {})
        response=[]
        iterable=self.app(env,lambda status,values:response.append((status,dict(values))))
        if not consume:return response[0],iterable
        try:result=b''.join(iterable)
        finally:
            if hasattr(iterable,'close'):iterable.close()
        return response[0][0],response[0][1],result

    def mutation_headers(self,owner='alice'):
        value=json.loads(self.call('/api/v1/context',owner=owner)[2])
        return {'HTTP_ORIGIN':'https://archive.example','HTTP_X_CSRF_TOKEN':value['csrf_token']}

    def queue(self,workspace):
        (workspace.session/'worker/requests').mkdir(parents=True)

    def test_priority_setting_requires_owner_csrf_fixed_choices_and_queues_no_archive_action(self):
        self.queue(self.alice)
        route=f'/api/v1/workspaces/{self.alice.id}/priority'
        self.assertEqual(self.call(route,method='POST',body={'priority':'owned_first'})[0],'403 Forbidden')
        self.assertEqual(self.call(route,owner='bob',method='POST',body={'priority':'owned_first'},headers=self.mutation_headers('bob'))[0],'404 Not Found')
        for body in ({'priority':'invalid'},{'priority':'owned_first','path':'PRIVATE'}):
            self.assertEqual(self.call(route,method='POST',body=body,headers=self.mutation_headers())[0],'400 Bad Request')
        status,_,body=self.call(route,method='POST',body={'priority':'owned_first'},headers=self.mutation_headers())
        self.assertEqual(status,'200 OK')
        request=json.loads((self.alice.session/'worker/requests'/(json.loads(body)['task_id']+'.json')).read_text())
        self.assertEqual(request,{'operation':'configure_priority','args':{'priority':'owned_first'}})
        self.assertEqual(self.registry.status('alice',self.alice.id)['queue_priority'],'expiring_first')

    def test_authentication_is_required_and_request_owner_headers_do_not_select_an_account(self):
        status,headers,body=self.call('/api/v1/workspaces',owner=None,headers={'HTTP_X_ARCHIVE_OWNER':'alice'})
        self.assertEqual(status,'401 Unauthorized')
        self.assertEqual(json.loads(body),{'code':'website_login_required'})
        value=json.loads(self.call('/api/v1/context',headers={'HTTP_X_ARCHIVE_OWNER':'bob'})[2])
        self.assertEqual(value['workspace_ids'],[self.alice.id])
        self.assertNotIn(self.bob.id,json.dumps(value))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_owner_scope_is_checked_for_status_jobs_schedules_tasks_and_exports(self):
        headers=self.mutation_headers('bob')
        for suffix,method,body in (('', 'GET',None),('/jobs','POST',{'operation':'sync'}),
                ('/session/check','POST',{}),
                ('/series','GET',None),('/audit','GET',None),('/jobs','POST',{'operation':'archive_watched_series','options':{'id':'42'}}),
                ('/stop','POST',{'run_id':'0'*24}),
                ('/daily','POST',{}),('/exports','GET',None),('/tasks/job-'+'0'*24,'GET',None),
                ('/exports/'+'0'*64,'GET',None)):
            status,_,value=self.call('/api/v1/workspaces/'+self.alice.id+suffix,owner='bob',method=method,body=body,headers=headers)
            self.assertEqual(status,'404 Not Found')
        self.assertEqual(json.loads(value),{'code':'not_found'})

    def test_run_stop_requires_owner_csrf_current_identity_and_no_extra_fields(self):
        with Inventory(self.alice.library) as inventory:run=inventory.start_run()
        progress=self.alice.library/'.archive-state/progress.json'
        progress.write_text(json.dumps({'run_id':run,'status':'running','cancellation_supported':True}))
        path='/api/v1/workspaces/'+self.alice.id+'/stop'
        headers=self.mutation_headers()
        self.assertEqual(self.call(path,method='GET')[0],'405 Method Not Allowed')
        self.assertEqual(self.call(path,method='POST',body={'run_id':run})[0],'403 Forbidden')
        for body in ({},{'run_id':'../PRIVATE'},{'run_id':'f'*24},{'run_id':run,'owner':'bob'}):
            self.assertEqual(self.call(path,method='POST',body=body,headers=headers)[0],'400 Bad Request')
        self.assertFalse((self.alice.library/'.archive-state/cancellations').exists())
        status,_,body=self.call(path,method='POST',body={'run_id':run},headers=headers)
        self.assertEqual(status,'200 OK')
        self.assertEqual(json.loads(body),{'run_id':run,'status':'cancellation_requested'})
        self.assertEqual(list((self.bob.library/'.archive-state').glob('cancellations/*')),[])

    def test_queued_stop_is_owner_scoped_csrf_protected_and_fixed(self):
        from comic_archive.archive import atomic_json
        queue=self.alice.session/'worker';(queue/'requests').mkdir(parents=True)
        (queue/'responses').mkdir()
        atomic_json(queue/'status.json',{'state':'ready','worker_version':35})
        task=self.registry.submit('alice',self.alice.id,'sync',{'limit':1})['task_id']
        path='/api/v1/workspaces/'+self.alice.id+'/tasks/'+task
        headers=self.mutation_headers()
        self.assertTrue(json.loads(self.call(path)[2])['cancellation_supported'])
        self.assertEqual(self.call(path,method='POST',body={'action':'stop_queued'})[0],'403 Forbidden')
        self.assertEqual(self.call(path,owner='bob',method='POST',body={'action':'stop_queued'},
                                   headers=self.mutation_headers('bob'))[0],'404 Not Found')
        for body in ({},{'action':'purchase'},{'action':'stop_queued','owner':'bob'},
                     {'action':'stop_queued','url':'https://tapas.io/'}):
            self.assertEqual(self.call(path,method='POST',body=body,headers=headers)[0],'400 Bad Request')
        self.assertFalse((queue/'cancellations').exists())
        status,_,body=self.call(path,method='POST',body={'action':'stop_queued'},headers=headers)
        self.assertEqual(status,'200 OK');self.assertEqual(json.loads(body)['status'],'cancellation_requested')
        self.assertTrue(json.loads(self.call(path)[2])['cancellation_requested'])
        self.assertEqual(list(self.bob.session.glob('worker/cancellations/*')),[])

    def test_audit_verifies_only_owned_files_and_does_not_export_private_account_state(self):
        with Inventory(self.alice.library) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'First','order':1,'access_state':'tapas_free'}],'subscribed')
        adapter=AccountAdapter()
        with patch('builtins.print'):archive_discovered(adapter,self.alice.library,limit=1)
        with Inventory(self.bob.library) as inventory:inventory.observe('99','BOB_PRIVATE',[],'subscribed')
        path='/api/v1/workspaces/'+self.alice.id+'/audit'
        status,_,body=self.call(path)
        self.assertEqual(status,'200 OK')
        value=json.loads(body)
        self.assertEqual(value['verified_inventory_episode_count'],1)
        self.assertFalse(value['account_archive_complete'])
        self.assertNotIn(b'BOB_PRIVATE',body);self.assertNotIn(b'account_binding',body)
        self.assertNotIn(b'account_salt',body)
        self.assertEqual(self.call(path,owner=None)[0],'401 Unauthorized')
        self.assertEqual(self.call(path,method='POST',body={},headers=self.mutation_headers())[0],'405 Method Not Allowed')
        with patch('comic_archive.hosted.WorkspaceRegistry.audit',side_effect=LibraryBusy('PRIVATE')):
            self.assertEqual(json.loads(self.call(path)[2]),{'code':'library_busy'})

    def test_session_check_has_owner_csrf_empty_body_and_filtered_health_result(self):
        self.queue(self.alice);self.queue(self.bob)
        path='/api/v1/workspaces/'+self.alice.id+'/session/check'
        self.assertEqual(self.call(path,method='POST',body={})[0],'403 Forbidden')
        self.assertEqual(self.call(path,method='POST',body={'cookies':'PRIVATE'},headers=self.mutation_headers())[0],'400 Bad Request')
        status,_,body=self.call(path,method='POST',body={},headers=self.mutation_headers())
        self.assertEqual(status,'200 OK');task=json.loads(body)['task_id']
        request=json.loads((self.alice.session/'worker/requests'/f'{task}.json').read_text())
        self.assertEqual(request,{'operation':'check_session','args':{}})
        self.assertEqual(list((self.bob.session/'worker/requests').iterdir()),[])
        responses=self.alice.session/'worker/responses';responses.mkdir()
        result={'authenticated':True,'account_binding':'matches','reader_or_comic_pages_requested':False,'identity':'ACCOUNT_PRIVATE','cookie':'SESSION_PRIVATE'}
        file=responses/f'{task}.json';file.write_text(json.dumps({'task_id':task,'status':'ok','result':result}))
        value=json.loads(self.call('/api/v1/workspaces/'+self.alice.id+'/tasks/'+task)[2])
        self.assertEqual(value['result'],{'authenticated':True,'account_binding':'matches'})
        self.assertNotIn('PRIVATE',json.dumps(value))
        file.write_text(json.dumps({'task_id':task,'status':'error','error_code':'login_required','error':'SESSION_PRIVATE'}))
        value=json.loads(self.call('/api/v1/workspaces/'+self.alice.id+'/tasks/'+task)[2])
        self.assertEqual(value['code'],'login_required');self.assertNotIn('PRIVATE',json.dumps(value))


    def test_scan_duration_projection_accepts_only_finite_nonnegative_numbers(self):
        self.queue(self.alice);responses=self.alice.session/'worker/responses';responses.mkdir()
        task='job-'+'a'*24;path=responses/(task+'.json')
        for seconds in (12.345,True,-1,float('nan'),'PRIVATE',[1]):
            path.write_text(json.dumps({'task_id':task,'status':'ok','result':{'status':'finished','scan_elapsed_seconds':seconds,'elapsed_seconds':seconds,'cookie':'PRIVATE'}}))
            status,_,body=self.call('/api/v1/workspaces/'+self.alice.id+'/tasks/'+task)
            self.assertEqual(status,'200 OK');value=json.loads(body)['result']
            if seconds==12.345:
                self.assertEqual(value['scan_elapsed_seconds'],12.345)
                self.assertEqual(value['elapsed_seconds'],12.345)
            else:
                self.assertNotIn('scan_elapsed_seconds',value)
                self.assertNotIn('elapsed_seconds',value)
            self.assertNotIn(b'PRIVATE',body)

    def test_scan_outcome_counts_omit_private_fields_and_invalid_values(self):
        self.queue(self.alice);responses=self.alice.session/'worker/responses';responses.mkdir()
        task='job-'+'a'*24
        refresh={'series_checks_completed':2,'successful_series_checks':1,'held_series_checks':1,
                 'series_check_target_count':4,'remaining_series_count':True,'response':'PRIVATE'}
        (responses/(task+'.json')).write_text(json.dumps({'task_id':task,'status':'ok','result':{
            'status':'limited','refresh':refresh}}))
        status,_,body=self.call('/api/v1/workspaces/'+self.alice.id+'/tasks/'+task)
        self.assertEqual(status,'200 OK')
        value=json.loads(body)['result']['refresh']
        self.assertEqual(value,{key:refresh[key] for key in ('series_checks_completed',
            'successful_series_checks','held_series_checks','series_check_target_count')})
        self.assertNotIn(b'PRIVATE',body)

    def test_series_access_counts_are_owner_scoped_observations_without_private_account_fields(self):
        self.queue(self.alice);self.queue(self.bob)
        with Inventory(self.alice.library) as inventory:
            inventory.observe('42','Mixed',[
                {'id':'81','title':'Free','order':1,'access_state':'tapas_free'},
                {'id':'82','title':'Unlocked','order':2,'access_state':'tapas_unlocked'},
                {'id':'83','title':'Denied','order':3,'access_state':'unavailable_or_unverified'}],'fixture')
            inventory.bind_account('ACCOUNT_PRIVATE')
        with Inventory(self.bob.library) as inventory:
            inventory.observe('99','BOB_PRIVATE',[{'id':'101','title':'Unlocked','order':1,'access_state':'tapas_unlocked'}],'fixture')
        status,_,body=self.call('/api/v1/workspaces/'+self.alice.id+'/series')
        self.assertEqual(status,'200 OK');self.assertNotIn(b'PRIVATE',body)
        rows=json.loads(body)['series'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['observed_free_episode_count'],1)
        self.assertEqual(rows[0]['observed_unlocked_episode_count'],1)
        self.assertEqual(rows[0]['observed_episode_count'],3)
        self.assertNotIn('purchased',rows[0]);self.assertNotIn('account_id',rows[0])

    def test_selected_series_list_and_job_only_use_that_owners_observed_inventory(self):
        self.queue(self.alice);self.queue(self.bob)
        with Inventory(self.alice.library) as inventory:
            inventory.observe('42','Selected',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
            inventory.observe('43','Empty',[],'fixture')
        with Inventory(self.bob.library) as inventory:inventory.observe('99','BOB_PRIVATE',[],'fixture')
        path='/api/v1/workspaces/'+self.alice.id
        status,_,body=self.call(path+'/series')
        self.assertEqual(status,'200 OK');self.assertNotIn(b'BOB_PRIVATE',body)
        rows=json.loads(body)['series']
        self.assertEqual([row['source_series_id'] for row in rows],['43','42'])
        self.assertEqual(rows[1]['episodes_by_status'],{'queued':1})
        self.assertEqual(rows[1]['observed_episode_count'],1)
        self.assertEqual(rows[0]['observed_episode_count'],0)
        self.assertEqual(rows[1]['observed_free_episode_count'],1)
        self.assertEqual(rows[1]['observed_unlocked_episode_count'],0)
        headers=self.mutation_headers()
        for selected in ('99','https://tapas.io/series/42',None):
            status,_,_=self.call(path+'/jobs',method='POST',body={'operation':'archive_watched_series','options':{'id':selected}},headers=headers)
            self.assertEqual(status,'400 Bad Request')
        self.assertEqual(list((self.alice.session/'worker/requests').iterdir()),[])
        status,_,body=self.call(path+'/jobs',method='POST',body={'operation':'archive_watched_series','options':{'id':'42'}},headers=headers)
        self.assertEqual(status,'200 OK')
        task=json.loads(body)['task_id']
        request=json.loads((self.alice.session/'worker/requests'/f'{task}.json').read_text())
        self.assertEqual(request['operation'],'archive_watched_series')
        self.assertEqual(request['args']['id'],'42')
        self.assertEqual(request['args']['max_seconds'],900)
        self.assertEqual(request['args']['max_pages'],1000)
        self.assertEqual(list((self.bob.session/'worker/requests').iterdir()),[])


    def test_csrf_origin_and_token_are_both_required_and_bound_to_owner(self):
        self.queue(self.alice)
        path='/api/v1/workspaces/'+self.alice.id+'/jobs'
        valid=self.mutation_headers()
        for headers in ({},{'HTTP_HOST':'archive.example'},
                valid|{'HTTP_ORIGIN':'https://elsewhere.example'},
                valid|{'HTTP_X_CSRF_TOKEN':self.mutation_headers('bob')['HTTP_X_CSRF_TOKEN']}):
            status,_,_=self.call(path,method='POST',body={'operation':'sync'},headers=headers)
            self.assertEqual(status,'403 Forbidden')
        self.assertEqual(list((self.alice.session/'worker/requests').iterdir()),[])
        with patch('comic_archive.web.time.time',return_value=7200):
            expired=self.mutation_headers()
        with patch('comic_archive.web.time.time',return_value=14400):
            self.assertEqual(self.call(path,method='POST',body={'operation':'sync'},headers=expired)[0],'403 Forbidden')

    def test_valid_jobs_are_fixed_bounded_and_scoped_to_the_authenticated_workspace(self):
        self.queue(self.alice);self.queue(self.bob)
        path='/api/v1/workspaces/'+self.alice.id+'/jobs'
        status,_,body=self.call(path,method='POST',body={'operation':'sync','options':{'free_only':True}},headers=self.mutation_headers())
        self.assertEqual(status,'200 OK')
        task=json.loads(body)['task_id']
        request=json.loads((self.alice.session/'worker/requests'/f'{task}.json').read_text())
        self.assertEqual(request['args']['max_seconds'],900)
        self.assertEqual(request['args']['max_bytes'],256*1024**2)
        self.assertEqual(list((self.bob.session/'worker/requests').iterdir()),[])
        for body in ({'operation':'purchase'},{'operation':'sync','owner':'bob'},
                {'operation':'sync','options':{'output':'/PRIVATE'}},
                {'operation':'sync','options':{'max_pages':1001}},
                {'operation':'sync','options':{'cookies':'PRIVATE'}},
                {'operation':'sync','options':'PRIVATE'}):
            status,_,value=self.call(path,method='POST',body=body,headers=self.mutation_headers())
            self.assertEqual(status,'400 Bad Request');self.assertNotIn(b'PRIVATE',value)

    def test_private_errors_and_response_fields_are_not_exposed(self):
        def fail(env):raise RuntimeError('Cookie=SESSION_PRIVATE')
        app=self.app
        self.app=HostedApplication(self.registry,fail,'https://archive.example')
        self.assertEqual(json.loads(self.call('/api/v1/context')[2]),{'code':'service_unavailable'})
        self.app=app
        self.queue(self.alice)
        task='job-'+'a'*24
        responses=self.alice.session/'worker/responses';responses.mkdir()
        (responses/(task+'.json')).write_text(json.dumps({'task_id':task,'status':'error','error':'SESSION_PRIVATE'}))
        status,_,value=self.call('/api/v1/workspaces/'+self.alice.id+'/tasks/'+task)
        self.assertEqual(status,'200 OK');self.assertNotIn(b'PRIVATE',value)

    def test_daily_schedule_preserves_free_only_and_server_limits(self):
        self.queue(self.alice)
        path='/api/v1/workspaces/'+self.alice.id+'/daily'
        status,_,_=self.call(path,method='POST',body={'run_now':True},headers=self.mutation_headers())
        self.assertEqual(status,'200 OK')
        request=json.loads(next((self.alice.session/'worker/requests').iterdir()).read_text())
        self.assertTrue(request['args']['options']['free_only'])
        self.assertEqual(request['args']['options']['max_seconds'],900)
        self.assertEqual(self.call(path,method='POST',body={'options':{'free_only':False}},headers=self.mutation_headers())[0],'400 Bad Request')

    @replaces_open_files
    @patch('builtins.print')
    def test_verified_download_is_pinned_and_closed_after_stream_or_head(self,output):
        report=self.alice.library/'discovery.json'
        report.write_text(json.dumps({'schema_version':1,'source':'tapas','series':[{
            'source_series_id':'42','title':'S','checked_at':'2026-10-02T00:00:00Z',
            'accessible_episodes':[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}]}]}))
        archive_discovered(AccountAdapter(),self.alice.library,report)
        exports=self.registry.exports('alice',self.alice.id)
        export=next(v for v in exports if v['kind']=='cbz')
        path='/api/v1/workspaces/'+self.alice.id+'/exports/'+export['id']
        archive=next(self.alice.library.glob('*/*.cbz'));original=archive.read_bytes()
        response,iterable=self.call(path,consume=False)
        replacement=archive.with_suffix('.replacement');replacement.write_bytes(b'PRIVATE');replacement.replace(archive)
        self.assertEqual(b''.join(iterable),original)
        self.assertTrue(iterable.download.stream.closed)
        self.assertEqual(response[1]['Content-Length'],str(len(original)))
        self.assertTrue(response[1]['Content-Disposition'].startswith("attachment; filename*=UTF-8''"))
        archive.write_bytes(original)
        status,headers,body=self.call(path,method='HEAD')
        self.assertEqual(status,'200 OK');self.assertEqual(body,b'')
        self.assertEqual(headers['Content-Length'],str(len(original)))
        response,iterable=self.call(path,consume=False);iterable.close()
        self.assertTrue(iterable.download.stream.closed)

    def test_busy_downloads_report_retry_and_head_has_no_body(self):
        with patch.object(self.registry,'exports',side_effect=LibraryBusy('PRIVATE')):
            path='/api/v1/workspaces/'+self.alice.id+'/exports'
            status,headers,body=self.call(path)
            self.assertEqual(status,'409 Conflict');self.assertEqual(headers['Retry-After'],'2')
            self.assertEqual(json.loads(body),{'code':'library_busy'})
            self.assertEqual(self.call(path,method='HEAD')[2],b'')

    def test_body_and_route_bounds_prevent_credentials_paths_and_unknown_actions(self):
        headers=self.mutation_headers()
        for extra,expected in (({'CONTENT_LENGTH':'16385'},'413 Content Too Large'),
                ({'CONTENT_TYPE':'text/plain'},'415 Unsupported Media Type')):
            self.assertEqual(self.call('/api/v1/workspaces',method='POST',body={},headers=headers|extra)[0],expected)
        self.assertEqual(self.call('/api/v1/workspaces',method='POST',body={'owner':'bob'},headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call('/api/v1/workspaces/../../PRIVATE')[0],'404 Not Found')
        self.assertEqual(self.call('/api/v1/context',headers={'QUERY_STRING':'owner=bob'})[0],'400 Bad Request')
        self.assertEqual(self.call('/api/v1/context',method='PUT')[0],'405 Method Not Allowed')

    def test_server_workspace_quota_is_atomic_across_concurrent_requests(self):
        def create(_):
            try:return self.registry.create('alice',max_per_owner=2).id
            except ArchiveError:return None
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(create,range(3)))
        self.assertEqual(sum(v is not None for v in results),1)
        self.assertEqual(len(self.registry.ids('alice')),2)
        self.assertEqual(len(self.registry.ids('bob')),1)

    def test_https_or_loopback_origin_and_authentication_callback_are_required(self):
        for origin in ('http://archive.example','https://user:PRIVATE@archive.example',
                'https://archive.example/path','https://archive.example?PRIVATE'):
            with self.assertRaises(ArchiveError):HostedApplication(self.registry,lambda _:None,origin)
        with self.assertRaises(ArchiveError):HostedApplication(self.registry,None,'https://archive.example')
        self.assertIsInstance(HostedApplication(self.registry,lambda _:None,'http://127.0.0.1:8765'),HostedApplication)

    def test_workspace_quota_must_be_a_server_chosen_positive_integer(self):
        for quota in (0,101,-1,True,2.0,'3',None):
            with self.subTest(quota=quota),self.assertRaises(ArchiveError):
                HostedApplication(self.registry,lambda _:'alice','https://archive.example',max_workspaces=quota)
        for quota in (1,100):
            self.assertEqual(HostedApplication(self.registry,lambda _:'alice','https://archive.example',max_workspaces=quota).max_workspaces,quota)

    def test_download_stream_read_failure_is_a_fixed_error_and_still_releases_the_file(self):
        from comic_archive.web import FileBody
        class Stream:
            def read(self,size):raise OSError('/PRIVATE/library/episode.cbz: input/output error')
        class Download:
            stream=Stream();closed=False
            def close(self):self.closed=True
        download=Download()
        with self.assertRaises(ArchiveError) as caught:b''.join(FileBody(download))
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertTrue(download.closed)

    def test_malformed_truncated_or_deeply_nested_bodies_are_refused_as_invalid_json_and_change_nothing(self):
        headers=self.mutation_headers()
        for raw,declared in ((b'{"owner"',None),(b'[]',None),(b'"text"',None),(b'null',None),(b'\xff\xfe',None),
                             (b'['*10000,None),(b'{}',10)):
            length=len(raw) if declared is None else declared
            status,_,body=self.call('/api/v1/workspaces',method='POST',
                headers=headers|{'CONTENT_LENGTH':str(length),'wsgi.input':io.BytesIO(raw)})
            self.assertEqual((status,json.loads(body)),('400 Bad Request',{'code':'invalid_json'}),raw[:20])
        self.assertEqual(self.registry.ids('alice'),[self.alice.id])

    def test_workspace_list_creation_and_status_are_owner_scoped_and_stop_at_the_quota(self):
        self.assertEqual(json.loads(self.call('/api/v1/workspaces')[2]),{'workspace_ids':[self.alice.id]})
        self.assertEqual(json.loads(self.call('/api/v1/workspaces',owner='bob')[2]),{'workspace_ids':[self.bob.id]})
        headers=self.mutation_headers()
        created=[]
        for _ in range(2):  # The default quota is three and alice already has one.
            status,_,body=self.call('/api/v1/workspaces',method='POST',body={},headers=headers)
            self.assertEqual(status,'200 OK');created.append(json.loads(body)['workspace_id'])
        status,_,body=self.call('/api/v1/workspaces',method='POST',body={},headers=headers)
        self.assertEqual((status,json.loads(body)),('400 Bad Request',{'code':'request_unavailable'}))
        self.assertEqual(self.registry.ids('alice'),sorted([self.alice.id,*created]))
        self.assertEqual(self.registry.ids('bob'),[self.bob.id])
        status,_,body=self.call('/api/v1/workspaces/'+created[0])
        self.assertEqual(status,'200 OK');self.assertEqual(json.loads(body)['workspace_id'],created[0])
        self.assertNotIn(str(self.registry.root).encode(),body)
        self.assertEqual(self.call('/api/v1/workspaces/'+created[0],owner='bob')[0],'404 Not Found')

    def test_account_and_browser_signin_controls_exist_only_in_the_local_dashboard(self):
        route='/api/v1/workspaces/'+self.alice.id
        headers=self.mutation_headers()
        for suffix in ('/account','/session/signin'):
            for method,body in (('GET',None),('POST',{})):
                status,_,value=self.call(route+suffix,method=method,body=body,headers=headers)
                self.assertEqual((status,json.loads(value)),('400 Bad Request',{'code':'request_unavailable'}))

    def test_daily_schedule_status_is_readable_and_unknown_settings_are_refused_before_queueing(self):
        self.queue(self.alice)
        path='/api/v1/workspaces/'+self.alice.id+'/daily'
        status,_,body=self.call(path)
        self.assertEqual(status,'200 OK');self.assertFalse(json.loads(body)['configured'])
        self.assertEqual(self.call(path,method='HEAD')[2],b'')
        self.assertEqual(self.call(path,owner='bob')[0],'404 Not Found')
        for body in ({'output':'/outside'},{'free_only':False},{'enabled':True,'cookies':'PRIVATE'}):
            status,_,value=self.call(path,method='POST',body=body,headers=self.mutation_headers())
            self.assertEqual((status,json.loads(value)),('400 Bad Request',{'code':'unknown_options'}))
        self.assertEqual(list((self.alice.session/'worker/requests').iterdir()),[])

    def test_export_ids_must_be_full_digests(self):
        route='/api/v1/workspaces/'+self.alice.id+'/exports/'
        self.assertEqual(json.loads(self.call(route+'not-a-digest')[2]),{'code':'not_found'})


if __name__=='__main__':unittest.main()
