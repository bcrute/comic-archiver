"""Loopback dashboard for a trusted local device and its existing CLI library.

This is deliberately separate from hosted website authentication. It reuses the
current project and browser worker without moving archives or copying sessions.
"""
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import io
import os
from pathlib import Path
import re
import secrets
from socketserver import ThreadingMixIn
import subprocess
import threading
import time
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .hosted import HostedJobLimits, Workspace, WorkspaceRegistry
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError
from .osdep import WINDOWS, detached
from .errors import DeviceFileOpenUnavailable
from .web import HostedApplication


LOCAL_OWNER = 'trusted-local-device'
COOKIE_NAME = 'comic_archive_local'
SESSION_SECONDS = 12 * 3600


class LocalWorkspaceRegistry(WorkspaceRegistry):
    """Present one existing project as a workspace, with no data migration."""
    interface_mode = 'local'

    def __init__(self, project_root):
        self.project_root = Path(project_root).absolute()
        if not self.project_root.is_dir() or any(p.is_symlink() for p in (self.project_root, *self.project_root.parents)):
            raise ArchiveError('Use an existing project directory without symlinked parents.')
        from .state_paths import private_path
        super().__init__(private_path(self.project_root, 'web'), limits=HostedJobLimits(max_bytes=8 * 1024**3))
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
        from .tapas_auth import SignIn as TapasSignIn
        self.tapas_signin = TapasSignIn(workspace)
        # A run lasts for days and through closing the program. The page's clock counts the present stretch
        # of work: since this program opened, or since Go if that was later.
        self.working_since = int(time.time())

    def ids(self, owner):
        return [self.workspace_id] if owner == LOCAL_OWNER else []

    def tapas_signin_status(self, owner, workspace_id):
        self.resolve(owner, workspace_id)
        return self.tapas_signin.status()

    def start_tapas_signin(self, owner, workspace_id):
        from .errors import LibraryBusy
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            info = self.status(owner, workspace_id)
            if self.account_status(owner, workspace_id).get('status') == 'running' or (info['worker'] or {}).get('reported_state') == 'busy':
                raise LibraryBusy('Pause the download before reconnecting Tapas.')
            return self.tapas_signin.start()

    def account_status(self, owner, workspace_id):
        from .account_ui import status
        return status(self.resolve(owner, workspace_id), since=self.working_since)

    def start_account(self, owner, workspace_id):
        from .errors import LibraryBusy
        from .account_ui import start
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            if self.tapas_signin.status()['status'] == 'running' or self.cloud_signin.status()['status'] == 'running':
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

    def submit(self, owner, workspace_id, operation, options=None):
        options = dict(options or {})
        if options.get('speed', 'normal') == 'normal' and options.get('max_bytes') is None:
            options['max_bytes'] = 256 * 1024**2
        if self.desktop_only and operation in {'archive_discovered', 'sync', 'archive_watched_series', 'archive_series_url'}:
            self._prepare_desktop_download(owner, workspace_id)
        return super().submit(owner, workspace_id, operation, options)

    def _prepare_desktop_download(self, owner, workspace_id):
        # Preserve the user's explicit cloud choice across Go, previews and
        # scheduled runs. Cloud offload still requires a successful pilot test.
        from .locking import LibraryLock
        with LibraryLock(self.resolve(owner, workspace_id).library):
            pass

    def preview_series_storage(self, owner, workspace_id, url, scope):
        from .preflight import validate_options
        validate_options({'url':url, 'scope':scope})
        if self.desktop_only:
            self._prepare_desktop_download(owner, workspace_id)
        return super().preview_series_storage(owner, workspace_id, url, scope)

    def configure_daily(self, owner, workspace_id, **options):
        if self.desktop_only and options.get('enabled', True):
            self._prepare_desktop_download(owner, workspace_id)
        return super().configure_daily(owner, workspace_id, **options)

    def create(self, owner, max_per_owner=None):
        raise ArchiveError('Local mode uses the existing project library.')

    def resolve(self, owner, workspace_id):
        if owner != LOCAL_OWNER or workspace_id != self.workspace_id:
            raise ArchiveError('Workspace is unavailable for this owner.')
        workspace = Workspace(self.workspace_id, self.project_root)
        for path in (workspace.root, workspace.library, workspace.session):
            if not path.is_dir() or any(p.is_symlink() for p in (path, *path.parents)):
                raise ArchiveError('Workspace storage is unavailable; existing files are retained.')
        return workspace


class LocalApplication:
    """Authenticate same-origin loopback browsing with a private signed cookie.

    Opening the root page on this trusted device creates the website session.
    Tapas credentials remain in the existing worker, never in this cookie.
    Host/peer/fetch checks prevent remote access and unsolicited cross-site
    bootstrapping; a user-activated top-level navigation may open the dashboard.
    """
    def __init__(self, registry, port, cookie_key=None):
        if not isinstance(registry, LocalWorkspaceRegistry) or type(port) is not int or not 1 <= port <= 65535:
            raise ArchiveError('Local dashboard requires a local registry and valid port.')
        self.registry = registry
        # A page opened before the app switched to another site's library keeps its cookie.
        self.cookie_key = registry._key if cookie_key is None else cookie_key
        self._dispatch_lock = threading.RLock()
        self.host = f'127.0.0.1:{port}'
        self.origin = f'http://{self.host}'
        self.app = HostedApplication(registry, lambda env: env.get('tapas.local.principal'), self.origin, max_workspaces=1)

    def _sign(self, payload):
        return hmac.new(self.cookie_key, b'local-browser-session\0' + payload.encode('ascii'), hashlib.sha256).hexdigest()

    def _cookie(self, environ):
        raw = environ.get('HTTP_COOKIE', '')
        if not isinstance(raw, str) or len(raw) > 4096:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return False
        item = cookie.get(COOKIE_NAME)
        token = item.value if item else ''
        if not re.fullmatch(r'[0-9]{1,12}\.[a-f0-9]{32}\.[a-f0-9]{64}', token):
            return False
        stamp, nonce, signature = token.split('.')
        return (0 <= time.time() - int(stamp) <= SESSION_SECONDS
                and hmac.compare_digest(signature, self._sign(stamp + '.' + nonce)))

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
        root_page = (environ.get('PATH_INFO') in {'/', '/library'} and environ.get('REQUEST_METHOD') in {'GET', 'HEAD'}
                     and not environ.get('QUERY_STRING'))
        # A user may click the displayed dashboard link from another website.
        # Only a user-activated top-level document navigation may cross sites;
        # API calls, POSTs, fetches and frames still require the local origin.
        user_navigation = (root_page and environ.get('REQUEST_METHOD') == 'GET'
                           and environ.get('HTTP_SEC_FETCH_MODE') == 'navigate'
                           and environ.get('HTTP_SEC_FETCH_DEST') == 'document'
                           and environ.get('HTTP_SEC_FETCH_USER') == '?1')
        if (origin not in {'', self.origin} or fetch_site not in {'', 'none', 'same-origin', 'cross-site', 'same-site'}
                or ((fetch_site not in {'', 'none', 'same-origin'} or not same_ref) and not user_navigation)):
            return self.app._json(start_response, '403 Forbidden', {'code': 'same_origin_required'}, environ.get('REQUEST_METHOD') == 'HEAD')
        authenticated = self._cookie(environ)
        bootstrap = not authenticated and root_page
        if not bootstrap and not authenticated:
            return self.app._json(start_response, '401 Unauthorized', {'code': 'local_session_required'}, environ.get('REQUEST_METHOD') == 'HEAD')
        trusted = dict(environ)
        trusted['tapas.local.principal'] = LOCAL_OWNER

        def respond(status, headers, exc_info=None):
            if bootstrap and status.startswith('200 '):
                payload = str(int(time.time())) + '.' + secrets.token_hex(16)
                value = payload + '.' + self._sign(payload)
                headers = [*headers, ('Set-Cookie', f'{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}')]
            return start_response(status, headers, exc_info)

        with self._dispatch_lock:
            return self.app(trusted, respond)


class LocalServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def server_close(self):
        app = getattr(self, 'application', None)
        if app is not None:
            app.registry.cloud_signin.close()
            app.registry.tapas_signin.close()
        super().server_close()


class PrivateRequestHandler(WSGIRequestHandler):
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


def create_server(project_root, port=8765):
    """Port zero is available to integration tests; the CLI uses a fixed port."""
    registry = LocalWorkspaceRegistry(project_root)
    try:
        server = make_server('127.0.0.1', port, None, server_class=LocalServer, handler_class=PrivateRequestHandler)
    except OSError:
        raise ArchiveError('Cannot bind the local dashboard. Choose an unused local port, or start it in your normal terminal.') from None
    try:
        server.set_app(LocalApplication(registry, server.server_port))
    except Exception:
        server.server_close()
        raise
    return server


def serve(project_root, port=8765):
    with create_server(project_root, port) as server:
        print(f'Local dashboard: http://127.0.0.1:{server.server_port}/', flush=True)
        print('Uses this project’s Library and existing browser worker. Ctrl+C stops the dashboard.', flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print('Local dashboard stopped.', flush=True)
