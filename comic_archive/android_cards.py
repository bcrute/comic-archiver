"""Each site's dashboard card for the phone, in the desktop's own words.

The phone reads the same download record, counts and estimates as the desktop and puts them
through dashboard_model, so a card says the same thing on both. Only what differs about the
phone is decided here: its downloader starts with the download, and nothing resumes by itself.
"""
import sqlite3
import time

from . import android_session, sites, worker_host
from .dashboard_model import State, card_summary, view
from .local_library import LOCAL_OWNER
from .models import ArchiveError


class Cards:
    """What each opened site's card last read, kept while the app runs: the speed and the
    waits on a card are measured from one reading to the next."""

    def __init__(self):
        self._states = {}
        self._records = {}

    def forget(self, session=None):
        """Drop what was read for one site's session folder, or for every site."""
        for kept in (self._states, self._records):
            kept.clear() if session is None else kept.pop(str(session), None)

    def card(self, registry, *, signed_in=None, now=None):
        """One site's card. signed_in is what the sign-in screen knows of the site being shown:
        False when its saved sign-in no longer works, None when it has not said."""
        now = time.time() if now is None else now
        workspace = registry.resolve(LOCAL_OWNER, registry.workspace_id)
        site = sites.of_session(workspace.session)
        key = str(workspace.session)
        s = self._states.setdefault(key, State())
        s.sites = sites.choices(site)
        try:
            reading = self._read(registry, workspace, s, key, signed_in, now)
        except (ArchiveError, OSError, sqlite3.Error):
            s.refresh_failed()
        else:
            s.received_status(reading['status'], now, transfer=reading['account'].get('transfer'))
            s.received_account(reading['account'], now)
            s.network, s.network_at = reading['network'], now
            s.loaded = True
            s.refreshed(now)
        v = view(s, now)
        summary = card_summary(s, v)
        return {'site': site.key, 'name': site.name, 'title': v['title'], 'state': summary['state'],
                'appearance': summary['appearance'], 'message': s.message.text if s.message else '',
                'rows': [{'name': row['name'], 'value': row['value'], 'warning': bool(row['warning'])}
                         for row in summary['rows']],
                # What Settings lists for the site: its download issues, and saved and pending by episode type.
                'issues': [{'reason': row['reason'], 'count': row['count']} for row in v['not_downloaded']],
                'types': [{'label': row['label'], 'saved': row['saved'], 'waiting': row['waiting']}
                          for row in v['episode_types']]}

    def _read(self, registry, workspace, s, key, signed_in, now):
        identity = registry.workspace_id
        status = registry.status(LOCAL_OWNER, identity)
        account = registry.account_status(LOCAL_OWNER, identity)
        worker = worker_host.status(workspace.session)
        if worker['running'] is True:
            state = worker.get('state')
            status['worker'] = {'reported_state': state if state in ('ready', 'starting') else 'busy'}
        elif worker['running'] is False:
            # The phone's downloader starts with the download, so one that is not running is ready
            # to. A download it was in the middle of waits for Start, as one that was paused does.
            status['worker'] = {'reported_state': 'ready'}
            status['progress'] = None
            if account.get('status') == 'running':
                account = account | {'status': 'stopped'}
        recorded = android_session.is_signed_in(workspace.session)
        status['session_configured'] = recorded
        if not recorded:
            auth, record = {'status': 'idle'}, None
        else:
            marker = (workspace.session / android_session.MARKER).stat()
            record = (marker.st_ino, marker.st_mtime_ns)
            auth = {'status': 'expired' if signed_in is False else 'connected'}
            if record != self._records.get(key, record) and auth['status'] == 'connected':
                # Signed in again since the last reading: an earlier refusal no longer counts.
                s.received_site_auth({'status': 'idle'}, now)
        self._records[key] = record
        s.received_site_auth(auth, now)
        try:
            network = registry.network_status(LOCAL_OWNER, identity)
        except (ArchiveError, OSError):
            network = None
        return {'status': status, 'account': account, 'network': network}
