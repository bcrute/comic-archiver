"""Owner-scoped workspace operations for the eventual hosted interface.

The caller must obtain owner identity from website authentication, never a
request parameter. This module does not implement website login or remote
Tapas login. Worker processes use server-chosen working directories.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys

from .archive import verify_episode_contents, verify_series_contents, episode_archives, sidecar_path, archive_extension, archive_from_sidecar
from .archive_paths import directory
from .inventory import Inventory
from . import sites
from .models import ArchiveError
from .monitoring import library_status
from .osdep import NOFOLLOW, NONBLOCK
from .queue_client import submit_job,submit_inspection,task_status


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path = field(repr=False)

    @property
    def library(self):
        return self.root/'Library'

    @property
    def session(self):
        from .state_paths import session_path
        return session_path(self.root)


@dataclass
class ExportDownload:
    filename: str
    size: int
    kind: str
    stream: object = field(repr=False)

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()


@dataclass(frozen=True)
class HostedJobLimits:
    limit: int=50
    max_pages: int=1000
    max_bytes: int=256*1024**2
    max_seconds: float=900

    def __post_init__(self):
        if any(type(v) is not int or v<1 for v in (self.limit,self.max_pages,self.max_bytes)) or type(self.max_seconds) not in (int,float) or not math.isfinite(self.max_seconds) or self.max_seconds<=0:
            raise ArchiveError('Hosted job limits must be finite positive budgets.')


class WorkspaceRegistry:
    interface_mode = 'hosted'
    desktop_only = True
    simple_mode = True
    location = None  # The desktop app's library folder, and what the dashboard may ask the window to do with it.
    theme = None  # The desktop app's light/dark choice; a hosted site follows each visitor's own browser.
    sites = None  # The desktop app's list of sites; a hosted site has none.
    site_choice = None  # The site the dashboard has asked the desktop app's window to switch to.

    def __init__(self,root,limits=None):
        self.limits=HostedJobLimits() if limits is None else limits
        if not isinstance(self.limits,HostedJobLimits):
            raise ArchiveError('Hosted job limits must be configured by the server.')
        self.root=directory(Path(root).absolute())
        self.root.chmod(0o700)
        self.workspaces=directory(self.root/'workspaces')
        self.workspaces.chmod(0o700)
        self.database=self.root/'registry.sqlite3'
        secret_path=self.root/'registry.key'
        if secret_path.is_symlink() or self.database.is_symlink():
            raise ArchiveError('Refusing a symlinked hosted registry.')
        try:
            fd=os.open(secret_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|NOFOLLOW,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd,'wb') as stream:
                stream.write(secrets.token_bytes(32));stream.flush();os.fsync(stream.fileno())
        fd=os.open(secret_path,os.O_RDONLY|NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            self._key=stream.read(33)
        if len(self._key)!=32:
            raise ArchiveError('Hosted registry key is invalid; existing workspaces are retained.')
        from .saved_library import Catalog
        self.saved_catalog = Catalog(self._key)
        secret_path.chmod(0o600)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY,owner_key TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS owner_workspaces ON workspaces(owner_key)')

    @contextmanager
    def _connection(self):
        if any(p.is_symlink() for p in (self.database,*self.database.parents)):
            raise ArchiveError('Refusing a symlinked hosted registry.')
        db=sqlite3.connect(self.database,timeout=5)
        self.database.chmod(0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _owner_key(self,owner):
        if not isinstance(owner,str) or not owner or len(owner.encode('utf-8'))>256:
            raise ArchiveError('A verified website owner identity is required.')
        return hmac.new(self._key,b'owner\0'+owner.encode('utf-8'),hashlib.sha256).hexdigest()

    def create(self,owner,max_per_owner=None):
        owner_key=self._owner_key(owner)
        if max_per_owner is not None and (type(max_per_owner) is not int or not 1<=max_per_owner<=100):
            raise ArchiveError('Workspace quota must be a server-configured positive integer.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('SELECT COUNT(*) FROM workspaces WHERE owner_key=?',(owner_key,)).fetchone()[0]
            if max_per_owner is not None and count>=max_per_owner:
                raise ArchiveError('The workspace limit for this owner was reached.')
            workspace_id=secrets.token_hex(16)
            root=self.workspaces/workspace_id
            root.mkdir(mode=0o700)
            workspace=Workspace(workspace_id,root)
            directory(workspace.session).chmod(0o700)
            directory(workspace.library).chmod(0o700)
            with Inventory(workspace.library):
                pass
            db.execute('INSERT INTO workspaces VALUES (?,?)',(workspace_id,owner_key))
        return workspace

    def ids(self,owner):
        with self._connection() as db:
            return [r[0] for r in db.execute('SELECT id FROM workspaces WHERE owner_key=? ORDER BY id',(self._owner_key(owner),))]

    def resolve(self,owner,workspace_id):
        if not isinstance(workspace_id,str) or not re.fullmatch(r'[a-f0-9]{32}',workspace_id):
            raise ArchiveError('Workspace is unavailable for this owner.')
        with self._connection() as db:
            row=db.execute('SELECT owner_key FROM workspaces WHERE id=?',(workspace_id,)).fetchone()
        if row is None or not hmac.compare_digest(row[0],self._owner_key(owner)):
            raise ArchiveError('Workspace is unavailable for this owner.')
        workspace=Workspace(workspace_id,self.workspaces/workspace_id)
        for path in (workspace.root,workspace.library,workspace.session):
            if not path.is_dir() or any(p.is_symlink() for p in (path,*path.parents)):
                raise ArchiveError('Workspace storage is unavailable; existing files are retained.')
        return workspace

    def submit(self,owner,workspace_id,operation,options=None):
        workspace=self.resolve(owner,workspace_id)
        if operation in {'archive_discovered','sync','archive_watched_series','archive_series_url'}:
            options=dict(options or {})
            for key in ('limit','max_pages','max_bytes','max_seconds'):
                maximum=getattr(self.limits,key)
                value=options.get(key)
                if value is None:
                    options[key]=maximum
                elif type(value) not in ((int,float) if key=='max_seconds' else (int,)) or not 0<value<=maximum:
                    raise ArchiveError('Requested job budget exceeds or violates the server limits.')
            if operation=='archive_watched_series':
                selected=options.get('id')
                with Inventory(workspace.library,read_only=True) as inventory:
                    if not isinstance(selected,str) or not any(series['id']==selected for series in inventory.watched_series()):
                        raise ArchiveError('Select a series already observed in this workspace.')
        # The fixed client validates operation/option names and never accepts paths.
        return submit_job(workspace.session,operation,options)

    def series(self,owner,workspace_id):
        workspace=self.resolve(owner,workspace_id)
        with Inventory(workspace.library,read_only=True) as inventory:
            counts={}
            access_counts={}
            for series_id,status,count in inventory.db.execute('SELECT series_id,status,COUNT(*) FROM episodes GROUP BY series_id,status'):
                if status in {'archived','queued','failed','unavailable','unsupported','downloading'}:
                    counts.setdefault(series_id,{})[status]=count
            for series_id,access,count in inventory.db.execute('''SELECT series_id,access_state,COUNT(*) FROM episodes
                    WHERE access_state IN '''+sites.sql_list(sites.ACCESS)+''' GROUP BY series_id,access_state'''):
                kind='free' if access in sites.FREE else 'unlocked'
                access_counts.setdefault(series_id,{})[kind]=access_counts.get(series_id,{}).get(kind,0)+count
            return [{'source_series_id':series['id'],'title':series['title'][:200],
                     'observed_episode_count':sum(counts.get(series['id'],{}).values()),
                     'observed_free_episode_count':access_counts.get(series['id'],{}).get('free',0),
                     'observed_unlocked_episode_count':access_counts.get(series['id'],{}).get('unlocked',0),
                     'episodes_by_status':counts.get(series['id'],{})}
                    for series in sorted(inventory.watched_series(),key=lambda row:(row['title'].casefold(),row['id']))]

    def audit(self,owner,workspace_id):
        from .audit import audit_library
        return audit_library(self.resolve(owner,workspace_id).library)

    def storage(self, owner, workspace_id):
        from .storage import storage_plan
        value = storage_plan(self.resolve(owner,workspace_id).library, local_only=self.desktop_only and self.interface_mode != 'local')
        value['storage_location'] = 'this_device' if self.interface_mode == 'local' else 'server'
        return value

    def configure_storage(self, owner, workspace_id, image_profile):
        from .storage import configure
        return configure(self.resolve(owner,workspace_id).library, image_profile=image_profile)

    def preview_series_storage(self, owner, workspace_id, url, scope):
        workspace = self.resolve(owner, workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Check device space through the local archiver.')
        return submit_inspection(workspace.session, 'preview_series', {'url': url, 'scope': scope})

    def purge_cache(self, owner, workspace_id):
        return submit_inspection(self.resolve(owner,workspace_id).session, 'purge_cache')

    def cloud_status(self, owner, workspace_id):
        workspace = self.resolve(owner,workspace_id)
        if self.interface_mode != 'local':
            return {'supported_on_this_interface':False}
        from .cloud import status
        return status(workspace.library) | {'supported_on_this_interface':True}

    def configure_cloud(self, owner, workspace_id, provider, enabled):
        workspace = self.resolve(owner,workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Cloud uploads run on the user device through the local archiver.')
        from .cloud import enable
        return enable(workspace.library,provider,enabled)

    def upload_cloud(self, owner, workspace_id):
        workspace = self.resolve(owner,workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Cloud uploads run on the user device through the local archiver.')
        return submit_inspection(workspace.session,'cloud_upload')

    def test_cloud(self, owner, workspace_id, provider):
        workspace = self.resolve(owner, workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Cloud tests run on your device through the local archiver.')
        return submit_inspection(workspace.session, 'cloud_test', {'provider': provider})

    def cleanup_cloud_test(self, owner, workspace_id, test_id):
        workspace = self.resolve(owner, workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Cloud test cleanup runs on your device through the local archiver.')
        from .cloud import status
        if test_id not in {record['test_id'] for record in status(workspace.library)['pending_test_cleanup']}:
            raise ArchiveError('Choose a pending test cleanup from this library.')
        return submit_inspection(workspace.session, 'cloud_cleanup', {'test_id': test_id})

    def network_status(self, owner, workspace_id):
        workspace = self.resolve(owner, workspace_id)
        if self.interface_mode != 'local':
            return {'supported_on_this_interface': False}
        from .network import status
        return status(workspace.library) | {'supported_on_this_interface': True}

    def configure_network(self, owner, workspace_id, wifi_only):
        workspace = self.resolve(owner, workspace_id)
        if self.interface_mode != 'local':
            raise ArchiveError('Network choices belong to the archiver on your device.')
        from .network import configure
        return configure(workspace.library, wifi_only) | {'supported_on_this_interface': True}

    def stop_run(self,owner,workspace_id,run_id):
        from .cancellation import request_stop
        return request_stop(self.resolve(owner,workspace_id).library,run_id)

    def stop_task(self,owner,workspace_id,task_id):
        from .task_cancellation import request_queued_stop
        return request_queued_stop(self.resolve(owner,workspace_id).session,task_id)

    def task(self,owner,workspace_id,task_id):
        workspace=self.resolve(owner,workspace_id)
        value=task_status(workspace.session,task_id)
        public={k:value[k] for k in ('task_id','status')}
        if value['status']=='queued':
            from .task_cancellation import can_stop_queued, queued_stop_requested
            public['cancellation_supported']=can_stop_queued(workspace.session,task_id)
            if public['cancellation_supported']:
                public['cancellation_requested']=queued_stop_requested(workspace.session,task_id)
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
            if isinstance(result.get('storage_preview'), dict):
                from .preflight import public_preview
                public['result'] = {'storage_preview':public_preview(result['storage_preview'])}
                return public
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
            for key in ('archived_episode_ids','enriched_series_ids'):
                if isinstance(result.get(key),list) and all(isinstance(v,str) and re.fullmatch(r'[1-9][0-9]{0,19}',v) for v in result[key]):
                    public['result'][key]=result[key]
            if type(result.get('cover_archived')) is bool:
                public['result']['cover_archived']=result['cover_archived']
            if type(result.get('authenticated')) is bool:
                public['result']['authenticated']=result['authenticated']
            if isinstance(result.get('account_binding'),str) and result['account_binding'] in {'matches','unbound'}:
                public['result']['account_binding']=result['account_binding']
        return public

    def check_session(self,owner,workspace_id):
        workspace=self.resolve(owner,workspace_id)
        return submit_inspection(workspace.session,'check_session')

    def configure_priority(self,owner,workspace_id,priority):
        workspace=self.resolve(owner,workspace_id)
        return submit_inspection(workspace.session,'configure_priority',{'priority':priority})

    def configure_daily(self,owner,workspace_id,*,enabled=True,at='08:00',timezone='America/Chicago',options=None,run_now=False):
        workspace=self.resolve(owner,workspace_id)
        from .daily import DEFAULT_OPTIONS
        if options is not None and not isinstance(options,dict):raise ArchiveError('Daily options must be fixed server-bounded values.')
        values=DEFAULT_OPTIONS|dict(options or {})
        for key in ('limit','max_pages','max_bytes','max_seconds'):
            maximum=getattr(self.limits,key)
            if options is None or key not in options:
                values[key]=min(values[key],maximum)
            elif type(values[key]) is not int or not 0<values[key]<=maximum:
                raise ArchiveError('Requested daily job budget exceeds server limits.')
        return submit_inspection(workspace.session,'configure_daily',{'enabled':enabled,'at':at,'timezone':timezone,
            'options':values,'run_now':run_now})

    def daily_status(self,owner,workspace_id):
        from .daily import status
        return status(self.resolve(owner,workspace_id).session)

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

    def worker_spec(self,owner,workspace_id,headless=True):
        workspace=self.resolve(owner,workspace_id)
        if type(headless) is not bool:
            raise ArchiveError('Worker display mode must be an explicit boolean.')
        args=[sys.executable,'-m','comic_archive','--browser','firefox']
        if headless:
            args.append('--headless')
        args.extend(('browser-worker','--stable-code'))
        return {'cwd':workspace.root,'argv':tuple(args)}  # Server-only launch data.

    def _exports(self,workspace):
        result={}
        def add(path,kind):
            if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)):
                raise ArchiveError('Refusing an unsafe export file.')
            relative=path.relative_to(workspace.library).as_posix()
            key=hmac.new(self._key,f'export\0{workspace.id}\0{relative}'.encode(),hashlib.sha256).hexdigest()
            result[key]=(path,kind)
        folders=sorted({p.parent for ext in ('.cbz', '.epub', '.tar.zst') for p in workspace.library.glob('*/*'+ext) if not p.parent.name.startswith('.')})
        for folder in folders:
            series=folder/'series.json'
            if series.is_symlink() or folder.is_symlink():
                raise ArchiveError('Refusing unsafe export storage.')
            episodes=[p for p in episode_archives(folder) if sidecar_path(p).is_file()]
            if not episodes:
                continue
            if series.is_file():
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

    def exports(self,owner,workspace_id):
        workspace=self.resolve(owner,workspace_id)
        return [{'id':key,'filename':path.name,'kind':kind,'size':path.stat().st_size}
                for key,(path,kind) in self._exports(workspace).items()]

    def saved_files(self, owner, workspace_id, **options):
        workspace = self.resolve(owner, workspace_id)
        return self.saved_catalog.page(workspace, self.interface_mode == 'local', **options)

    def open_export(self,owner,workspace_id,export_id):
        workspace=self.resolve(owner,workspace_id)
        if not isinstance(export_id,str) or not re.fullmatch(r'[a-f0-9]{64}',export_id):
            raise ArchiveError('Export is unavailable for this owner.')
        selected=self._exports(workspace).get(export_id)
        if selected is None:
            raise ArchiveError('Export is unavailable for this owner.')
        path,kind=selected
        def pin(candidate):
            if any(p.is_symlink() for p in (candidate,*candidate.parents)):
                raise ArchiveError('Refusing an unsafe export file.')
            fd=os.open(candidate,os.O_RDONLY|NOFOLLOW|NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd);raise ArchiveError('Refusing an unsafe export file.')
            return os.fdopen(fd,'rb')
        def metadata(stream,maximum):
            raw=stream.read(maximum+1)
            if len(raw)>maximum:raise ArchiveError('Export metadata exceeds the supported size.')
            value=json.loads(raw)
            if not isinstance(value,dict):raise ArchiveError('Export metadata is invalid.')
            return value
        try:
            with ExitStack() as stack:
                primary=stack.enter_context(pin(path))
                if kind in {'cbz','epub','tar.zst','episode_metadata'}:
                    sidecar=primary if kind=='episode_metadata' else stack.enter_context(pin(sidecar_path(path)))
                    value=metadata(sidecar,16*1024*1024)
                    extension=archive_extension(value)
                    if kind!='episode_metadata' and not path.name.endswith(extension):
                        raise ArchiveError('Export format changed; refresh the file list before download.')
                    archive=stack.enter_context(pin(archive_from_sidecar(path,value))) if kind=='episode_metadata' else primary
                    errors=verify_episode_contents(archive,value)
                else:
                    series=primary if kind=='series_metadata' else stack.enter_context(pin(path.parent/'series.json'))
                    value=metadata(series,256*1024)
                    name=value.get('cover_file');cover=None
                    if name is not None:
                        if name not in {'cover.jpg','cover.png','cover.webp','cover.gif'}:
                            raise ArchiveError('Export requires repair before download.')
                        if kind=='cover' and name!=path.name:
                            raise ArchiveError('Export changed; refresh the file list before download.')
                        image=primary if kind=='cover' else stack.enter_context(pin(path.parent/name))
                        cover=image.read(10*1024*1024+1)
                    elif kind=='cover':
                        raise ArchiveError('Export changed; refresh the file list before download.')
                    errors=verify_series_contents(value,cover)
                if errors:raise ArchiveError('Export requires repair before download.')
                # Verification and streaming use the same inode. A replacement
                # can affect a later download, never these verified bytes.
                info=os.fstat(primary.fileno())
                stream=os.fdopen(os.dup(primary.fileno()),'rb');stream.seek(0)
                return ExportDownload(path.name,info.st_size,kind,stream)
        except (OSError,ValueError,TypeError,RecursionError):
            raise ArchiveError('Export changed or requires repair; refresh and retry the download.') from None
