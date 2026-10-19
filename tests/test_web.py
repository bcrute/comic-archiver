import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comic_archive.errors import LibraryBusy
from comic_archive.local_web import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError
from comic_archive.web import HostedApplication


class WebTests(unittest.TestCase):
    def test_unknown_or_foreign_export_ids_never_launch_an_opener(self):
        route=f'/api/v1/workspaces/{self.workspace.id}/exports/'
        with patch('comic_archive.local_web.subprocess.Popen') as launch:
            self.assertEqual(self.call(route+'a'*64,method='POST',body={'action':'open'},headers=self.mutation_headers())[0],'400 Bad Request')
            self.assertEqual(self.call(route+'a'*64,owner='bob',method='POST',body={'action':'open'},headers=self.mutation_headers('bob'))[0],'404 Not Found')
            for key in ('a'*63,'not-a-digest'):
                status,_,body=self.call(route+key,method='POST',body={'action':'open'},headers=self.mutation_headers())
                self.assertEqual((status,json.loads(body)),('405 Method Not Allowed',{'code':'method_not_allowed'}))
            self.assertEqual(self.call(route+'a'*64)[0],'405 Method Not Allowed')
            launch.assert_not_called()

    def test_cloud_and_network_routes_are_owner_scoped_csrf_protected_and_refuse_credentials_and_paths(self):
        route='/api/v1/workspaces/'+self.workspace.id
        for suffix in ('/cloud','/cloud/upload','/cloud/signin','/network'):
            self.assertEqual(self.call(route+suffix,owner='bob')[0],'404 Not Found')
            self.assertEqual(self.call(route+suffix,method='POST',body={})[0],'403 Forbidden')
        self.assertTrue(json.loads(self.call(route+'/cloud')[2])['supported_on_this_interface'])
        headers=self.mutation_headers()
        for provider in ('google','dropbox','proton','b2'):
            self.assertEqual(self.call(route+'/cloud/signin',method='POST',body={'provider':provider},headers=headers)[0],'400 Bad Request')
        for body in ({'provider':'b2','enabled':True,'password':'SECRET'},{'config':'/private/config'},{'provider':'b2'}):
            self.assertEqual(self.call(route+'/cloud',method='POST',body=body,headers=headers)[0],'400 Bad Request')
        for body in ({},{'storage':'both'},{'storage':'shared','path':'/outside'}):
            self.assertEqual(self.call(route+'/cloud/storage',method='POST',body=body,headers=headers)[0],'400 Bad Request')
        for body in ({'wifi_only':'false'},{'wifi_only':False,'path':'/outside'}):
            self.assertEqual(self.call(route+'/network',method='POST',body=body,headers=headers)[0],'400 Bad Request')
        # No worker queue yet: the upload is refused without a request file appearing anywhere.
        self.assertEqual(json.loads(self.call(route+'/cloud/upload',method='POST',body={},headers=headers)[2]),{'code':'request_unavailable'})
        self.queue()
        self.assertEqual(self.call(route+'/cloud/upload',method='POST',body={'limit':999},headers=headers)[0],'400 Bad Request')
        self.assertEqual(list((self.workspace.session/'worker/requests').iterdir()),[])
        status,_,body=self.call(route+'/cloud/upload',method='POST',body={},headers=headers)
        self.assertEqual(status,'200 OK')
        task=json.loads(body)['task_id']
        request=json.loads((self.workspace.session/'worker/requests'/(task+'.json')).read_text())
        self.assertEqual(request,{'operation':'cloud_upload','args':{}})
        self.assertEqual(json.loads(self.call(route+'/tasks/'+task)[2]),{'task_id':task,'status':'queued'})

    def test_configured_origin_is_normalized_to_the_origin_a_browser_sends(self):
        route='/api/v1/workspaces/'+self.workspace.id+'/network'
        for configured,expected in (('https://ARCHIVE.EXAMPLE:443/','https://archive.example'),
                ('http://LOCALHOST:80/','http://localhost'),('http://[::1]:80/','http://[::1]'),
                ('https://archive.example:8443','https://archive.example:8443')):
            self.app=HostedApplication(self.registry,lambda env:LOCAL_OWNER,configured)
            self.assertEqual(self.app.public_origin,expected)
            headers=self.mutation_headers()|{'HTTP_ORIGIN':expected}
            status,_,body=self.call(route,method='POST',body={'unknown':True},headers=headers)
            self.assertEqual(status,'400 Bad Request')
            self.assertEqual(json.loads(body),{'code':'unknown_options'})

    def test_dashboard_assets_require_website_authentication_and_have_self_only_policy(self):
        for path,media in (('/','text/html'),('/library','text/html'),('/assets/library.js','text/javascript'),('/assets/archive.js','text/javascript'),
                           ('/assets/theme.js','text/javascript'),('/assets/archive.css','text/css')):
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
        self.assertNotIn(self.workspace.id.encode(),self.call('/')[2])
        self.assertEqual(list(self.workspace.session.glob('worker/requests/*')),[])

    def test_saved_file_pages_are_owner_scoped_bounded_read_only_and_head_safe(self):
        route='/api/v1/workspaces/'+self.workspace.id+'/saved-files'
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
        self.root=Path(work.name)
        self.registry=LocalWorkspaceRegistry(self.root)
        self.workspace=self.registry.resolve(LOCAL_OWNER,self.registry.workspace_id)
        self.app=HostedApplication(self.registry,lambda env:env.get('website.authenticated_subject'),'https://archive.example')

    def call(self,path,owner=LOCAL_OWNER,method='GET',body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else b''
        env={'REQUEST_METHOD':method,'PATH_INFO':path,'QUERY_STRING':'','CONTENT_TYPE':'application/json',
             'CONTENT_LENGTH':str(len(data)),'wsgi.input':io.BytesIO(data)}
        if owner is not None:env['website.authenticated_subject']=owner
        env.update(headers or {})
        response=[]
        iterable=self.app(env,lambda status,values:response.append((status,dict(values))))
        result=b''.join(iterable)
        return response[0][0],response[0][1],result

    def mutation_headers(self,owner=LOCAL_OWNER):
        value=json.loads(self.call('/api/v1/context',owner=owner)[2])
        return {'HTTP_ORIGIN':'https://archive.example','HTTP_X_CSRF_TOKEN':value['csrf_token']}

    def queue(self):
        (self.workspace.session/'worker/requests').mkdir(parents=True)

    def answer(self,body):
        """The worker's answer to a task, as the page sees it."""
        task='job-'+'a'*24
        responses=self.workspace.session/'worker/responses';responses.mkdir(parents=True,exist_ok=True)
        (responses/(task+'.json')).write_text(json.dumps({'task_id':task}|body))
        status,_,value=self.call('/api/v1/workspaces/'+self.workspace.id+'/tasks/'+task)
        self.assertEqual(status,'200 OK');self.assertNotIn(b'PRIVATE',value)
        return json.loads(value)

    def test_authentication_is_required_and_request_owner_headers_do_not_select_an_account(self):
        status,headers,body=self.call('/api/v1/context',owner=None,headers={'HTTP_X_ARCHIVE_OWNER':LOCAL_OWNER})
        self.assertEqual(status,'401 Unauthorized')
        self.assertEqual(json.loads(body),{'code':'website_login_required'})
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertEqual(self.call('/api/v1/context',owner='x'*257)[0],'401 Unauthorized')
        value=json.loads(self.call('/api/v1/context',headers={'HTTP_X_ARCHIVE_OWNER':'bob'})[2])
        self.assertEqual(value['workspace_ids'],[self.workspace.id])
        self.assertEqual({k:value[k] for k in ('interface_mode','desktop_only','simple_mode','theme','sites','site_settings',
                                               'site_switching','library_folder')},
                         {'interface_mode':'local','desktop_only':True,'simple_mode':True,'theme':None,'sites':None,
                          'site_settings':None,'site_switching':False,'library_folder':None})
        value=json.loads(self.call('/api/v1/context',owner='bob',headers={'HTTP_X_ARCHIVE_OWNER':LOCAL_OWNER})[2])
        self.assertEqual(value['workspace_ids'],[])
        self.assertNotIn(self.workspace.id,json.dumps(value))

    def test_owner_scope_is_checked_for_every_workspace_route(self):
        headers=self.mutation_headers('bob')
        for suffix,method,body in (('','GET',None),('/saved-files','GET',None),('/account','GET',None),('/account','POST',{}),
                ('/account/pause','POST',{}),('/account/speed','POST',{'speed':'fast'}),('/session/signin','GET',None),
                ('/session/signin','POST',{}),('/cloud','GET',None),('/cloud/upload','POST',{}),('/cloud/test','POST',{'provider':'b2'}),
                ('/cloud/cancel-signin','POST',{}),('/network','GET',None),('/tasks/job-'+'0'*24,'GET',None),
                ('/exports/'+'0'*64,'POST',{'action':'open'})):
            status,_,value=self.call('/api/v1/workspaces/'+self.workspace.id+suffix,owner='bob',method=method,body=body,headers=headers)
            self.assertEqual((status,json.loads(value)),('404 Not Found',{'code':'not_found'}),suffix)

    def test_removed_and_desktop_only_routes_are_not_found_without_a_desktop_window(self):
        headers=self.mutation_headers()
        for path,body in (('/api/v1/location',{'action':'open'}),('/api/v1/site',{'site':'tapas'}),
                ('/api/v1/site-speed',{'site':'tapas','speed':'fast'}),('/api/v1/theme',{'theme':'dark'}),('/api/v1/workspaces',{})):
            status,_,value=self.call(path,method='POST',body=body,headers=headers)
            self.assertEqual((status,json.loads(value)),('404 Not Found',{'code':'not_found'}),path)
        route='/api/v1/workspaces/'+self.workspace.id
        for suffix in ('/series','/audit','/storage','/daily','/jobs','/stop','/priority'):
            self.assertEqual(self.call(route+suffix)[0],'404 Not Found',suffix)
        self.assertEqual(self.call('/api/v1/workspaces')[0],'404 Not Found')

    def test_session_and_cloud_task_answers_keep_only_fixed_error_codes(self):
        from comic_archive.cloud_errors import MESSAGES
        for code in ('login_required',*MESSAGES,'PRIVATE',['PRIVATE'],None):
            value=self.answer({'status':'error','error_code':code,'error':'SESSION_PRIVATE PRIVATE_PATH','cookie':'PRIVATE'})
            self.assertEqual(value['code'],code if isinstance(code,str) and code!='PRIVATE' else 'task_failed')
            self.assertNotIn('result',value)

    def test_source_refusal_projection_keeps_only_gateway_status_and_a_sane_retry_delay(self):
        from comic_archive.retry_after import MAX_RETRY_AFTER
        for status,delay,kept_status,kept_delay in ((429,30,429,30),(403,0,403,0),(429,MAX_RETRY_AFTER,429,MAX_RETRY_AFTER),
                (429,MAX_RETRY_AFTER+1,429,None),(429,-1,429,None),(429,True,429,None),(429,'30',429,None),(500,30,None,None)):
            value=self.answer({'status':'error','error_code':'source_refused','error':'PRIVATE',
                'result':{'source_http_status':status,'retry_after_seconds':delay,'cookie':'PRIVATE'}})
            self.assertEqual(value['code'],'source_refused')
            self.assertEqual(value.get('source_http_status'),kept_status,(status,delay))
            self.assertEqual(value.get('retry_after_seconds'),kept_delay,(status,delay))
        self.assertEqual(self.answer({'status':'error','error_code':'source_refused','result':'PRIVATE'})['code'],'source_refused')

    def test_task_projection_omits_paths_credentials_and_diagnostics(self):
        value=self.answer({'status':'ok','result':{'status':'limited','downloaded_pages':2,'downloaded_bytes':100,
            'downloaded_chapters':-1,'folder':'PRIVATE_PATH','cookie':'PRIVATE_COOKIE','diagnostic':{'token':'PRIVATE'},
            'archived_episode_ids':['81'],'enriched_series_ids':['42'],'storage_preview':{'path':'PRIVATE'}}})
        self.assertEqual(value,{'task_id':'job-'+'a'*24,'status':'ok','result':{'status':'limited','downloaded_pages':2,
            'downloaded_bytes':100,'archived_episode_ids':['81']}})
        self.assertEqual(self.answer({'status':'ok','result':'PRIVATE'}),{'task_id':'job-'+'a'*24,'status':'ok'})
        self.assertEqual(self.answer({'status':'ok','result':{'status':'PRIVATE'}})['result'],{})

    def test_cloud_test_result_projection_exposes_only_verified_counts_and_booleans(self):
        result=self.answer({'status':'ok','result':{'verified_episodes':2,'uploaded_episodes':3,'freed_bytes':10,
            'test_files_deleted':True,'local_archives_retained':True,'config':'PRIVATE','test_id':'PRIVATE','prefix':'PRIVATE'}})['result']
        self.assertEqual(result,{'verified_episodes':2,'uploaded_episodes':3,'freed_bytes':10,'test_files_deleted':True,'local_archives_retained':True})
        result=self.answer({'status':'ok','result':{'verified_episodes':True,'test_files_deleted':1,'local_archives_retained':'PRIVATE'}})['result']
        self.assertEqual(result,{})

    def test_network_pause_projection_keeps_only_fixed_reason_and_gateway_status(self):
        for reason,http_status in (('network_unavailable',503),('PRIVATE',503),('network_unavailable','PRIVATE'),
                                   ('network_unavailable',403)):
            result=self.answer({'status':'ok','result':{'status':'paused','pause_reason':reason,
                'source_http_status':http_status,'diagnostic':'PRIVATE'}})['result']
            self.assertEqual(result.get('pause_reason'),reason if reason=='network_unavailable' else None)
            self.assertEqual(result.get('source_http_status'),503 if reason=='network_unavailable' and http_status==503 else None)
        result=self.answer({'status':'ok','result':{'status':'finished','pause_reason':'network_unavailable','source_http_status':503}})['result']
        self.assertEqual(result,{'status':'finished'})

    def test_metadata_retry_projection_publishes_only_valid_counts(self):
        for count in (1,0,-1,True,'PRIVATE'):
            result=self.answer({'status':'ok','result':{'status':'finished','metadata_unavailable_series_count':count,
                'metadata_unavailable_series':[{'series_id':'42','code':'PRIVATE_ERROR'}],
                'folder':'PRIVATE_PATH','diagnostic':'PRIVATE_COOKIE'}})['result']
            self.assertEqual(result.get('metadata_unavailable_series_count'),count if type(count) is int and count>=0 else None)
            self.assertNotIn('metadata_unavailable_series',result)

    def test_single_series_result_projection_keeps_only_valid_ids_flags_and_cancellation(self):
        value=self.answer({'status':'ok','result':{'status':'finished','source_series_id':'42','cover_archived':True,
            'cancelled_before_start':True,'archived_episode_ids':['81','82'],'folder':'PRIVATE'}})
        self.assertEqual(value['result'],{'status':'finished','source_series_id':'42',
            'cancelled_before_start':True,'archived_episode_ids':['81','82']})
        for series_id in ('0123','abc','1'*21,42):
            value=self.answer({'status':'ok','result':{'status':'finished','source_series_id':series_id,
                'cancelled_before_start':1,'archived_episode_ids':['81','PRIVATE']}})
            self.assertEqual(value['result'],{'status':'finished'},series_id)

    def test_scan_duration_projection_accepts_only_finite_nonnegative_numbers(self):
        for seconds in (12.345,True,-1,float('nan'),'PRIVATE',[1]):
            value=self.answer({'status':'ok','result':{'status':'finished','scan_elapsed_seconds':seconds,'elapsed_seconds':seconds,'cookie':'PRIVATE'}})['result']
            if seconds==12.345:
                self.assertEqual(value['scan_elapsed_seconds'],12.345)
                self.assertEqual(value['elapsed_seconds'],12.345)
            else:
                self.assertNotIn('scan_elapsed_seconds',value)
                self.assertNotIn('elapsed_seconds',value)

    def test_scan_outcome_counts_omit_private_fields_and_invalid_values(self):
        refresh={'series_checks_completed':2,'successful_series_checks':1,'held_series_checks':1,
                 'series_check_target_count':4,'remaining_series_count':True,'response':'PRIVATE'}
        value=self.answer({'status':'ok','result':{'status':'limited','refresh':refresh}})['result']['refresh']
        self.assertEqual(value,{key:refresh[key] for key in ('series_checks_completed',
            'successful_series_checks','held_series_checks','series_check_target_count')})

    def test_unsafe_or_missing_task_answers_are_refused_without_detail(self):
        route='/api/v1/workspaces/'+self.workspace.id+'/tasks/job-'+'b'*24
        self.assertEqual(json.loads(self.call(route)[2]),{'code':'request_unavailable'})
        self.assertEqual(self.call('/api/v1/workspaces/'+self.workspace.id+'/tasks/job-'+'g'*24)[0],'405 Method Not Allowed')
        self.assertEqual(self.call(route,method='POST',body={'action':'stop_queued'},headers=self.mutation_headers())[0],'405 Method Not Allowed')

    def test_csrf_origin_and_token_are_both_required_and_bound_to_owner(self):
        self.queue()
        path='/api/v1/workspaces/'+self.workspace.id+'/cloud/upload'
        valid=self.mutation_headers()
        for headers in ({},{'HTTP_HOST':'archive.example'},
                valid|{'HTTP_ORIGIN':'https://elsewhere.example'},
                valid|{'HTTP_X_CSRF_TOKEN':self.mutation_headers('bob')['HTTP_X_CSRF_TOKEN']},
                valid|{'HTTP_X_CSRF_TOKEN':'PRIVATE'}):
            status,_,_=self.call(path,method='POST',body={},headers=headers)
            self.assertEqual(status,'403 Forbidden')
        self.assertEqual(list((self.workspace.session/'worker/requests').iterdir()),[])
        with patch('comic_archive.web.time.time',return_value=7200):
            expired=self.mutation_headers()
        with patch('comic_archive.web.time.time',return_value=10800):
            self.assertEqual(self.call(path,method='POST',body={},headers=expired)[0],'200 OK')
        with patch('comic_archive.web.time.time',return_value=14400):
            self.assertEqual(self.call(path,method='POST',body={},headers=expired)[0],'403 Forbidden')

    def test_private_errors_and_response_fields_are_not_exposed(self):
        def fail(env):raise RuntimeError('Cookie=SESSION_PRIVATE')
        app=self.app
        self.app=HostedApplication(self.registry,fail,'https://archive.example')
        self.assertEqual(json.loads(self.call('/api/v1/context')[2]),{'code':'service_unavailable'})
        self.app=app
        value=self.answer({'status':'error','error':'SESSION_PRIVATE'})
        self.assertEqual(value['code'],'task_failed')

    def test_busy_library_reports_retry_and_head_has_no_body(self):
        path='/api/v1/workspaces/'+self.workspace.id
        with patch.object(self.registry,'status',side_effect=LibraryBusy('PRIVATE')):
            status,headers,body=self.call(path)
            self.assertEqual(status,'409 Conflict');self.assertEqual(headers['Retry-After'],'2')
            self.assertEqual(json.loads(body),{'code':'library_busy'})
            self.assertEqual(self.call(path,method='HEAD')[2],b'')

    def test_body_and_route_bounds_prevent_credentials_paths_and_unknown_actions(self):
        headers=self.mutation_headers()
        route='/api/v1/workspaces/'+self.workspace.id+'/network'
        for extra,expected in (({'CONTENT_LENGTH':'16385'},'413 Content Too Large'),({'CONTENT_LENGTH':'-1'},'413 Content Too Large'),
                ({'CONTENT_TYPE':'text/plain'},'415 Unsupported Media Type')):
            self.assertEqual(self.call(route,method='POST',body={},headers=headers|extra)[0],expected)
        self.assertEqual(self.call(route,method='POST',body={'wifi_only':True,'owner':'bob'},headers=headers)[0],'400 Bad Request')
        self.assertEqual(self.call('/api/v1/workspaces/../../PRIVATE')[0],'404 Not Found')
        self.assertEqual(self.call('/api/v1/context',headers={'QUERY_STRING':'owner=bob'})[0],'400 Bad Request')
        self.assertEqual(self.call(route,headers={'QUERY_STRING':'owner=bob'})[0],'400 Bad Request')
        self.assertEqual(self.call('/api/v1/context',method='PUT')[0],'405 Method Not Allowed')

    def test_https_or_loopback_origin_and_authentication_callback_are_required(self):
        for origin in ('http://archive.example','https://user:PRIVATE@archive.example',
                'https://archive.example/path','https://archive.example?PRIVATE','https://archive.example:99999','ftp://archive.example'):
            with self.assertRaises(ArchiveError):HostedApplication(self.registry,lambda _:None,origin)
        with self.assertRaises(ArchiveError):HostedApplication(self.registry,None,'https://archive.example')
        self.assertIsInstance(HostedApplication(self.registry,lambda _:None,'http://127.0.0.1:8765'),HostedApplication)

    def test_malformed_truncated_or_deeply_nested_bodies_are_refused_as_invalid_json_and_change_nothing(self):
        from comic_archive.network import policy
        before=policy(self.workspace.library)
        headers=self.mutation_headers()
        for raw,declared in ((b'{"wifi_only"',None),(b'[]',None),(b'"text"',None),(b'null',None),(b'\xff\xfe',None),
                             (b'['*10000,None),(b'{}',10)):
            length=len(raw) if declared is None else declared
            status,_,body=self.call('/api/v1/workspaces/'+self.workspace.id+'/network',method='POST',
                headers=headers|{'CONTENT_LENGTH':str(length),'wsgi.input':io.BytesIO(raw)})
            self.assertEqual((status,json.loads(body)),('400 Bad Request',{'code':'invalid_json'}),raw[:20])
        self.assertEqual(policy(self.workspace.library),before)


if __name__=='__main__':unittest.main()
