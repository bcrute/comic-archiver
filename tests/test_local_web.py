import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive.inventory import Inventory
from comic_archive.locking import LibraryLock
from comic_archive.local_web import (COOKIE_NAME, LOCAL_OWNER, NEEDS_LAUNCH, SESSION_SECONDS, LocalApplication,
                                     LocalWorkspaceRegistry, LocalServer, PrivateRequestHandler, create_server)
from comic_archive.models import ArchiveError
from comic_archive.sites import choices


class LocalWebTests(unittest.TestCase):

    def file_to_open(self):
        folder=self.workspace.library/'portable';folder.mkdir()
        archive=folder/'saved episode.cbz';archive.write_bytes(b'owned portable file')
        archive.with_suffix('.json').write_text('{}')
        return archive,next(key for key,(_,kind) in self.registry._exports(self.workspace).items() if kind=='cbz')

    def test_library_folder_is_named_only_by_the_desktop_app_and_the_page_can_only_leave_a_fixed_request(self):
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        self.assertIsNone(context['library_folder'])
        self.assertEqual(self.call('/api/v1/location',method='POST',body={'action':'open'},cookie=cookie,headers=headers)[0],'404 Not Found')
        class Folder:
            asked=[]
            def get(self): return '/home/user/Tapas Archive/Library'
            def ask(self,action): self.asked.append(action)
        folder=self.registry.location=Folder()
        self.assertEqual(json.loads(self.call('/api/v1/context',cookie=cookie)[2])['library_folder'],'/home/user/Tapas Archive/Library')
        self.assertEqual(self.call('/api/v1/location',method='POST',body={'action':'open'})[0],'401 Unauthorized')
        self.assertEqual(self.call('/api/v1/location',method='POST',body={'action':'open'},cookie=cookie)[0],'403 Forbidden')
        self.assertEqual(self.call('/api/v1/location',cookie=cookie)[0],'404 Not Found')
        # Never a path from the page: the window's own dialog chooses the folder.
        for body in ({},{'action':'delete'},{'action':['open']},{'action':'choose','path':'/PRIVATE'},{'path':'/PRIVATE'}):
            self.assertEqual(self.call('/api/v1/location',method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
        self.assertEqual(folder.asked,[])
        for action in ('open','choose'):
            status,_,payload=self.call('/api/v1/location',method='POST',body={'action':action},cookie=cookie,headers=headers)
            self.assertEqual((status,json.loads(payload)),('200 OK',{'action':action}))
        self.assertEqual(folder.asked,['open','choose'])

    def test_a_site_is_chosen_only_among_those_that_work_and_the_page_only_leaves_a_request_for_the_window(self):
        from comic_archive import app_runtime, sites
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        # A browser tab has no window to switch.
        self.assertEqual(self.call('/api/v1/site',method='POST',body={'site':'tapas'},cookie=cookie,headers=headers)[0],'404 Not Found')
        choice=self.registry.site_choice=app_runtime.SiteChoice()
        self.assertEqual(self.call('/api/v1/site',method='POST',body={'site':'tapas'})[0],'401 Unauthorized')
        self.assertEqual(self.call('/api/v1/site',method='POST',body={'site':'tapas'},cookie=cookie)[0],'403 Forbidden')
        # Only a site that works, named by its key alone.
        for body in ({},{'site':'webtoon'},{'site':'nowhere'},{'site':['tapas']},{'site':'tapas','path':'/PRIVATE'}):
            self.assertEqual(self.call('/api/v1/site',method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
        self.assertIsNone(choice.take())
        status,_,payload=self.call('/api/v1/site',method='POST',body={'site':'tapas'},cookie=cookie,headers=headers)
        self.assertEqual((status,json.loads(payload),choice.take()),('200 OK',{'site':'tapas'},sites.DEFAULT))

    def test_theme_choice_exists_only_in_the_desktop_app_and_changes_need_cookie_origin_and_csrf(self):
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        # In a browser tab nothing keeps a choice: the page follows the browser's own setting.
        self.assertIsNone(context['theme'])
        self.assertEqual(self.call('/api/v1/theme',method='POST',body={'theme':'dark'},cookie=cookie,headers=headers)[0],'404 Not Found')
        class Choice:
            value='system'
            def get(self): return self.value
            def set(self,value): self.value=value
        choice=self.registry.theme=Choice()
        self.assertEqual(json.loads(self.call('/api/v1/context',cookie=cookie)[2])['theme'],'system')
        self.assertEqual(self.call('/api/v1/theme',method='POST',body={'theme':'dark'})[0],'401 Unauthorized')
        self.assertEqual(self.call('/api/v1/theme',method='POST',body={'theme':'dark'},cookie=cookie)[0],'403 Forbidden')
        self.assertEqual(self.call('/api/v1/theme',cookie=cookie)[0],'404 Not Found')
        for body in ({},{'theme':'sepia'},{'theme':['dark']},{'theme':None},{'theme':'dark','path':'/PRIVATE'}):
            self.assertEqual(self.call('/api/v1/theme',method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
        self.assertEqual(choice.value,'system')
        status,_,payload=self.call('/api/v1/theme',method='POST',body={'theme':'dark'},cookie=cookie,headers=headers)
        self.assertEqual((status,json.loads(payload),choice.value),('200 OK',{'theme':'dark'},'dark'))
        self.assertEqual(json.loads(self.call('/api/v1/context',cookie=cookie)[2])['theme'],'dark')

    def test_other_sites_speeds_are_listed_and_changed_only_in_the_desktop_app_with_cookie_origin_and_csrf(self):
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        # A browser tab has no other sites.
        self.assertIsNone(context['site_settings'])
        self.assertEqual(self.call('/api/v1/site-speed',method='POST',body={'site':'tappytoon','speed':'slow'},cookie=cookie,headers=headers)[0],'404 Not Found')
        class Settings:
            chosen=[]
            def list(self): return [{'key':'tapas','name':'Tapas','current':True,'speed':'fast'}]
            def choose_speed(self,site,speed):
                if site=='tapas': raise ArchiveError('PRIVATE the open site')
                self.chosen.append((site,speed)); return self.list()
        settings=self.registry.site_settings=Settings()
        self.assertEqual(json.loads(self.call('/api/v1/context',cookie=cookie)[2])['site_settings'],settings.list())
        self.assertEqual(self.call('/api/v1/site-speed',method='POST',body={'site':'tappytoon','speed':'slow'})[0],'401 Unauthorized')
        self.assertEqual(self.call('/api/v1/site-speed',method='POST',body={'site':'tappytoon','speed':'slow'},cookie=cookie)[0],'403 Forbidden')
        for body in ({},{'site':'tappytoon'},{'speed':'slow'},{'site':'tappytoon','speed':3},{'site':['tappytoon'],'speed':'slow'},
                     {'site':'tappytoon','speed':'slow','path':'/PRIVATE'}):
            self.assertEqual(self.call('/api/v1/site-speed',method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
        status,_,payload=self.call('/api/v1/site-speed',method='POST',body={'site':'tapas','speed':'slow'},cookie=cookie,headers=headers)
        self.assertEqual((status,json.loads(payload)),('400 Bad Request',{'code':'request_unavailable'}))
        self.assertEqual(settings.chosen,[])
        status,_,payload=self.call('/api/v1/site-speed',method='POST',body={'site':'tappytoon','speed':'slow'},cookie=cookie,headers=headers)
        self.assertEqual((status,json.loads(payload),settings.chosen),('200 OK',{'site_settings':settings.list()},[('tappytoon','slow')]))

    def test_a_site_uses_the_shared_cloud_storage_or_its_own_as_asked_by_the_local_page(self):
        from comic_archive import shared
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route='/api/v1/workspaces/'+self.workspace.id+'/cloud/storage'
        self.assertIsNone(json.loads(self.call(route[:-len('/storage')],cookie=cookie)[2])['storage'])
        # A library the app has not linked has nothing to share.
        self.assertEqual(self.call(route,method='POST',body={'storage':'separate'},cookie=cookie,headers=headers)[0],'400 Bad Request')
        shared.link(self.workspace.library,self.root/'.comic-shared')
        self.assertEqual(self.call(route,method='POST',body={'storage':'separate'},cookie=cookie)[0],'403 Forbidden')
        for body in ({},{'storage':'both'},{'storage':['shared']},{'storage':'shared','path':'/PRIVATE'}):
            self.assertEqual(self.call(route,method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
        self.assertEqual(shared.cloud_mode(self.workspace.library),'shared')
        for mode in ('separate','shared'):
            status,_,payload=self.call(route,method='POST',body={'storage':mode},cookie=cookie,headers=headers)
            self.assertEqual((status,json.loads(payload)['storage'],shared.cloud_mode(self.workspace.library)),('200 OK',mode,mode))

    def test_site_list_is_in_the_context_only_when_the_desktop_app_gives_one(self):
        cookie=self.session()
        # In a browser tab there is no list: the page keeps its site choice hidden.
        self.assertIsNone(json.loads(self.call('/api/v1/context',cookie=cookie)[2])['sites'])
        self.registry.sites=choices()
        sites=json.loads(self.call('/api/v1/context',cookie=cookie)[2])['sites']
        self.assertEqual([(site['key'],site['available'],site['current']) for site in sites],
                         [('tapas',True,True),('webtoon',False,False),('tappytoon',True,False)])

    def test_both_pages_share_the_menu_with_settings_and_only_the_dashboard_lists_sites(self):
        cookie=self.session()
        for route,title in (('/','<title>Comic Archiver</title>'),('/library','<title>Saved library — Comic Archiver</title>')):
            page=self.call(route,cookie=cookie)[2].decode()
            self.assertIn(title,page); self.assertNotIn('Tapas Archive',page)
            self.assertIn('<a id="settings-nav" href="/#settings">Settings</a>',page)
            self.assertEqual(page.count('<select id="site">'),int(route=='/'))
        # Where the cloud helper is not offered, the app on this computer still has its download settings.
        with patch('comic_archive.cloud_tools.offered',return_value=False):
            self.assertIn('<a id="settings-nav" href="/#settings">Settings</a>',self.call('/',cookie=cookie)[2].decode())

    def test_open_existing_file_requires_local_cookie_origin_csrf_and_fixed_action(self):
        archive,identifier=self.file_to_open()
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route=f'/api/v1/workspaces/{self.workspace.id}/exports/{identifier}'
        with patch('comic_archive.local_web.WINDOWS',False), patch('comic_archive.local_web.subprocess.Popen') as launch:
            launch.return_value.wait.return_value=0
            self.assertEqual(self.call(route,method='POST',body={'action':'open'})[0],'401 Unauthorized')
            self.assertEqual(self.call(route,method='POST',body={'action':'open'},cookie=cookie)[0],'403 Forbidden')
            for body in ({},{'action':'download'},{'action':'open','path':'/PRIVATE'}, {'action':'open','command':'sh'}):
                self.assertEqual(self.call(route,method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
            launch.assert_not_called()
            before=list(archive.parent.iterdir())
            with LibraryLock(self.workspace.library):
                response=self.call(route,method='POST',body={'action':'open'},cookie=cookie,headers=headers)
            self.assertEqual(response[0],'200 OK');self.assertEqual(json.loads(response[2]),{'status':'open_requested'})
            self.assertEqual(launch.call_args.args[0],['xdg-open',str(archive)])
            self.assertNotIn(str(archive),response[2].decode())
            self.assertEqual(list(archive.parent.iterdir()),before)
            self.assertEqual(archive.read_bytes(),b'owned portable file')
            self.assertFalse(list(self.workspace.session.glob('worker/requests/*')))

    def test_open_refuses_foreign_missing_private_and_symlinked_files_before_launch(self):
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_web.WINDOWS',False), patch('comic_archive.local_web.subprocess.Popen') as launch:
            for owner,workspace,export in [('other-owner',self.workspace.id,identifier),
                    (LOCAL_OWNER,'0'*32,identifier),(LOCAL_OWNER,self.workspace.id,'0'*64),
                    (LOCAL_OWNER,self.workspace.id,'../../.comic-session/state.json')]:
                with self.assertRaises(ArchiveError): self.registry.open_file_on_device(owner,workspace,export)
            archive.unlink()
            with self.assertRaises(ArchiveError): self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            private=self.workspace.session/'state.json';private.write_text('PRIVATE_COOKIES')
            archive.symlink_to(private)
            with self.assertRaises(ArchiveError): self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            launch.assert_not_called();self.assertEqual(private.read_text(),'PRIVATE_COOKIES')

    def test_missing_opener_or_default_app_returns_fixed_private_error_and_allows_retry(self):
        archive,identifier=self.file_to_open()
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':self.app.origin,'HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route=f'/api/v1/workspaces/{self.workspace.id}/exports/{identifier}'
        with patch('comic_archive.local_web.WINDOWS',False), patch('comic_archive.local_web.subprocess.Popen') as launch:
            launch.side_effect=FileNotFoundError('/PRIVATE_PATH RAW_HELPER_ERROR')
            response=self.call(route,method='POST',body={'action':'open'},cookie=cookie,headers=headers)
            self.assertEqual(response[0],'400 Bad Request')
            self.assertEqual(json.loads(response[2]),{'code':'device_file_open_unavailable'})
            launch.side_effect=None;launch.return_value.wait.return_value=3
            self.assertEqual(self.call(route,method='POST',body={'action':'open'},cookie=cookie,headers=headers)[2],response[2])
            launch.return_value.wait.return_value=0
            self.assertEqual(self.call(route,method='POST',body={'action':'open'},cookie=cookie,headers=headers)[0],'200 OK')

    def test_default_app_that_stays_attached_is_not_killed_or_kept_waiting_in_browser(self):
        import subprocess
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_web.WINDOWS',False), patch('comic_archive.local_web.subprocess.Popen') as launch, patch('comic_archive.local_web.threading.Thread') as thread:
            launch.return_value.wait.side_effect=subprocess.TimeoutExpired('xdg-open',2)
            result=self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            self.assertEqual(result,{'status':'open_requested'})
            launch.return_value.kill.assert_not_called();launch.return_value.terminate.assert_not_called()
            thread.assert_called_once_with(target=launch.return_value.wait,daemon=True)
            thread.return_value.start.assert_called_once()

    def test_on_windows_the_saved_file_is_handed_to_windows_and_a_refusal_is_reported(self):
        from comic_archive.errors import DeviceFileOpenUnavailable
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_web.WINDOWS',True), patch('comic_archive.local_web.os.startfile',create=True) as opened, \
                patch('comic_archive.local_web.subprocess.Popen') as launch:
            self.assertEqual(self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier),{'status':'open_requested'})
            opened.assert_called_once_with(archive);launch.assert_not_called()
            opened.side_effect=OSError('PRIVATE')  # No program on this computer opens such a file.
            with self.assertRaises(DeviceFileOpenUnavailable):
                self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)

    def test_saved_library_view_can_bootstrap_locally_and_only_lists_owned_files(self):
        self.assertEqual(self.call('/library')[0],'401 Unauthorized')
        status, headers, body = self.call('/library', headers=self.launch())
        self.assertEqual(status,'200 OK'); self.assertIn(b'id="saved-files"',body)
        cookie=headers['Set-Cookie'].split(';',1)[0]
        route='/api/v1/workspaces/'+self.workspace.id+'/saved-files'
        self.assertEqual(self.call(route)[0], '401 Unauthorized')
        status, _, body = self.call(route, cookie=cookie, headers={'QUERY_STRING':'summary=1'})
        self.assertEqual(status,'200 OK'); self.assertEqual([g['id'] for g in json.loads(body)['locations']],['device'])
        self.assertEqual(self.call('/api/v1/workspaces/'+'0'*32+'/saved-files',cookie=cookie)[0], '404 Not Found')

    def test_button_signin_is_local_owner_csrf_scoped_and_credentials_never_enter_worker_queue(self):
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':'http://127.0.0.1:8765','HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route='/api/v1/workspaces/'+self.workspace.id+'/cloud/signin'
        credentials={'key_id':'K'*12,'application_key':'PRIVATE_KEY'+'A'*20}
        self.assertEqual(self.call(route,method='POST',body={'provider':'b2','credentials':credentials},cookie=cookie)[0],'403 Forbidden')
        for body in ({'provider':[]},{'provider':'ftp'},{'provider':'google','credentials':credentials},{'provider':'b2'},
                {'provider':'b2','credentials':{'password':'PRIVATE'}},{'provider':'b2','credentials':credentials|{'path':'/outside'}},
                {'provider':'b2','credentials':credentials,'path':'/outside'}):
            self.assertEqual(self.call(route,method='POST',body=body,headers=headers,cookie=cookie)[0],'400 Bad Request')
        with patch.object(self.registry.cloud_signin,'start',return_value={'status':'running','provider':'b2'}) as start:
            response=self.call(route,method='POST',body={'provider':'b2','credentials':credentials},headers=headers,cookie=cookie)
            self.assertEqual(response[0],'200 OK');start.assert_called_once_with('b2',credentials)
            self.assertNotIn('PRIVATE_KEY',response[2].decode())
            self.assertEqual(self.call('/api/v1/workspaces/'+'0'*32+'/cloud/signin',method='POST',body={'provider':'b2','credentials':credentials},headers=headers,cookie=cookie)[0],'404 Not Found')
        self.assertFalse(list(self.workspace.session.glob('worker/requests/*.json')))
        cancel=route.replace('/signin','/cancel-signin')
        self.assertEqual(self.call(cancel,method='POST',body={},cookie=cookie)[0],'403 Forbidden')
        self.assertEqual(self.call(cancel,method='POST',body={},headers=headers,cookie=cookie)[0],'200 OK')

    def test_cloud_test_controls_require_csrf_and_cleanup_belongs_to_this_library(self):
        cookie=self.session();context=json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers={'HTTP_ORIGIN':'http://127.0.0.1:8765','HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route='/api/v1/workspaces/'+self.workspace.id+'/cloud'
        queue=self.workspace.session/'worker';(queue/'requests').mkdir(parents=True);(queue/'status.json').write_text('{"state":"ready"}')
        self.assertEqual(self.call(route+'/test',method='POST',body={'provider':'b2'},cookie=cookie)[0],'403 Forbidden')
        for body in ({'provider':'b2','episodes':100},{'provider':[]},{'provider':'google'},{'password':'PRIVATE'}):
            self.assertEqual(self.call(route+'/test',method='POST',body=body,headers=headers,cookie=cookie)[0],'400 Bad Request')
        result=self.call(route+'/test',method='POST',body={'provider':'b2'},headers=headers,cookie=cookie)
        self.assertEqual(result[0],'200 OK')
        task=json.loads(result[2])['task_id']
        self.assertEqual(json.loads((queue/'requests'/f'{task}.json').read_text()),{'operation':'cloud_test','args':{'provider':'b2'}})
        test_id='f257c685-2a39-4e16-8dc0-353338e2f7d7'
        self.assertEqual(self.call(route+'/cleanup',method='POST',body={'test_id':'../outside'},headers=headers,cookie=cookie)[0],'400 Bad Request')
        with patch('comic_archive.cloud.status',return_value={'pending_test_cleanup':[]}):
            self.assertEqual(self.call(route+'/cleanup',method='POST',body={'test_id':test_id},headers=headers,cookie=cookie)[0],'400 Bad Request')
        with patch('comic_archive.cloud.status',return_value={'pending_test_cleanup':[{'test_id':test_id,'provider':'b2'}]}):
            result=self.call(route+'/cleanup',method='POST',body={'test_id':test_id},headers=headers,cookie=cookie)
        self.assertEqual(result[0],'200 OK');task=json.loads(result[2])['task_id']
        self.assertEqual(json.loads((queue/'requests'/f'{task}.json').read_text()),{'operation':'cloud_cleanup','args':{'test_id':test_id}})

    def test_network_choice_is_local_strict_csrf_protected_and_changes_during_import_lock(self):
        cookie = self.session()
        context = json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers = {'HTTP_ORIGIN':'http://127.0.0.1:8765','HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route = '/api/v1/workspaces/'+self.workspace.id+'/network'
        with patch('comic_archive.network.probe',return_value={'connection':'cellular','metered':True}):
            info = json.loads(self.call(route,cookie=cookie)[2])
            self.assertTrue(info['wifi_only']); self.assertFalse(info['allowed'])
            self.assertEqual(self.call(route,method='POST',body={'wifi_only':False},cookie=cookie)[0],'403 Forbidden')
            for body in ({'wifi_only':'false'},{'wifi_only':False,'path':'/outside'},{}):
                self.assertEqual(self.call(route,method='POST',body=body,cookie=cookie,headers=headers)[0],'400 Bad Request')
            with LibraryLock(self.workspace.library):
                result = self.call(route,method='POST',body={'wifi_only':False},cookie=cookie,headers=headers)
            self.assertEqual(result[0],'200 OK'); self.assertTrue(json.loads(result[2])['allowed'])
            self.assertEqual(self.call('/api/v1/workspaces/'+'0'*32+'/network',cookie=cookie)[0],'404 Not Found')

    def test_local_storage_and_cloud_are_private_fixed_and_do_not_accept_authentication_payloads(self):
        cookie = self.session()
        context = json.loads(self.call('/api/v1/context',cookie=cookie)[2])
        headers = {'HTTP_ORIGIN':'http://127.0.0.1:8765','HTTP_X_CSRF_TOKEN':context['csrf_token']}
        route = '/api/v1/workspaces/'+self.workspace.id
        info = json.loads(self.call(route+'/cloud',cookie=cookie)[2])
        self.assertTrue(info['supported_on_this_interface'])
        self.assertFalse(info['targets'][0]['connected'])
        self.assertEqual(self.call(route+'/cloud',method='POST',body={'provider':'b2','enabled':True},cookie=cookie)[0],'403 Forbidden')
        for body in ({'provider':'b2','enabled':True,'password':'SECRET'},{'remote':'PRIVATE'}):
            self.assertEqual(self.call(route+'/cloud',method='POST',body=body,headers=headers,cookie=cookie)[0],'400 Bad Request')
        with patch('comic_archive.cloud.enable',return_value=info) as enable:
            self.assertEqual(self.call(route+'/cloud',method='POST',body={'provider':'b2','enabled':True},headers=headers,cookie=cookie)[0],'200 OK')
            enable.assert_called_once_with(self.workspace.library,'b2',True)
        queue = self.workspace.session/'worker';(queue/'requests').mkdir(parents=True)
        (queue/'status.json').write_text('{"state":"ready"}')
        result = self.call(route+'/cloud/upload',method='POST',body={},headers=headers,cookie=cookie)
        self.assertEqual(result[0],'200 OK')
        request = next((queue/'requests').glob('*.json'))
        self.assertEqual(json.loads(request.read_text()),{'operation':'cloud_upload','args':{}})
        self.assertEqual(self.call(route+'/cloud/upload',method='POST',body={'limit':999},headers=headers,cookie=cookie)[0],'400 Bad Request')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry = LocalWorkspaceRegistry(self.root)
        self.workspace = self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id)
        self.app = LocalApplication(self.registry, 8765)

    def call(self, path='/', method='GET', body=None, headers=None, cookie=None):
        data = json.dumps(body).encode() if body is not None else b''
        env = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': '127.0.0.1:8765', 'wsgi.url_scheme': 'http',
               'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': '',
               'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(data)), 'wsgi.input': io.BytesIO(data)}
        if cookie is not None:
            env['HTTP_COOKIE'] = cookie
        env.update(headers or {})
        captured = []
        result = self.app(env, lambda status, values, exc_info=None: captured.append((status, dict(values))))
        try:
            payload = b''.join(result)
        finally:
            if hasattr(result, 'close'):
                result.close()
        return *captured[0], payload

    def launch(self, app=None):
        return {'QUERY_STRING': 'launch=' + (app or self.app).launch_token}

    def session(self):
        status, headers, _ = self.call(headers={'HTTP_SEC_FETCH_SITE': 'none'} | self.launch())
        self.assertEqual(status, '200 OK')
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_existing_library_and_session_are_reused_without_copying_or_chmoding_project(self):
        (self.workspace.session / 'owned.txt').write_text('PRIVATE')
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('42', 'Existing series', [], 'fixture')
        mode = self.root.stat().st_mode
        registry = LocalWorkspaceRegistry(self.root)
        self.assertEqual(registry.workspace_id, self.workspace.id)
        self.assertEqual(self.workspace.root, self.root)
        self.assertEqual(self.root.stat().st_mode, mode)
        self.assertEqual(registry.status(LOCAL_OWNER, self.workspace.id)['series_count'], 1)
        self.assertEqual((self.workspace.session / 'owned.txt').read_text(), 'PRIVATE')
        self.assertEqual(registry.ids(LOCAL_OWNER), [self.workspace.id])
        self.assertEqual(registry.ids('someone-else'), [])
        with self.assertRaises(ArchiveError): registry.resolve('someone-else', self.workspace.id)
        self.assertEqual((self.root / '.comic-web/registry.key').stat().st_mode & 0o777, PRIVATE_FILE)

    def test_dashboard_start_does_not_take_the_writer_lock_or_reset_an_active_import(self):
        from concurrent.futures import ThreadPoolExecutor
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('42', 'Existing', [{'id':'81','title':'E','order':1,'access_state':'tapas_free'}], 'subscribed')
            inventory.start_run()
            inventory.mark('81', 'downloading')
        with LibraryLock(self.workspace.library), ThreadPoolExecutor(max_workers=1) as executor:
            registry = executor.submit(LocalWorkspaceRegistry, self.root).result(timeout=5)
            self.assertEqual(registry.workspace_id, self.workspace.id)
        with Inventory(self.workspace.library, read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'], {'downloading':1})
            self.assertEqual(inventory.status()['last_run']['status'], 'running')

    def test_only_the_launch_address_bootstraps_private_cookie_and_assets_and_api_require_it(self):
        # Any program or user on this computer can reach the port; without the launch secret it gets no session.
        for path in ('/', '/library'):
            for method in ('GET', 'HEAD'):
                status, headers, body = self.call(path, method=method)
                self.assertEqual(status, '401 Unauthorized')
                self.assertNotIn('Set-Cookie', headers)
                self.assertEqual(body, b'' if method == 'HEAD' else NEEDS_LAUNCH)
        self.assertIn(b'Open Comic Archiver itself', NEEDS_LAUNCH)
        token = self.app.launch_token
        self.assertGreaterEqual(len(token), 43)
        for query in ('launch=', 'launch=' + token[:-1], 'launch=' + token + 'x', 'launch=' + token + '&x=1', 'x=1&launch=' + token,
                      'launch=' + token.swapcase(), 'launch=é' + token,
                      'launch=' + LocalApplication(self.registry, 8765).launch_token, 'token=' + token):
            status, headers, _ = self.call(headers={'QUERY_STRING': query})
            self.assertEqual(status, '401 Unauthorized', query)
            self.assertNotIn('Set-Cookie', headers)
        for path in ('/api/v1/context', '/assets/archive.js'):
            self.assertNotIn('Set-Cookie', self.call(path, headers=self.launch())[1])
        self.assertEqual(self.call(method='POST', body={}, headers=self.launch())[0], '401 Unauthorized')
        status, headers, body = self.call(headers=self.launch())
        self.assertEqual(status, '200 OK')
        self.assertIn(b'archive.js', body)
        self.assertTrue(headers['Set-Cookie'].startswith(COOKIE_NAME + '-8765='))
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('Domain=', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self.app.launch_url, 'http://127.0.0.1:8765/?launch=' + token)
        # The page itself, once open, loads without the secret.
        self.assertEqual(self.call(cookie=cookie)[0], '200 OK')
        for path in ('/assets/archive.js', '/assets/archive.css', '/api/v1/context'):
            self.assertEqual(self.call(path)[0], '401 Unauthorized')
            status, headers, body = self.call(path, cookie=cookie)
            self.assertEqual(status, '200 OK')
            self.assertNotIn('Set-Cookie', headers)
            self.assertNotIn(b'PRIVATE', body)
        context = json.loads(self.call('/api/v1/context', cookie=cookie)[2])
        self.assertEqual(context['workspace_ids'], [self.workspace.id])
        self.assertEqual((context['interface_mode'], context['desktop_only']), ('local', True))

    def test_cookie_signature_age_and_forged_principal_are_checked(self):
        with patch('comic_archive.local_web.time.time', return_value=100000):
            cookie = self.session()
            self.assertEqual(self.call('/api/v1/context', cookie=cookie)[0], '200 OK')
            # A cookie header that cannot be parsed is refused, not a server error.
            self.assertEqual(self.call('/api/v1/context', cookie=cookie + '; $unknown=1')[0], '401 Unauthorized')
        for stamp in (99999, 100000 + SESSION_SECONDS + 1):
            with patch('comic_archive.local_web.time.time', return_value=stamp):
                self.assertEqual(self.call('/api/v1/context', cookie=cookie)[0], '401 Unauthorized')
        forged = cookie[:-1] + ('1' if cookie[-1] != '1' else '2')
        for value in (forged, 'bad=1', COOKIE_NAME + '=not-signed', 'x' * 4097):
            self.assertEqual(self.call('/api/v1/context', cookie=value, headers={'comic.local.principal': LOCAL_OWNER})[0], '401 Unauthorized')

    def test_a_page_in_use_has_its_cookie_issued_again_past_half_its_life(self):
        with patch('comic_archive.local_web.time.time', return_value=100000):
            cookie = self.session()
        with patch('comic_archive.local_web.time.time', return_value=100000 + SESSION_SECONDS // 2):
            status, headers, _ = self.call('/api/v1/context', cookie=cookie)
            self.assertEqual(status, '200 OK'); self.assertNotIn('Set-Cookie', headers)
        with patch('comic_archive.local_web.time.time', return_value=100000 + SESSION_SECONDS // 2 + 1):
            status, headers, _ = self.call('/api/v1/context', cookie=cookie)
            self.assertEqual(status, '200 OK')
            self.assertIn('HttpOnly', headers['Set-Cookie']); self.assertIn(f'Max-Age={SESSION_SECONDS}', headers['Set-Cookie'])
            renewed = headers['Set-Cookie'].split(';', 1)[0]
            # A refused request renews nothing.
            self.assertNotIn('Set-Cookie', self.call('/api/v1/workspaces/' + '0' * 32, cookie=cookie)[1])
        with patch('comic_archive.local_web.time.time', return_value=100000 + SESSION_SECONDS + 60):
            self.assertEqual(self.call('/api/v1/context', cookie=cookie)[0], '401 Unauthorized')
            self.assertEqual(self.call('/api/v1/context', cookie=renewed)[0], '200 OK')

    def test_each_port_has_its_own_cookie_and_launch_secret_and_a_site_switch_keeps_both(self):
        other = LocalApplication(self.registry, 8766)
        self.assertNotEqual(other.launch_token, self.app.launch_token)
        self.assertEqual((other.cookie_name, self.app.cookie_name), (COOKIE_NAME + '-8766', COOKIE_NAME + '-8765'))
        cookie = self.session()
        env = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': '127.0.0.1:8766', 'wsgi.url_scheme': 'http', 'REQUEST_METHOD': 'GET',
               'PATH_INFO': '/api/v1/context', 'QUERY_STRING': '', 'HTTP_COOKIE': cookie}
        captured = []
        other(env, lambda status, values, exc_info=None: captured.append(status))
        self.assertEqual(captured, ['401 Unauthorized'])
        switched = LocalApplication(self.registry, 8765, cookie_key=self.app.cookie_key, launch_token=self.app.launch_token)
        self.assertEqual(switched.launch_url, self.app.launch_url)
        self.app = switched
        self.assertEqual(self.call('/api/v1/context', cookie=cookie)[0], '200 OK')
        for token in ('', 'short', 'x' * 129, 'a/b' * 20, 42):
            with self.assertRaises(ArchiveError):
                LocalApplication(self.registry, 8765, launch_token=token)

    def test_a_slow_saved_library_listing_does_not_hold_up_other_requests(self):
        from concurrent.futures import ThreadPoolExecutor
        cookie = self.session()
        scanning, release = threading.Event(), threading.Event()
        real = self.registry.saved_catalog.collect

        def slow(*args):
            scanning.set(); release.wait(10)
            return real(*args)
        route = '/api/v1/workspaces/' + self.workspace.id
        with patch.object(self.registry.saved_catalog, 'collect', side_effect=slow), ThreadPoolExecutor(max_workers=2) as pool:
            listing = pool.submit(self.call, route + '/saved-files', cookie=cookie, headers={'QUERY_STRING': 'summary=1&refresh=1'})
            self.assertTrue(scanning.wait(5))
            try:
                # The dashboard's status and context answer while the scan goes on.
                self.assertEqual(pool.submit(self.call, route, cookie=cookie).result(timeout=5)[0], '200 OK')
            finally:
                release.set()
            self.assertEqual(listing.result(timeout=10)[0], '200 OK')

    def test_remote_cross_site_and_dns_rebinding_requests_never_bootstrap_or_authenticate(self):
        cookie = self.session()
        cases = ({'REMOTE_ADDR': '192.0.2.1'}, {'HTTP_HOST': 'evil.example:8765'},
                 {'HTTP_HOST': '127.0.0.1:8766'}, {'wsgi.url_scheme': 'https'},
                 {'HTTP_SEC_FETCH_SITE': 'cross-site'}, {'HTTP_SEC_FETCH_SITE': 'same-site'},
                 {'HTTP_ORIGIN': 'https://evil.example'}, {'HTTP_REFERER': 'https://evil.example/'},
                 {'HTTP_REFERER': 'http://[::1'},
                 {'REMOTE_ADDR': '192.0.2.1', 'HTTP_X_FORWARDED_FOR': '127.0.0.1'},
                 {'HTTP_HOST': 'evil.example', 'HTTP_X_FORWARDED_HOST': '127.0.0.1:8765'})
        for headers in cases:
            for path in ('/', '/api/v1/context'):
                status, values, _ = self.call(path, cookie=cookie, headers=headers)
                self.assertEqual(status, '403 Forbidden')
                self.assertNotIn('Set-Cookie', values)
        self.assertEqual(self.call(method='POST', body={})[0], '401 Unauthorized')
        self.assertNotIn('Set-Cookie', self.call(headers={'QUERY_STRING': 'token=PRIVATE'})[1])

    def test_user_can_click_a_dashboard_link_but_frames_post_forms_and_api_calls_cannot_bootstrap(self):
        headers = {'HTTP_SEC_FETCH_SITE': 'cross-site', 'HTTP_SEC_FETCH_MODE': 'navigate',
                   'HTTP_SEC_FETCH_DEST': 'document', 'HTTP_SEC_FETCH_USER': '?1',
                   'HTTP_REFERER': 'https://another-site.example/'}
        # Without the launch secret, another site's link opens nothing.
        self.assertEqual(self.call(headers=headers)[0], '403 Forbidden')
        headers |= self.launch()
        status, values, _ = self.call(headers=headers)
        self.assertEqual(status, '200 OK')
        cookie = values['Set-Cookie'].split(';', 1)[0]
        for changed in ({'HTTP_SEC_FETCH_DEST': 'iframe'}, {'HTTP_SEC_FETCH_USER': ''},
                        {'HTTP_SEC_FETCH_MODE': 'cors'}, {'HTTP_ORIGIN': 'https://another-site.example'}):
            self.assertEqual(self.call(headers=headers | changed)[0], '403 Forbidden')
        self.assertEqual(self.call('/api/v1/context', headers=headers, cookie=cookie)[0], '403 Forbidden')
        self.assertEqual(self.call(method='POST', body={}, headers=headers, cookie=cookie)[0], '403 Forbidden')

    def test_symlinked_session_or_project_is_rejected_without_changing_files(self):
        other = self.root / 'other'
        other.mkdir()
        project = self.root / 'project'
        project.mkdir()
        (project / '.comic-session').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(project)
        link = self.root / 'linked-project'
        link.symlink_to(project, target_is_directory=True)
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(link)
        self.assertEqual(list(other.iterdir()), [])

    def test_server_binds_only_loopback_and_bind_errors_do_not_expose_raw_details(self):
        with patch('comic_archive.local_web.make_server') as make:
            make.return_value.server_port = 9876
            server = create_server(self.root, 9876)
            make.assert_called_once_with('127.0.0.1', 9876, None, server_class=LocalServer, handler_class=PrivateRequestHandler)
            self.assertIsInstance(server.set_app.call_args.args[0], LocalApplication)
            self.assertEqual(server.set_app.call_args.args[0].origin, 'http://127.0.0.1:9876')
        with patch('comic_archive.local_web.make_server', side_effect=PermissionError('PRIVATE_INTERNAL_DETAILS')):
            with self.assertRaises(ArchiveError) as result:
                create_server(self.root)
            self.assertNotIn('PRIVATE', str(result.exception))

    def test_request_logging_omits_paths_cookies_and_raw_bad_request_messages(self):
        handler = object.__new__(PrivateRequestHandler)
        handler.command = 'GET'
        handler.path = '/?cookie=PRIVATE'
        with patch('comic_archive.local_web.WSGIRequestHandler.log_message') as log:
            handler.log_request(200, 987)
            handler.log_message('Bad request: %s', 'COOKIE_PRIVATE')
            log.assert_called_once_with('%s %s', 'GET', '200')

    def test_browser_signin_and_account_views_are_cookie_protected_read_only_and_reject_wrong_methods(self):
        cookie = self.session()
        headers = {'HTTP_ORIGIN': self.app.origin, 'HTTP_X_CSRF_TOKEN': json.loads(self.call('/api/v1/context', cookie=cookie)[2])['csrf_token']}
        base = '/api/v1/workspaces/' + self.workspace.id
        self.assertEqual(self.call(base + '/session/signin')[0], '401 Unauthorized')
        self.assertEqual(self.call(base + '/account')[0], '401 Unauthorized')
        self.assertEqual(json.loads(self.call(base + '/session/signin', cookie=cookie)[2]), {'status': 'idle'})
        account = json.loads(self.call(base + '/account', cookie=cookie)[2])
        self.assertTrue(account['supported']); self.assertFalse(account['configured']); self.assertFalse(account['coverage_verified'])
        for method, suffix in (('GET', '/account/pause'), ('POST', '/account/other')):
            status, _, body = self.call(base + suffix, method=method, body={} if method == 'POST' else None, headers=headers, cookie=cookie)
            self.assertEqual((status, json.loads(body)), ('405 Method Not Allowed', {'code': 'method_not_allowed'}))

    def test_browser_signin_is_refused_while_the_downloader_is_busy_and_does_not_start(self):
        cookie = self.session()
        headers = {'HTTP_ORIGIN': self.app.origin, 'HTTP_X_CSRF_TOKEN': json.loads(self.call('/api/v1/context', cookie=cookie)[2])['csrf_token']}
        worker = self.workspace.session / 'worker'; worker.mkdir(parents=True)
        (worker / 'status.json').write_text('{"state":"busy"}')
        with patch.object(self.registry.site_signin, 'start') as start:
            status, values, body = self.call('/api/v1/workspaces/' + self.workspace.id + '/session/signin', method='POST', body={}, headers=headers, cookie=cookie)
        self.assertEqual((status, json.loads(body)), ('409 Conflict', {'code': 'library_busy'}))
        start.assert_not_called()
        self.assertEqual(values['Retry-After'], '2')
        self.assertEqual(self.registry.site_signin.status(), {'status': 'idle'})

    def test_cloud_signin_status_is_readable_and_cancel_accepts_neither_a_body_nor_a_read(self):
        cookie = self.session()
        headers = {'HTTP_ORIGIN': self.app.origin, 'HTTP_X_CSRF_TOKEN': json.loads(self.call('/api/v1/context', cookie=cookie)[2])['csrf_token']}
        route = '/api/v1/workspaces/' + self.workspace.id + '/cloud/signin'
        self.assertEqual(self.call(route)[0], '401 Unauthorized')
        self.assertEqual(json.loads(self.call(route, cookie=cookie)[2]), {'status': 'idle'})
        self.assertEqual(self.call('/api/v1/workspaces/' + '0' * 32 + '/cloud/signin', cookie=cookie)[0], '404 Not Found')
        cancel = route.replace('/signin', '/cancel-signin')
        status, _, body = self.call(cancel, method='POST', body={'provider': 'google'}, headers=headers, cookie=cookie)
        self.assertEqual((status, json.loads(body)), ('400 Bad Request', {'code': 'unknown_options'}))
        self.assertEqual(self.call(cancel, cookie=cookie)[0], '405 Method Not Allowed')

    def test_vanished_or_symlinked_library_is_reported_unavailable_and_never_recreated(self):
        cookie = self.session()
        route = '/api/v1/workspaces/' + self.workspace.id
        self.assertEqual(self.call(route, cookie=cookie)[0], '200 OK')
        library = self.workspace.library
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        library.rename(self.root / 'moved-library'); library.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(json.loads(self.call(route, cookie=cookie)[2]), {'code': 'not_found'})
        self.assertEqual(list(elsewhere.iterdir()), [])
        library.unlink()
        self.assertEqual(self.call(route, cookie=cookie)[0], '404 Not Found')
        self.assertFalse(library.exists() or library.is_symlink())

    def test_library_that_is_a_file_is_refused_and_left_alone(self):
        project = self.root / 'project'; project.mkdir()
        (project / 'Library').write_text('NOT A FOLDER')
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(project)
        self.assertEqual((project / 'Library').read_text(), 'NOT A FOLDER')
    def archive_one(self):
        from contextlib import closing
        from comic_archive.archive import Store
        from test_jobs import AccountAdapter
        adapter = AccountAdapter(); manifest = adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.workspace.library)) as store: store.archive(manifest, adapter, progress=lambda _: None)
        archive = next(self.workspace.library.glob('*/*.cbz'))
        return archive, archive.with_suffix('.json'), archive.parent / 'series.json'

    def listed(self):
        return {path.name: kind for path, kind in self.registry._exports(self.workspace).values()}

    def test_session_state_stray_covers_and_private_folders_are_never_offered_as_saved_files(self):
        import shutil
        from test_archive import png
        archive, sidecar, series = self.archive_one()
        (self.workspace.session / 'state.json').write_text('PRIVATE_COOKIES')
        (series.parent / 'cover.jpg').write_bytes(png(1))  # Never named by the series file.
        for name in ('.archive-state', '.archive-cache', '.other-private'):
            folder = self.workspace.library / name; folder.mkdir(exist_ok=True)
            shutil.copy2(archive, folder / 'private.cbz')
            shutil.copy2(sidecar, folder / 'private.json')
            shutil.copy2(series, folder / 'series.json')
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata', 'series.json': 'series_metadata'})

    def test_unreadable_series_file_or_unpaired_folders_never_break_or_widen_the_saved_files(self):
        from test_archive import png
        archive, sidecar, series = self.archive_one()
        orphans = self.workspace.library / 'orphans'; orphans.mkdir()
        (orphans / 'episode.cbz').write_bytes(b'NO_SIDECAR'); (orphans / 'series.json').write_text('{}')
        (series.parent / 'cover.png').write_bytes(png(1))
        value = json.loads(series.read_text())
        for content in ('not json', '[]', json.dumps(value | {'cover_file': {'malformed': 'field'}}),
                        json.dumps(value | {'cover_file': '../cover.png'})):
            series.write_text(content)
            self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata', 'series.json': 'series_metadata'}, content)
        series.write_text(json.dumps(value | {'cover_file': 'cover.png'}))
        self.assertEqual(self.listed()['cover.png'], 'cover')
        outside = self.root / 'outside.json'; outside.write_text(json.dumps({'cover_file': 'cover.png'}))
        series.unlink(); series.symlink_to(outside)
        # The link is left out, never followed; the episode beside it is still offered.
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata'})
        series.unlink()
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata'})

    def test_a_linked_file_or_linked_details_are_left_out_and_the_rest_of_the_library_is_still_offered(self):
        archive, sidecar, _ = self.archive_one()
        other = archive.with_name('0002 - Other.cbz'); other.write_bytes(archive.read_bytes())
        other.with_suffix('.json').write_bytes(sidecar.read_bytes())
        private = self.root / 'private.cbz'; private.write_bytes(b'PRIVATE_COOKIES')
        linked = archive.with_name('0003 - Linked.cbz'); linked.symlink_to(private)
        linked.with_suffix('.json').write_bytes(sidecar.read_bytes())
        lone = archive.with_name('0004 - Lone.cbz'); lone.write_bytes(archive.read_bytes())
        lone.with_suffix('.json').symlink_to(sidecar)  # Its details are a link: the episode is left out with them.
        self.assertEqual(set(self.listed()), {archive.name, sidecar.name, other.name, other.with_suffix('.json').name, 'series.json'})

    def test_status_reports_a_session_file_but_refuses_a_symlinked_one(self):
        status = self.registry.status(LOCAL_OWNER, self.workspace.id)
        self.assertFalse(status['session_configured']); self.assertFalse(status['inventory_complete'])
        self.assertNotIn(str(self.root), json.dumps(status))
        state = self.workspace.session / 'state.json'; state.write_text('PRIVATE_COOKIES')
        self.assertTrue(self.registry.status(LOCAL_OWNER, self.workspace.id)['session_configured'])
        outside = self.root / 'outside.json'; outside.write_text('PRIVATE_COOKIES')
        state.unlink(); state.symlink_to(outside)
        with self.assertRaises(ArchiveError): self.registry.status(LOCAL_OWNER, self.workspace.id)


if __name__ == '__main__':
    unittest.main()
