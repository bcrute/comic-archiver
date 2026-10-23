"""What the window asks of the open library, and what it reads back: the app's own calls, without Qt.

Each call runs on a thread of the window's, never on the window's own: some take seconds. Calls that change the
library take turns, as one request at a time once did; reading saved files does not wait for them. A refusal comes
back as Refused, worded for the person. Nothing here names a path, a key or an account in what it says or logs.
"""
import logging
import re
import threading
from urllib.parse import urlencode

from . import cloud_runtime, sites
from .dashboard_model import LIBRARY_SECONDS, NETWORK_SECONDS, Message, error_text
from .errors import DeviceFileOpenUnavailable, LibraryBusy
from .library_model import NO_OPENER, OPEN_FAILED, UNAVAILABLE
from .local_library import LOCAL_OWNER
from .models import ArchiveError
from .storage_model import task_text

LOG = logging.getLogger('comic_archive.desktop')
STORAGE_MODES = ('shared', 'separate')
TASK_ID = re.compile(r'job-[a-f0-9]{24}')
EXPORT_ID = re.compile(r'[a-f0-9]{64}')


class Refused(Exception):
    """An action the library did not take, or a read it could not answer; str() is what the person is told."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def described(error):
    from .app_runtime import describe
    return describe(error)


class Actions:
    """The open library's calls, for whichever runtime the window has now: a site or folder change replaces it."""

    def __init__(self, runtime):
        self._runtime = runtime  # A callable: the window's current runtime.
        self._turn = threading.RLock()

    def _registry(self):
        runtime = self._runtime()
        registry = runtime.registry if runtime is not None else None
        if registry is None:
            raise Refused('service_unavailable', error_text('service_unavailable'))
        return runtime, registry

    def _call(self, name, *args, locked=True, failed=None, **options):
        runtime, registry = self._registry()
        site = runtime.site.name
        try:
            if not locked:
                return getattr(registry, name)(LOCAL_OWNER, registry.workspace_id, *args, **options)
            with self._turn:
                return getattr(registry, name)(LOCAL_OWNER, registry.workspace_id, *args, **options)
        except LibraryBusy:
            raise Refused('library_busy', error_text('library_busy', site)) from None
        except DeviceFileOpenUnavailable:
            raise Refused('device_file_open_unavailable', NO_OPENER) from None
        except ArchiveError:
            raise Refused('request_unavailable', failed or error_text('request_unavailable', site)) from None
        except Exception as error:
            # Exceptions can carry account data and local paths: the log names the kind and the place only.
            LOG.error('the library did not answer %s: %s', name, described(error))
            raise Refused('service_unavailable', failed or error_text('service_unavailable', site)) from None

    def context(self):
        """What the window shows around the library: the sites, their settings and the folder."""
        runtime, registry = self._registry()
        try:
            settings = runtime.site_settings.list()
        except Exception as error:
            LOG.warning('the sites\' settings were not read: %s', described(error))
            settings = []
        return {'sites': sites.choices(runtime.site), 'site_settings': settings, 'library_folder': str(runtime.folder),
                'cloud_offered': cloud_runtime.offered(), 'workspace_id': registry.workspace_id}

    def poll(self, task=None, network=True, cloud=True, library=False, refresh=False):
        """One refresh of the dashboard: every read it needs, together, or Refused if any failed."""
        reading = {'status': self._call('status'), 'account': self._call('account_status'),
                   'site_auth': self._call('site_signin_status')}
        if cloud:
            reading['cloud'] = self._call('cloud_status')
            reading['cloud_auth'] = self._call('cloud_signin_status')
        if task is not None:
            reading['task'] = self.task(task)
        if network:
            reading['network'] = self._call('network_status')
        if library:
            # Its own failure only says that the counts are unavailable (False); the rest of the refresh stands.
            try:
                reading['summary'] = self.saved_files(summary=True, refresh=refresh)
            except Refused:
                reading['summary'] = False
        return reading

    def go(self):
        return self._call('start_account')

    def pause(self):
        return self._call('pause_account')

    def signin(self):
        return self._call('start_site_signin')

    def sign_out(self):
        return self._call('sign_out_site')

    def speed(self, value):
        return self._call('choose_speed', value)

    def site_speed(self, key, value):
        """Another site's speed; the open site's goes through its own account, which may be downloading."""
        runtime, _ = self._registry()
        try:
            with self._turn:
                return runtime.site_settings.choose_speed(key, value)
        except ArchiveError:
            raise Refused('request_unavailable', error_text('request_unavailable', runtime.site.name)) from None

    def wifi_only(self, value):
        if type(value) is not bool:
            raise Refused('request_unavailable', error_text('request_unavailable'))
        return self._call('configure_network', value)

    def cloud_enable(self, provider, enabled):
        return self._call('configure_cloud', provider, enabled)

    def choose_storage(self, chosen, targets):
        """One place at a time: whatever was chosen before is turned off first, then the new one on."""
        for target in targets:
            if target.get('enabled') and target.get('provider') != chosen:
                self.cloud_enable(target['provider'], False)
        if chosen != 'device':
            self.cloud_enable(chosen, True)
        return self._call('cloud_status')

    def cloud_storage(self, mode):
        if mode not in STORAGE_MODES:
            raise Refused('request_unavailable', error_text('request_unavailable'))
        return self._call('choose_cloud_storage', mode)

    def cloud_signin(self, provider, credentials):
        from .cloud_auth import validate
        try:
            validate(provider, credentials)
        except ArchiveError:
            raise Refused('request_unavailable', error_text('request_unavailable')) from None
        return self._call('start_cloud_signin', provider, credentials)

    def cloud_cancel(self):
        return self._call('cancel_cloud_signin')

    def cloud_test(self, provider='b2'):
        from .cloud import PROVIDERS
        if provider not in PROVIDERS:
            raise Refused('request_unavailable', error_text('request_unavailable'))
        return self._call('test_cloud', provider)

    def cloud_upload(self):
        return self._call('upload_cloud')

    def cloud_cleanup(self, test_id):
        from .publication import valid_export_id
        if not valid_export_id(test_id):
            raise Refused('request_unavailable', error_text('request_unavailable'))
        return self._call('cleanup_cloud_test', test_id)

    def task(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise Refused('request_unavailable', error_text('request_unavailable'))
        return self._call('task', task_id)

    def saved_files(self, **options):
        """A page of the saved-file catalog. Read behind the catalog's own lock: a slow scan does not hold up Pause or Go."""
        from .saved_library import options as checked
        values = {k: ('1' if v else '0') if type(v) is bool else str(v) for k, v in options.items() if k != 'query'}
        if options.get('query'):
            values['q'] = options['query']
        try:
            chosen = checked(urlencode(values))
        except ArchiveError:
            raise Refused('request_unavailable', error_text('request_unavailable')) from None
        return self._call('saved_files', locked=False, failed=UNAVAILABLE, **chosen)

    def open_file(self, export_id):
        if not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id):
            raise Refused('request_unavailable', OPEN_FAILED)
        return self._call('open_file_on_device', export_id, failed=OPEN_FAILED)


def due(state, now):
    """What the next refresh reads besides the dashboard itself: (network, library, recount)."""
    return (now - state.network_at > NETWORK_SECONDS, state.library_refresh or now - state.library_at > LIBRARY_SECONDS,
            state.library_refresh)


def received(state, reading, now, task=None):
    """Put a whole refresh on the window's state. Returns True when the B2 key fields are to be emptied: the key
    has just connected. task is the cloud task the refresh asked about; one started since is left alone."""
    previous_account, previous_auth = (state.account or {}).get('status'), (state.cloud_auth or {}).get('status')
    state.status = reading['status']
    state.received_account(reading['account'], now)
    state.received_site_auth(reading['site_auth'], now)
    for name in ('cloud', 'cloud_auth', 'network'):
        if name in reading:
            setattr(state, name, reading[name])
    if 'network' in reading:
        state.network_at = now
    if 'summary' in reading:
        state.summary, state.library_at, state.library_refresh = reading['summary'], now, False
        if reading['summary']:
            state.locations = reading['summary'].get('locations') or []
    state.loaded = True
    state.refreshed(now)
    result = reading.get('task')
    if result and task == state.task and result.get('status') in ('ok', 'error'):
        text, error = task_text(state.task_kind, result, state.site())
        state.cloud_message = Message(text, error)
        if error:
            state.failed(text, now)
        state.task = state.task_kind = None
        state.library_refresh = True
    if previous_account != 'finished' and state.account.get('status') == 'finished':
        state.library_refresh = True
    connected = previous_auth != 'connected' and (state.cloud_auth or {}).get('status') == 'connected'
    if connected:
        state.bucket_draft, state.library_refresh = None, True
    return connected
