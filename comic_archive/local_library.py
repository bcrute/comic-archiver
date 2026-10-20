"""The desktop window's one library: the open site's folder, as the window reads and changes it.

It reuses the current project and browser worker without moving archives or copying sessions.
"""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

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


LOCAL_OWNER = 'trusted-local-device'


class NoSignIn:
    """The site sign-in before the desktop window supplies one: idle, and unable to start."""
    def status(self):
        return {'status': 'idle'}

    def start(self):
        raise ArchiveError('Sign in from the desktop app.')

    def sign_out(self):
        raise ArchiveError('Sign out from the desktop app.')

    def close(self):
        pass


class LocalWorkspaceRegistry:
    """Present one existing project as a workspace, with no data migration."""

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
        # Opening the library must not take the import writer lock or reset
        # unfinished runs when the existing worker is busy.
        with Inventory(workspace.library, read_only=inventory_path.is_file()):
            pass
        self.site_signin = NoSignIn()  # The desktop window puts its own in its place (app_runtime).
        # A run lasts for days and through closing the program. The window's clock counts the present stretch
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

    def sign_out_site(self, owner, workspace_id):
        from .errors import LibraryBusy
        workspace = self.resolve(owner, workspace_id)
        with LibraryLock(workspace.session / '.account-ui'):
            info = self.status(owner, workspace_id)
            if self.account_status(owner, workspace_id).get('status') == 'running' or (info['worker'] or {}).get('reported_state') == 'busy':
                raise LibraryBusy('Pause the download before signing out.')
            return self.site_signin.sign_out()

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
            # Go applies the choice, so what the window shows while a download runs is the pace it runs at.
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
    """The secret behind this library's name and file links, made on first use and kept private."""
    root = directory(root)
    root.chmod(0o700)
    path = root / 'registry.key'
    if path.is_symlink():
        raise ArchiveError('Refusing a symlinked library key.')
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
        raise ArchiveError('Library key is invalid; existing files are retained.')
    path.chmod(0o600)
    return key
