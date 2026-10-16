"""Small authenticated WSGI interface for an existing website.

The host supplies authentication middleware. No owner header, password, Tapas
cookie, proxy configuration, executable command or filesystem path is accepted
from requests. This module does not launch workers or acquire Tapas sessions.
"""
import hashlib
import hmac
import json
import re
import time
from importlib.resources import files
from urllib.parse import quote, urlsplit

from . import cloud_tools, sites
from .errors import LibraryBusy, DeviceFileOpenUnavailable
from .hosted import WorkspaceRegistry
from .models import ArchiveError

THEMES = ('system','light','dark')
LOCATION_ACTIONS = ('open','choose')


class HTTPError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class FileBody:
    """A verified descriptor remains pinned until the server finishes/aborts."""
    def __init__(self, download):
        self.download = download

    def __iter__(self):
        try:
            while chunk := self.download.stream.read(65536):
                yield chunk
        except OSError:
            raise ArchiveError('Export read failed; retry the download.') from None
        finally:
            self.close()

    def close(self):
        self.download.close()


class HostedApplication:
    def __init__(self, registry, authenticate, public_origin, max_workspaces=3):
        if not isinstance(registry, WorkspaceRegistry) or not callable(authenticate):
            raise ArchiveError('The website must supply an owner registry and trusted authentication callback.')
        try:
            origin = urlsplit(public_origin)
            if (not origin.hostname or origin.username or origin.password or origin.path not in {'','/'} or origin.query
                    or origin.fragment or origin.scheme not in {'https','http'}
                    or (origin.scheme=='http' and origin.hostname not in {'localhost','127.0.0.1','::1'})):
                raise ValueError
            origin.port
        except (ValueError, TypeError, AttributeError):
            raise ArchiveError('Use an HTTPS website origin, or a loopback HTTP origin for local tests.') from None
        if type(max_workspaces) is not int or not 1<=max_workspaces<=100:
            raise ArchiveError('Workspace quota must be a server-configured positive integer.')
        self.registry, self.authenticate = registry, authenticate
        host=origin.hostname
        if ':' in host:
            host='['+host+']'
        port=origin.port
        suffix=':'+str(port) if port is not None and port!={'https':443,'http':80}[origin.scheme] else ''
        self.public_origin, self.max_workspaces = f'{origin.scheme}://{host}{suffix}', max_workspaces

    def _csrf(self, owner, hour):
        owner_key = self.registry._owner_key(owner)
        message = f'website-csrf\0{owner_key}\0{hour}'.encode()
        return hmac.new(self.registry._key, message, hashlib.sha256).hexdigest()

    def _mutation(self, environ, owner):
        # Compare with deployment configuration, never Host/forwarded headers.
        if environ.get('HTTP_ORIGIN') != self.public_origin:
            raise HTTPError('403 Forbidden','origin_required')
        token = environ.get('HTTP_X_CSRF_TOKEN','')
        hour = int(time.time() // 3600)
        if not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{64}',token) or not any(
                hmac.compare_digest(token,self._csrf(owner,value)) for value in (hour,hour-1)):
            raise HTTPError('403 Forbidden','csrf_required')
        if environ.get('CONTENT_TYPE','').split(';',1)[0].strip() != 'application/json':
            raise HTTPError('415 Unsupported Media Type','json_required')
        length = environ.get('CONTENT_LENGTH','')
        if not isinstance(length,str) or not re.fullmatch(r'[0-9]{1,5}',length) or int(length)>16384:
            raise HTTPError('413 Content Too Large','body_limit')
        try:
            data = environ['wsgi.input'].read(int(length))
            if len(data) != int(length):
                raise ValueError
            value = json.loads(data)
            if not isinstance(value,dict):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise HTTPError('400 Bad Request','invalid_json') from None

    def __call__(self, environ, start_response):
        download = None
        try:
            owner = self.authenticate(environ)
            if not isinstance(owner,str) or not owner or len(owner.encode('utf-8'))>256:
                raise HTTPError('401 Unauthorized','website_login_required')
            method = environ.get('REQUEST_METHOD','GET')
            path = environ.get('PATH_INFO','')
            if method not in {'GET','HEAD','POST'}:
                raise HTTPError('405 Method Not Allowed','method_not_allowed')
            query = environ.get('QUERY_STRING','')
            if query and not (method in {'GET','HEAD'} and re.fullmatch(r'/api/v1/workspaces/[a-f0-9]{32}/saved-files', path)):
                raise HTTPError('400 Bad Request','query_not_supported')
            assets={'/':('index.html','text/html; charset=utf-8'),
                    '/library':('library.html','text/html; charset=utf-8'),
                    '/assets/library.js':('library.js','text/javascript; charset=utf-8'),
                    '/assets/archive.js':('archive.js','text/javascript; charset=utf-8'),
                    '/assets/theme.js':('theme.js','text/javascript; charset=utf-8'),
                    '/assets/archive.css':('archive.css','text/css; charset=utf-8')}
            if path in assets:
                if method not in {'GET','HEAD'}:
                    raise HTTPError('405 Method Not Allowed','method_not_allowed')
                filename,media=assets[path]
                data=files('comic_archive').joinpath('static',filename).read_bytes()
                if filename in {'index.html','library.html'}:
                    simple = self.registry.simple_mode
                    header = files('comic_archive').joinpath('static','header.html').read_text(encoding='utf-8').format(
                        library_current=' aria-current="page"' if filename == 'library.html' else '',
                        save_current=' aria-current="page"' if filename == 'index.html' else '',
                        save_href='/' if simple else '/#import-title',
                        save_label='Dashboard' if simple else 'Save episodes',
                        advanced_hidden=' hidden' if simple else '',
                        # Cloud storage is set up on the person's own computer, where its helper is offered.
                        storage_hidden='' if cloud_tools.offered() and (
                            not self.registry.desktop_only or self.registry.interface_mode == 'local') else ' hidden')
                    data = data.replace(b'<!-- shared-header -->', header.encode('utf-8'))
                headers=[(key,value) for key,value in self._headers(media) if key!='Content-Security-Policy']
                headers += [('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                            ('Content-Length',str(len(data)))]
                start_response('200 OK',headers)
                return [] if method=='HEAD' else [data]
            if path == '/api/v1/context' and method in {'GET','HEAD'}:
                value = {'csrf_token':self._csrf(owner,int(time.time() // 3600)),
                         'interface_mode':self.registry.interface_mode,
                         'desktop_only':self.registry.desktop_only,
                         'cloud_offered':cloud_tools.offered(),
                         'simple_mode':self.registry.simple_mode,
                         'theme':self.registry.theme.get() if self.registry.theme else None,
                         'sites':self.registry.sites,
                         'site_switching':self.registry.site_choice.waiting() if self.registry.site_choice else False,
                         'library_folder':self.registry.location.get() if self.registry.location else None,
                         'workspace_ids':self.registry.ids(owner),'max_workspaces':self.max_workspaces,
                         'limits':{key:getattr(self.registry.limits,key) for key in ('limit','max_pages','max_bytes','max_seconds')}}
            elif path == '/api/v1/location' and method == 'POST' and self.registry.location:
                options = self._mutation(environ,owner)
                if set(options) != {'action'} or options['action'] not in LOCATION_ACTIONS:
                    raise HTTPError('400 Bad Request','unknown_options')
                # The app's own window opens the folder or asks for another; no path comes from the page.
                self.registry.location.ask(options['action'])
                value = {'action':options['action']}
            elif path == '/api/v1/site' and method == 'POST' and self.registry.site_choice:
                options = self._mutation(environ,owner)
                site = sites.find(options.get('site')) if set(options) == {'site'} and isinstance(options['site'],str) else None
                if site is None:
                    raise HTTPError('400 Bad Request','unknown_options')
                # The app's own window switches; it asks first if a download or sign-in would be cut short.
                self.registry.site_choice.ask(site)
                value = {'site':site.key}
            elif path == '/api/v1/theme' and method == 'POST' and self.registry.theme:
                options = self._mutation(environ,owner)
                if set(options) != {'theme'} or options['theme'] not in THEMES:
                    raise HTTPError('400 Bad Request','unknown_options')
                self.registry.theme.set(options['theme'])
                value = {'theme':self.registry.theme.get()}
            elif path == '/api/v1/workspaces':
                if method in {'GET','HEAD'}:
                    value = {'workspace_ids':self.registry.ids(owner)}
                else:
                    if self._mutation(environ,owner):
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = {'workspace_id':self.registry.create(owner,max_per_owner=self.max_workspaces).id}
            else:
                match = re.fullmatch(r'/api/v1/workspaces/([a-f0-9]{32})(?:/(jobs|daily|exports|saved-files|tasks|series|session|audit|stop|priority|storage|cloud|network|account)(?:/([a-z0-9-]{1,64}))?)?',path)
                if match is None:
                    raise HTTPError('404 Not Found','not_found')
                workspace_id, action, key = match.groups()
                try:
                    self.registry.resolve(owner,workspace_id)
                except ArchiveError:
                    raise HTTPError('404 Not Found','not_found') from None
                if action is None and method in {'GET','HEAD'}:
                    value = self.registry.status(owner,workspace_id)
                elif action == 'account' or (action == 'session' and key == 'signin'):
                    if self.registry.interface_mode != 'local':
                        raise HTTPError('400 Bad Request', 'request_unavailable')
                    if method in {'GET','HEAD'} and (action == 'session' or key is None):
                        value = self.registry.tapas_signin_status(owner, workspace_id) if action == 'session' else self.registry.account_status(owner, workspace_id)
                    elif method == 'POST' and action == 'account' and key == 'speed':
                        options = self._mutation(environ, owner)
                        if set(options) != {'speed'} or not isinstance(options['speed'], str):
                            raise HTTPError('400 Bad Request', 'unknown_options')
                        value = self.registry.choose_speed(owner, workspace_id, options['speed'])
                    elif method == 'POST' and ((action == 'account' and key in {None,'pause'}) or action == 'session'):
                        if self._mutation(environ, owner):
                            raise HTTPError('400 Bad Request', 'unknown_options')
                        value = (self.registry.start_tapas_signin(owner, workspace_id) if action == 'session'
                            else self.registry.pause_account(owner, workspace_id) if key == 'pause'
                            else self.registry.start_account(owner, workspace_id))
                    else:
                        raise HTTPError('405 Method Not Allowed','method_not_allowed')
                elif action == 'saved-files' and key is None and method in {'GET','HEAD'}:
                    from .saved_library import options
                    value = self.registry.saved_files(owner, workspace_id, **options(query))
                elif action == 'series' and key is None and method in {'GET','HEAD'}:
                    value = {'series':self.registry.series(owner,workspace_id)}
                elif action == 'audit' and key is None and method in {'GET','HEAD'}:
                    value = self.registry.audit(owner,workspace_id)
                elif action == 'storage' and key is None and method in {'GET','HEAD'}:
                    value = self.registry.storage(owner,workspace_id)
                elif action == 'storage' and key is None and method == 'POST':
                    body = self._mutation(environ,owner)
                    if set(body) != {'image_profile'}:
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.configure_storage(owner,workspace_id,body['image_profile'])
                elif action == 'storage' and key == 'purge' and method == 'POST':
                    if self._mutation(environ,owner):
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.purge_cache(owner,workspace_id)
                elif action == 'storage' and key == 'preview' and method == 'POST':
                    body = self._mutation(environ, owner)
                    from .preflight import validate_options
                    validate_options(body)
                    value = self.registry.preview_series_storage(owner, workspace_id, body['url'], body['scope'])
                elif action == 'cloud' and key is None and method in {'GET','HEAD'}:
                    value = self.registry.cloud_status(owner,workspace_id)
                elif action == 'cloud' and key is None and method == 'POST':
                    body = self._mutation(environ,owner)
                    if set(body) != {'provider','enabled'}:
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.configure_cloud(owner,workspace_id,body['provider'],body['enabled'])
                elif action == 'cloud' and key in {'signin', 'cancel-signin'}:
                    if self.registry.interface_mode != 'local':
                        raise HTTPError('400 Bad Request', 'request_unavailable')
                    if key == 'signin' and method in {'GET', 'HEAD'}:
                        value = self.registry.cloud_signin_status(owner, workspace_id)
                    elif method == 'POST':
                        body = self._mutation(environ, owner)
                        if key == 'cancel-signin':
                            if body:
                                raise HTTPError('400 Bad Request', 'unknown_options')
                            value = self.registry.cancel_cloud_signin(owner, workspace_id)
                        else:
                            if set(body) - {'provider', 'credentials'} or 'provider' not in body:
                                raise HTTPError('400 Bad Request', 'unknown_options')
                            from .cloud_auth import validate
                            credentials = body.get('credentials', {})
                            validate(body['provider'], credentials)
                            value = self.registry.start_cloud_signin(owner, workspace_id, body['provider'], credentials)
                    else:
                        raise HTTPError('405 Method Not Allowed', 'method_not_allowed')
                elif action == 'cloud' and key == 'upload' and method == 'POST':
                    if self._mutation(environ,owner):
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.upload_cloud(owner,workspace_id)
                elif action == 'cloud' and key == 'test' and method == 'POST':
                    body = self._mutation(environ, owner)
                    from .cloud import PROVIDERS
                    if set(body) != {'provider'} or not isinstance(body['provider'], str) or body['provider'] not in PROVIDERS:
                        raise HTTPError('400 Bad Request', 'unknown_options')
                    value = self.registry.test_cloud(owner, workspace_id, body['provider'])
                elif action == 'cloud' and key == 'cleanup' and method == 'POST':
                    body = self._mutation(environ, owner)
                    from .publication import valid_export_id
                    if set(body) != {'test_id'} or not valid_export_id(body['test_id']):
                        raise HTTPError('400 Bad Request', 'unknown_options')
                    value = self.registry.cleanup_cloud_test(owner, workspace_id, body['test_id'])
                elif action == 'network' and key is None and method in {'GET', 'HEAD'}:
                    value = self.registry.network_status(owner, workspace_id)
                elif action == 'network' and key is None and method == 'POST':
                    body = self._mutation(environ, owner)
                    if set(body) != {'wifi_only'} or type(body['wifi_only']) is not bool:
                        raise HTTPError('400 Bad Request', 'unknown_options')
                    value = self.registry.configure_network(owner, workspace_id, body['wifi_only'])
                elif action == 'stop' and key is None and method=='POST':
                    body=self._mutation(environ,owner)
                    if set(body)!={'run_id'}:
                        raise HTTPError('400 Bad Request','unknown_options')
                    value=self.registry.stop_run(owner,workspace_id,body['run_id'])
                elif action == 'session' and key == 'check' and method == 'POST':
                    if self._mutation(environ,owner):
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.check_session(owner,workspace_id)
                elif action=='priority' and key is None and method=='POST':
                    body=self._mutation(environ,owner)
                    if set(body)!={'priority'}: raise HTTPError('400 Bad Request','unknown_options')
                    value=self.registry.configure_priority(owner,workspace_id,body['priority'])
                elif action == 'jobs' and key is None and method == 'POST':
                    body = self._mutation(environ,owner)
                    if set(body)-{'operation','options'} or body.get('operation') not in {'sync','archive_discovered','archive_watched_series','archive_series_url'}:
                        raise HTTPError('400 Bad Request','unknown_operation')
                    if 'options' in body and not isinstance(body['options'],dict):
                        raise HTTPError('400 Bad Request','invalid_options')
                    value = self.registry.submit(owner,workspace_id,body['operation'],body.get('options'))
                elif action == 'daily' and key is None:
                    if method in {'GET','HEAD'}:
                        value = self.registry.daily_status(owner,workspace_id)
                    else:
                        body = self._mutation(environ,owner)
                        if set(body)-{'enabled','at','timezone','options','run_now'}:
                            raise HTTPError('400 Bad Request','unknown_options')
                        value = self.registry.configure_daily(owner,workspace_id,**body)
                elif action == 'tasks' and method in {'GET','HEAD'} and key and re.fullmatch(r'job-[a-f0-9]{24}',key):
                    value = self.registry.task(owner,workspace_id,key)
                elif action == 'tasks' and method=='POST' and key and re.fullmatch(r'job-[a-f0-9]{24}',key):
                    body=self._mutation(environ,owner)
                    if body!={'action':'stop_queued'}:
                        raise HTTPError('400 Bad Request','unknown_options')
                    value=self.registry.stop_task(owner,workspace_id,key)
                elif action == 'exports' and method == 'POST' and key and re.fullmatch(r'[a-f0-9]{64}',key):
                    if self.registry.interface_mode != 'local':
                        raise HTTPError('400 Bad Request','request_unavailable')
                    if self._mutation(environ,owner) != {'action':'open'}:
                        raise HTTPError('400 Bad Request','unknown_options')
                    value = self.registry.open_file_on_device(owner,workspace_id,key)
                elif action == 'exports' and method in {'GET','HEAD'}:
                    if key is None:
                        value = {'exports':self.registry.exports(owner,workspace_id)}
                    elif re.fullmatch(r'[a-f0-9]{64}',key):
                        download = self.registry.open_export(owner,workspace_id,key)
                        media = {'cbz':'application/zip','tar.zst':'application/zstd','epub':'application/epub+zip','episode_metadata':'application/json','series_metadata':'application/json',
                                 'cover':'application/octet-stream'}[download.kind]
                        headers = self._headers(media) + [('Content-Length',str(download.size)),
                            ('Content-Disposition',"attachment; filename*=UTF-8''"+quote(download.filename,safe=''))]
                        start_response('200 OK',headers)
                        if method == 'HEAD':
                            download.close()
                            return []
                        return FileBody(download)
                    else:
                        raise HTTPError('404 Not Found','not_found')
                else:
                    raise HTTPError('405 Method Not Allowed','method_not_allowed')
            return self._json(start_response,'200 OK',value,method == 'HEAD')
        except HTTPError as error:
            return self._json(start_response,error.status,{'code':error.code},environ.get('REQUEST_METHOD')=='HEAD')
        except LibraryBusy:
            return self._json(start_response,'409 Conflict',{'code':'library_busy'},environ.get('REQUEST_METHOD')=='HEAD',extra=[('Retry-After','2')])
        except DeviceFileOpenUnavailable:
            return self._json(start_response,'400 Bad Request',{'code':'device_file_open_unavailable'})
        except ArchiveError:
            return self._json(start_response,'400 Bad Request',{'code':'request_unavailable'},environ.get('REQUEST_METHOD')=='HEAD')
        except Exception:
            # Exceptions can carry request bodies, account data and local paths.
            if download is not None:
                download.close()
            return self._json(start_response,'503 Service Unavailable',{'code':'service_unavailable'},environ.get('REQUEST_METHOD')=='HEAD')

    def _headers(self,media):
        return [('Content-Type',media),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
                ('Referrer-Policy','no-referrer'),('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'; base-uri 'none'")]

    def _json(self,start_response,status,value,head=False,extra=()):
        data = json.dumps(value,ensure_ascii=True,separators=(',',':')).encode()
        start_response(status,self._headers('application/json')+[('Content-Length',str(len(data))),*extra])
        return [] if head else [data]
