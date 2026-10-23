"""Native cloud actions over the desktop's registry, policies and durable queue.

No sign-in cookies or private connection paths cross this UI projection. Starting
a cloud test may prepare the embedded worker, but never calls account Go.
"""
import re
import threading

from . import android_session, app_actions, background, sites, storage_model, worker_host
from .dashboard_model import State
from .local_library import LOCAL_OWNER
from .locking import LibraryLock
from .models import ArchiveError

TASKS = {'test': 'cloud-test', 'retry': 'cloud', 'cleanup': 'cloud-cleanup'}


class AndroidCloud:
    def __init__(self, registry):
        self.registry = registry
        self.workspace = registry.resolve(LOCAL_OWNER, registry.workspace_id)
        self.site = sites.of_session(self.workspace.session)
        self._lock = threading.RLock()
        self._task = None
        self._kind = None
        self._message = ''
        self._recover_task()

    def _recover_task(self):
        """The core queue is durable across process death; adopt it without running it.

        Fixed cloud requests contain no credentials. Read with the queue's own
        path/size guards, and never derive an executable command from their data.
        """
        folder = self.workspace.session / 'worker'
        for path in sorted((folder / 'requests').glob('job-*.json')):
            if not re.fullmatch(r'job-[a-f0-9]{24}', path.stem):
                continue
            if (folder / 'responses' / path.name).exists():
                continue
            record = background.read(path)
            kinds = {'cloud_test': 'cloud-test', 'cloud_upload': 'cloud', 'cloud_cleanup': 'cloud-cleanup'}
            operation = record.get('operation')
            if isinstance(operation, str) and operation in kinds:
                self._task, self._kind = path.stem, kinds[operation]
                self._message = 'A cloud task is unfinished. Continue it when ready; local originals are retained.'
                return

    def _read(self):
        registry, identity = self.registry, self.registry.workspace_id
        status = registry.status(LOCAL_OWNER, identity)
        account = registry.account_status(LOCAL_OWNER, identity)
        # The Android record is verified by the native sign-in engine. Desktop's
        # empty compatibility state file alone must not enable cloud tasks.
        status['session_configured'] = android_session.is_signed_in(self.workspace.session)
        return State(sites=sites.choices(android_session.signed_in_site(self.workspace.session) or self.site),
                     status=status, account=account,
                     cloud=registry.cloud_status(LOCAL_OWNER, identity),
                     cloud_auth=registry.cloud_signin_status(LOCAL_OWNER, identity),
                     task=self._task, task_kind=self._kind, workspace_id=identity)

    @property
    def busy(self):
        # Read while the caller holds .account-ui; do not invert its lock order
        # with this controller's lock while Go and a cloud action race.
        return bool(self._task or self.registry.cloud_signin.status().get('status') == 'running')

    def _view(self, state):
        worker = state.status.get('worker') or {}
        can_start = (state.status['session_configured'] and worker.get('reported_state') not in {'busy', 'starting'}
                     and state.account.get('status') != 'running')
        return storage_model.b2(state, can_start_worker=can_start)

    def snapshot(self):
        with self._lock:
            if self._task:
                result = self.registry.task(LOCAL_OWNER, self.registry.workspace_id, self._task)
                if result['status'] in {'ok', 'error'}:
                    self._message = storage_model.task_text(self._kind, result)[0]
                    self._task = self._kind = None
                    self._recover_task()
            state = self._read()
            view = self._view(state)
            choice = storage_model.storage_choice(state)
            try:
                summary = self.registry.saved_files(LOCAL_OWNER, self.registry.workspace_id, summary=True)
                state.locations = summary.get('locations') or []
            except ArchiveError:
                state.locations = []
            place = storage_model.storage(state)
            projected = {key: view[key] for key in (
                        'pill', 'text', 'status', 'auth', 'repair', 'bucket', 'connect_text', 'connect_enabled',
                        'cancel_visible', 'cancel_enabled', 'test_enabled', 'retry_enabled', 'sharing_visible',
                        'sharing_enabled', 'shared', 'sharing_help', 'cleanup')}
            projected['cleanup'] = view['cleanup'][:200]
            return {'b2': projected, 'cleanup_remaining': max(0, len(view['cleanup']) - 200),
                    'bucket_suggestion': storage_model.bucket_name(None, view['bucket'], self.registry.workspace_id),
                    'destination': choice['chosen'], 'destinations': choice['options'],
                    'destination_help': choice['help'], 'destination_lock': choice['lock'],
                    'device_stored': place['device_stored'], 'b2_stored': place['b2_stored'],
                    'cost': view['cost'],
                    'message': self._message, 'task_running': self._task is not None,
                    'task_held': bool(self._task and not worker_host.status(self.workspace.session)['running']),
                    'auth_status': (state.cloud_auth or {}).get('status', 'idle')}

    def act(self, action, *, credentials=None, value=None, start_worker=None):
        with self._lock, LibraryLock(self.workspace.session / '.account-ui'):
            state = self._read()
            view = self._view(state)
            registry, identity = self.registry, self.registry.workspace_id
            if action == 'cancel':
                if not view['cancel_visible'] or not view['cancel_enabled']:
                    raise ArchiveError('There is no cloud sign-in to cancel.')
                registry.cancel_cloud_signin(LOCAL_OWNER, identity)
            elif action == 'resume':
                if (not self._task or not state.status['session_configured'] or not callable(start_worker)
                        or state.account.get('status') == 'running' or (state.cloud_auth or {}).get('status') == 'running'):
                    raise ArchiveError('Sign in and pause saving before continuing cloud tasks.')
                start_worker()
                self._message = 'Continuing the unfinished cloud task. Saving still waits for Go.'
            elif action == 'connect':
                if not view['connect_enabled']:
                    raise ArchiveError('Finish the current activity before changing the B2 connection.')
                registry.start_cloud_signin(LOCAL_OWNER, identity, 'b2', credentials)
                self._message = storage_model.CONNECTING
            elif action == 'sharing':
                if not view['sharing_enabled'] or self._task or state.account.get('status') == 'running':
                    raise ArchiveError('Finish the current activity before changing cloud sharing.')
                if value not in {'shared', 'separate'}:
                    raise ArchiveError('Choose shared or separate cloud storage.')
                registry.choose_cloud_storage(LOCAL_OWNER, identity, value)
            elif action == 'destination':
                choice = storage_model.storage_choice(state)
                selected = next((option for option in choice['options'] if option['key'] == value), None)
                if not selected or not selected['enabled']:
                    raise ArchiveError('Check the connection and finish the current activity before changing storage.')
                # Reuse the exact desktop switching order and enabling checks.
                runtime = type('CloudRuntime', (), {'registry': registry, 'site': self.site})()
                try:
                    app_actions.Actions(lambda: runtime).choose_storage(value, state.cloud_targets())
                except app_actions.Refused as error:
                    raise ArchiveError(str(error)) from None
                self._message = 'Storage choice saved. Saving still waits for Go.'
            elif action in TASKS:
                permitted = (view['test_enabled'] if action == 'test' else view['retry_enabled'] if action == 'retry'
                             else any(row['test_id'] == value and row['enabled'] for row in view['cleanup']))
                if not permitted or not callable(start_worker):
                    raise ArchiveError('Sign in and pause saving before starting this cloud task.')
                start_worker()  # Explicit task admission, never account Go.
                fresh = self._read()
                if not storage_model.can_import(fresh):
                    raise ArchiveError('The downloader is not ready for a cloud task. Try again.')
                if action == 'test':
                    task = registry.test_cloud(LOCAL_OWNER, identity, 'b2')
                elif action == 'retry':
                    task = registry.upload_cloud(LOCAL_OWNER, identity)
                else:
                    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9-]{36}', value):
                        raise ArchiveError('Choose a recorded cloud test to clean up.')
                    task = registry.cleanup_cloud_test(LOCAL_OWNER, identity, value)
                self._task, self._kind = task['task_id'], TASKS[action]
                self._message = storage_model.TASK_STARTED[self._kind]
            else:
                raise ArchiveError('Choose a supported cloud action.')
        return self.snapshot()
