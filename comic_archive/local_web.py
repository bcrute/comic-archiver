"""The desktop app's dashboard: a loopback server for this computer's own window and its one library.

It reuses the current project and browser worker without moving archives or copying sessions.
"""
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
from socketserver import ThreadingMixIn
import subprocess
import threading
import time
from urllib.parse import urlsplit
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer, make_server

from .archive import episode_archives, sidecar_path
from .archive_paths import directory
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError
from .monitoring import library_status
from .osdep import NOFOLLOW, WINDOWS, detached
from .queue_client import submit_inspection, task_status
from .workspace import Workspace
from .errors import DeviceFileOpenUnavailable
from .web import HostedApplication


LOCAL_OWNER = 'trusted-local-device'
COOKIE_NAME = 'comic_archive_local'
SESSION_SECONDS = 12 * 3600
NEEDS_LAUNCH = ('This address alone does not open the dashboard. Open Comic Archiver itself, or the full '
                'dashboard address it showed when it started.\n').encode()
SAVED_FILES = re.compile(r'/api/v1/workspaces/[a-f0-9]{32}/saved-files')


class NoSignIn:
    """The site sign-in before the desktop window supplies one: idle, and unable to start."""
    def status(self):
        return {'status': 'idle'}

    def start(self):
        raise ArchiveError('Sign in from the desktop app.')

    def close(self):
        pass


class LocalWorkspaceRegistry:
    """Present one existing project as a workspace, with no data migration."""
    location = None  # The desktop app's library folder, and what the dashboard may ask the window to do with it.
    theme = None  # The desktop app's light/dark choice.
    sites = None  # The desktop app's list of sites.
    site_choice = None  # The site the dashboard has asked the desktop app's window to switch to.
    site_settings = None  # The desktop app's settings of each site it is signed in to.

    def __init__(self, project_root):
        self.project_root = Path(project_root).absolute()
        if not self.project_root.is_dir() or any(p.is_symlink() for p in (self.project_root, *self.project_root.parents)):
            raise ArchiveError('Use an existing project directory without symlinked parents.')
        from .state_paths import private_path
        self._key = _private_key(private_path(self.project_root, 'web'))
        from .saved_library import Catalog
        self.saved_catalog = Catalog(self._key)
        from .cloud_auth import SignIn
        self.cloud_signin = SignIn(self.project_root / 'Library')
        self.workspace_id = hmac.new(self._key, b'local-workspace', hashlib.sha256).hexdigest()[:32]
        workspace = Workspace(self.workspace_id, self.project_root)
        for path in (workspace.library, workspace.session):
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ArchiveError('Local library and session directories must not be symlinked.')
            if not path.exists():
                path.mkdir(mode=0o700)
            if not path.is_dir():
                raise ArchiveError('Local library and session storage is unavailable.')
        inventory_path = workspace.library / '.archive-state/inventory.sqlite3'
        # Opening the dashboard must not take the import writer lock or reset
        # unfinished runs when the existing worker is busy.
        with Inventory(workspace.library, read_only=inventory_path.is_file()):
            pass
        self.site_signin = NoSignIn()  # The desktop window puts its own in its place (app_runtime).
        # A run lasts for days and through closing the program. The page's clock counts the present stretch
        # of work: since this program opened, or since Go if that was later.
        self.working_since = int(time.time())

    def ids(self, owner):
        return [self.workspace_id] if owner == LOCAL_OWNER else []

    def cloud_status(self, owner, workspace_id):
        workspace = self.resolve(owner,workspace_id)
        from .cloud import status
        return status(workspace.library) | {'supported_on_this_interface':True}

    def configure_cloud(self, owner, workspace_id, provider, enabled):
        workspace = self.resolve(owner,workspace_id)
        from .cloud import enable
        return enable(workspace.library,provider,enabled)

    def choose_cloud_storage(self, owner, workspace_id, mode):
        workspace = self.resolve(owner,workspace_id)
        from .shared import choose_cloud
        choose_cloud(workspace.library, mode)
        return self.cloud_status(owner, workspace_id)

    def upload_cloud(self, owner, workspace_id):
        workspace = self.resolve(owner,workspace_id)
        return submit_inspection(workspace.session,'cloud_upload')

    def test_cloud(self, owner, workspace_id, provider):
        workspace = self.resolve(owner, workspace_id)
        return submit_inspection(workspace.session, 'cloud_test', {'provider': provider})

    def cleanup_cloud_test(self, owner, workspace_id, test_id):
        workspace = self.resolve(owner, workspace_id)
        from .cloud import status
        if test_id not in {record['test_id'] for record in status(workspace.library)['pending_test_cleanup']}:
            raise ArchiveError('Choose a pending test cleanup from this library.')
        return submit_inspection(workspace.session, 'cloud_cleanup', {'test_id': test_id})

    def network_status(self, owner, workspace_id):
        workspace = self.resolve(owner, workspace_id)
        from .network import status
        return status(workspace.library) | {'supported_on_this_interface': True}

    def configure_network(self, owner, workspace_id, wifi_only):
        workspace = self.resolve(owner, workspace_id)
        from .network import configure
        return configure(workspace.library, wifi_only) | {'supported_on_this_interface': True}

    def task(self,owner,workspace_id,task_id):
        workspace=self.resolve(owner,workspace_id)
        value=task_status(workspace.session,task_id)
        public={k:value[k] for k in ('task_id','status')}
        if value['status']=='error':
            code = value.get('error_code')
            from .cloud_errors import MESSAGES
            public['code']=code if isinstance(code, str) and code in {'login_required','source_refused', *MESSAGES} else 'task_failed'
            partial = value.get('result', {})
            if public['code'] == 'source_refused' and isinstance(partial, dict):
                status = partial.get('source_http_status')
                if type(status) is int and status in {403,429}:
                    public['source_http_status'] = status
                    from .retry_after import MAX_RETRY_AFTER
                    delay=partial.get('retry_after_seconds')
                    if type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER:
                        public['retry_after_seconds']=delay
        elif isinstance(value.get('result'),dict):
            result=value['result']
            # Public job metrics only: no worker diagnostics, paths or response bodies.
            public['result']={k:result[k] for k in ('downloaded_pages','downloaded_chapters','downloaded_covers','downloaded_bytes','pending_episode_count','metadata_unavailable_series_count','uploaded_episodes','local_archives_deleted','freed_bytes','removed_bytes','cache_files','verified_episodes')
                              if type(result.get(k)) is int and result[k]>=0}
            for key in ('test_files_deleted', 'local_archives_retained'):
                if type(result.get(key)) is bool:
                    public['result'][key] = result[key]
            for key in ('scan_elapsed_seconds','elapsed_seconds'):
                seconds=result.get(key)
                if type(seconds) in (int,float) and math.isfinite(seconds) and seconds>=0:
                    public['result'][key]=seconds
            refresh=result.get('refresh')
            if isinstance(refresh,dict):
                public['result']['refresh']={key:refresh[key] for key in ('series_checks_completed',
                    'successful_series_checks','held_series_checks','series_check_target_count','remaining_series_count')
                    if type(refresh.get(key)) is int and refresh[key]>=0}
            if result.get('status') in {'finished','limited','login_required','paused','interrupted','cancelled'}:
                public['result']['status']=result['status']
            if result.get('cancelled_before_start') is True:
                public['result']['cancelled_before_start']=True
            if result.get('status')=='paused' and result.get('pause_reason')=='network_unavailable':
                public['result']['pause_reason']='network_unavailable'
                status=result.get('source_http_status')
                if type(status) is int and status in {502,503,504}:
                    public['result']['source_http_status']=status
            if isinstance(result.get('source_series_id'),str) and re.fullmatch(r'[1-9][0-9]{0,19}',result['source_series_id']):
                public['result']['source_series_id']=result['source_series_id']
            ids=result.get('archived_episode_ids')
            if isinstance(ids,list) and all(isinstance(v,str) and re.fullmatch(r'[1-9][0-9]{0,19}',v) for v in ids):
                public['result']['archived_episode_ids']=ids
        return public

    def status(self,owner,workspace_id):
        workspace=self.resolve(owner,workspace_id)
        data=library_status(workspace.library,workspace.session)
        result={k:data[k] for k in ('inventory_complete','last_completed_scan','series_count','episode_count','episodes_by_status','unsupported_by_reason',
                                   'scan_remaining_series_count','series_without_scan_checkpoint_count',
                                   'series_without_episode_observations_count','series_scan_gap_count','series_scan_gaps','scan_cycle_started','scan_freshness','progress','worker','source_recovery','queue_priority','estimate')}
        result['workspace_id']=workspace.id
        state=workspace.session/'state.json'
        if state.is_symlink():
            raise ArchiveError('Workspace session storage is unavailable.')
        result['session_configured']=state.is_file()  # Presence is not authentication proof.
        last=data.get('last_run')
        result['last_run']={k:last[k] for k in ('id','started_at','finished_at','status')} if last else None
        return result

    def _exports(self,workspace):
        # A file that is a link, or gone since the folder was read, is left out; the rest of the library is still offered.
        result={}
        def safe(path):
            return path.is_file() and not any(p.is_symlink() for p in (path,*path.parents))
        def add(path,kind):
            if not safe(path):
                return
            relative=path.relative_to(workspace.library).as_posix()
            key=hmac.new(self._key,f'export\0{workspace.id}\0{relative}'.encode(),hashlib.sha256).hexdigest()
            result[key]=(path,kind)
        folders=sorted({p.parent for ext in ('.cbz', '.epub', '.tar.zst') for p in workspace.library.glob('*/*'+ext) if not p.parent.name.startswith('.')})
        for folder in folders:
            series=folder/'series.json'
            if folder.is_symlink():
                continue
            # An episode is offered with its details or not at all.
            episodes=[p for p in episode_archives(folder) if safe(p) and safe(sidecar_path(p))]
            if not episodes:
                continue
            if safe(series):
                add(series,'series_metadata')
                cover=None
                try:
                    if series.stat().st_size<=256*1024:
                        cover=json.loads(series.read_text()).get('cover_file')
                except (ValueError,AttributeError):
                    pass  # Optional metadata repair must not hide valid CBZ pairs.
                if isinstance(cover,str) and cover in {'cover.jpg','cover.png','cover.webp','cover.gif'} and (folder/cover).is_file():
                    add(folder/cover,'cover')
            for archive in episodes:
                add(archive,'tar.zst' if archive.name.endswith('.tar.zst') else archive.suffix[1:])
                add(sidecar_path(archive),'episode_metadata')
        return result

    def saved_files(self, owner, workspace_id, **options):
        workspace = self.resolve(owner, workspace_id)
        return self.saved_catalog.page(workspace, True, **options)

    def site_signin_status(self, owner, workspace_id):
        self.resolve(owner, workspace_id)
        return self.site_signin.status()

    def start_site_signin(self, owner, workspace_id):
        from .errors import LibraryBusy
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            info = self.status(owner, workspace_id)
            if self.account_status(owner, workspace_id).get('status') == 'running' or (info['worker'] or {}).get('reported_state') == 'busy':
                raise LibraryBusy('Pause the download before reconnecting Tapas.')
            return self.site_signin.start()

    def account_status(self, owner, workspace_id):
        from .account_ui import status
        return status(self.resolve(owner, workspace_id), since=self.working_since)

    def start_account(self, owner, workspace_id):
        from .errors import LibraryBusy
        from .account_ui import start
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            if self.site_signin.status()['status'] == 'running' or self.cloud_signin.status()['status'] == 'running':
                raise LibraryBusy('Complete browser sign-in before downloading.')
            if self.account_status(owner, workspace_id).get('status') == 'running':
                return self.account_status(owner, workspace_id)
            info = self.status(owner, workspace_id)
            if not info['session_configured'] or (info['worker'] or {}).get('reported_state') != 'ready':
                raise ArchiveError('Connect Tapas and start the downloader before downloading.')
            self._prepare_desktop_download(owner, workspace_id)
            self.working_since = int(time.time())
            start(workspace)
            return self.account_status(owner, workspace_id)

    def choose_speed(self, owner, workspace_id, value):
        from .errors import LibraryBusy
        from .account_ui import choose_speed
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            # Go applies the choice, so what the page shows while a download runs is the pace it runs at.
            if self.account_status(owner, workspace_id).get('status') == 'running':
                raise LibraryBusy('Pause the download before changing its speed.')
            choose_speed(workspace, value)
            return self.account_status(owner, workspace_id)

    def pause_account(self, owner, workspace_id):
        from .account_ui import pause
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            pause(workspace)
            return self.account_status(owner, workspace_id)

    def cloud_signin_status(self, owner, workspace_id):
        self.resolve(owner, workspace_id)
        return self.cloud_signin.status()

    def start_cloud_signin(self, owner, workspace_id, provider, credentials):
        self.resolve(owner, workspace_id)
        return self.cloud_signin.start(provider, credentials)

    def cancel_cloud_signin(self, owner, workspace_id):
        self.resolve(owner, workspace_id)
        return self.cloud_signin.cancel()

    def open_file_on_device(self, owner, workspace_id, export_id):
        workspace = self.resolve(owner, workspace_id)
        if not isinstance(export_id, str) or not re.fullmatch(r'[a-f0-9]{64}', export_id):
            raise ArchiveError('Saved file is unavailable.')
        selected = self._exports(workspace).get(export_id)
        if selected is None:
            raise ArchiveError('Saved file is unavailable.')
        # Only registered portable exports reach the fixed desktop opener.
        # There is no archive extraction, byte transfer or temporary copy.
        path, _ = selected
        try:
            if WINDOWS:
                os.startfile(path)  # Windows takes the file and reports nothing more about the application.
                return {'status': 'open_requested'}
            process = subprocess.Popen(['xdg-open', str(path)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())
            try:
                result = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Some default applications remain attached to the launcher.
                # Keep them open and reap the child when the user closes them.
                threading.Thread(target=process.wait, daemon=True).start()
            else:
                if result != 0:
                    raise DeviceFileOpenUnavailable('Device file opener is unavailable.')
        except OSError:
            raise DeviceFileOpenUnavailable('Device file opener is unavailable.') from None
        return {'status': 'open_requested'}

    def _prepare_desktop_download(self, owner, workspace_id):
        # Preserve the user's explicit cloud choice across Go, previews and
        # scheduled runs. Cloud offload still requires a successful pilot test.
        from .locking import LibraryLock
        with LibraryLock(self.resolve(owner, workspace_id).library):
            pass

    def resolve(self, owner, workspace_id):
        if owner != LOCAL_OWNER or workspace_id != self.workspace_id:
            raise ArchiveError('Workspace is unavailable for this owner.')
        workspace = Workspace(self.workspace_id, self.project_root)
        for path in (workspace.root, workspace.library, workspace.session):
            if not path.is_dir() or any(p.is_symlink() for p in (path, *path.parents)):
                raise ArchiveError('Workspace storage is unavailable; existing files are retained.')
        return workspace


def _private_key(root):
    """The secret behind this library's cookies and file links, made on first use and kept private."""
    root = directory(root)
    root.chmod(0o700)
    path = root / 'registry.key'
    if path.is_symlink():
        raise ArchiveError('Refusing a symlinked dashboard key.')
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32)); stream.flush(); os.fsync(stream.fileno())
    with os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW), 'rb') as stream:
        key = stream.read(33)
    if len(key) != 32:
        raise ArchiveError('Dashboard key is invalid; existing files are retained.')
    path.chmod(0o600)
    return key


class LocalApplication:
    """Authenticate same-origin loopback browsing with a private signed cookie.

    Opening the launch address, which holds this server's own secret, creates
    the website session; the port alone is reachable by every program and user
    on the computer. Tapas credentials remain in the existing worker, never in
    this cookie. Host/peer/fetch checks prevent remote access and unsolicited
    cross-site bootstrapping; a user-activated top-level navigation may open
    the launch address.
    """
    def __init__(self, registry, port, cookie_key=None, launch_token=None):
        if not isinstance(registry, LocalWorkspaceRegistry) or type(port) is not int or not 1 <= port <= 65535:
            raise ArchiveError('Local dashboard requires a local registry and valid port.')
        if launch_token is not None and not (isinstance(launch_token, str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', launch_token)):
            raise ArchiveError('Local dashboard requires a valid launch secret.')
        self.registry = registry
        # A page opened before the app switched to another site's library keeps its cookie and launch address.
        self.cookie_key = registry._key if cookie_key is None else cookie_key
        self.launch_token = secrets.token_urlsafe(32) if launch_token is None else launch_token
        # Named for the port, so dashboards on other ports of this computer keep their own.
        self.cookie_name = f'{COOKIE_NAME}-{port}'
        self._dispatch_lock = threading.RLock()
        self.host = f'127.0.0.1:{port}'
        self.origin = f'http://{self.host}'
        self.launch_url = f'{self.origin}/?launch={self.launch_token}'
        self.app = HostedApplication(registry, lambda env: env.get('comic.local.principal'), self.origin)

    def _sign(self, payload):
        return hmac.new(self.cookie_key, b'local-browser-session\0' + payload.encode('ascii'), hashlib.sha256).hexdigest()

    def _cookie(self, environ):
        """The age in seconds of a valid session cookie, else None."""
        raw = environ.get('HTTP_COOKIE', '')
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return None
        item = cookie.get(self.cookie_name)
        token = item.value if item else ''
        if not re.fullmatch(r'[0-9]{1,12}\.[a-f0-9]{32}\.[a-f0-9]{64}', token):
            return None
        stamp, nonce, signature = token.split('.')
        age = time.time() - int(stamp)
        return age if 0 <= age <= SESSION_SECONDS and hmac.compare_digest(signature, self._sign(stamp + '.' + nonce)) else None

    def __call__(self, environ, start_response):
        # Never accept proxy headers as proof of a local peer or website owner.
        if (environ.get('REMOTE_ADDR') != '127.0.0.1' or environ.get('HTTP_HOST') != self.host
                or environ.get('wsgi.url_scheme') != 'http'):
            return self.app._json(start_response, '403 Forbidden', {'code': 'loopback_required'}, environ.get('REQUEST_METHOD') == 'HEAD')
        fetch_site = environ.get('HTTP_SEC_FETCH_SITE', '')
        origin = environ.get('HTTP_ORIGIN', '')
        referer = environ.get('HTTP_REFERER', '')
        try:
            ref = urlsplit(referer) if referer else None
            same_ref = ref is None or (ref.scheme + '://' + ref.netloc == self.origin)
        except (TypeError, ValueError):
            same_ref = False
        method = environ.get('REQUEST_METHOD')
        query = environ.get('QUERY_STRING') or ''
        root_page = environ.get('PATH_INFO') in {'/', '/library'} and method in {'GET', 'HEAD'}
        # Only the launch address opens a session; constant-time, so the secret cannot be guessed piece by piece.
        launch = root_page and hmac.compare_digest(query.encode('latin-1', 'replace'), b'launch=' + self.launch_token.encode())
        # A user may click the launch link from another website.
        # Only a user-activated top-level document navigation may cross sites;
        # API calls, POSTs, fetches and frames still require the local origin.
        user_navigation = (launch and method == 'GET'
                           and environ.get('HTTP_SEC_FETCH_MODE') == 'navigate'
                           and environ.get('HTTP_SEC_FETCH_DEST') == 'document'
                           and environ.get('HTTP_SEC_FETCH_USER') == '?1')
        if (origin not in {'', self.origin} or fetch_site not in {'', 'none', 'same-origin', 'cross-site', 'same-site'}
                or ((fetch_site not in {'', 'none', 'same-origin'} or not same_ref) and not user_navigation)):
            return self.app._json(start_response, '403 Forbidden', {'code': 'same_origin_required'}, method == 'HEAD')
        age = self._cookie(environ)
        if age is None and not launch:
            if root_page and not query:
                # Someone opened the plain address: say where the dashboard opens instead.
                data = b'' if method == 'HEAD' else NEEDS_LAUNCH
                start_response('401 Unauthorized', self.app._headers('text/plain; charset=utf-8') + [('Content-Length', str(len(NEEDS_LAUNCH)))])
                return [data]
            return self.app._json(start_response, '401 Unauthorized', {'code': 'local_session_required'}, method == 'HEAD')
        trusted = dict(environ)
        trusted['comic.local.principal'] = LOCAL_OWNER
        if launch:
            trusted['QUERY_STRING'] = ''
        # A page in use keeps its session: past half its life the cookie is issued again.
        issue = launch or age > SESSION_SECONDS / 2

        def respond(status, headers, exc_info=None):
            if issue and status.startswith('200 '):
                payload = str(int(time.time())) + '.' + secrets.token_hex(16)
                value = payload + '.' + self._sign(payload)
                headers = [*headers, ('Set-Cookie', f'{self.cookie_name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}')]
            return start_response(status, headers, exc_info)

        if not launch and method in {'GET', 'HEAD'} and SAVED_FILES.fullmatch(environ.get('PATH_INFO') or ''):
            # Read-only, behind the catalog's own lock: a slow library scan does not hold up Pause or Go.
            return self.app(trusted, respond)
        with self._dispatch_lock:
            return self.app(trusted, respond)


class LocalServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def server_close(self):
        app = getattr(self, 'application', None)
        if app is not None:
            app.registry.cloud_signin.close()
            app.registry.site_signin.close()
        super().server_close()


class PrivateServerHandler(ServerHandler):
    server_software = 'ComicArchiver'


class PrivateRequestHandler(WSGIRequestHandler):
    # The Server header names no Python version, and a connection that sends nothing is let go.
    server_version, sys_version = 'ComicArchiver', ''
    timeout = 60

    def handle(self):
        # The standard library's own steps, with a response handler that does not name its Python either.
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = self.request_version = self.command = ''
            self.send_error(414)
            return
        if not self.parse_request():
            return
        handler = PrivateServerHandler(self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=False)
        handler.request_handler = self
        handler.run(self.server.get_app())

    def log_message(self, format, *args):
        if format == '%s %s' and len(args) == 2:
            super().log_message(format, *args)

    def log_request(self, code='-', size='-'):
        # No paths, query strings, headers, cookies, bodies or exception text.
        method = self.command if self.command in {'GET', 'HEAD', 'POST'} else 'OTHER'
        status = str(code) if re.fullmatch(r'[0-9]{3}', str(code)) else 'unknown'
        self.log_message('%s %s', method, status)

    def get_stderr(self):
        return io.StringIO()


def create_server(project_root, port=0):
    """On port zero the system picks a free port."""
    registry = LocalWorkspaceRegistry(project_root)
    try:
        server = make_server('127.0.0.1', port, None, server_class=LocalServer, handler_class=PrivateRequestHandler)
    except OSError:
        raise ArchiveError('Cannot bind the local dashboard.') from None
    try:
        server.set_app(LocalApplication(registry, server.server_port))
    except Exception:
        server.server_close()
        raise
    return server

