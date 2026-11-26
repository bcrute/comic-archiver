"""Each site's dashboard card for the phone, in the desktop's own words.

The phone reads the same download record, counts and estimates as the desktop and puts them
through dashboard_model, so a card says the same thing on both. Only what differs about the
phone is decided here: its downloader starts with the download, and nothing resumes by itself.
"""
import sqlite3
import time

from . import android_download, android_session, sites, worker_host
from .dashboard_model import SPEED_NAMES, SPEEDS, State, card_summary, download_choice, pill, view
from .local_library import LOCAL_OWNER
from .messages import say
from .models import ArchiveError


def _word(value):
    return value if isinstance(value, str) and value.isidentifier() else ''


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

    def state(self, session):
        """What one site's card last read, for the details given to support; None before its first card."""
        return self._states.get(str(session))

    def card(self, registry, *, signed_in=None, now=None):
        """One site's card. signed_in is what the sign-in screen knows of the site being shown:
        False when its saved sign-in no longer works, None when it has not said."""
        now = time.time() if now is None else now
        workspace = registry.resolve(LOCAL_OWNER, registry.workspace_id)
        site = sites.of_session(workspace.session)
        key = str(workspace.session)
        s = self._states.setdefault(key, State())
        s.sites = sites.choices(site)
        bulk = ''
        # A cleared list is shown again only on what this reading finds in the download's record, and only when
        # the fetch was already under way before the reading began: a Start can land in the middle of one.
        awaited, fresh = android_download.list_awaited(workspace), None
        try:
            reading = self._read(registry, workspace, s, key, signed_in, now)
        except (ArchiveError, OSError, sqlite3.Error):
            s.refresh_failed()
        else:
            fresh = reading['recorded'] if awaited else None
            s.received_status(reading['status'], now, transfer=reading['account'].get('transfer'))
            s.received_account(reading['account'], now)
            s.network, s.network_at = reading['network'], now
            bulk = reading['bulk']
            s.loaded = True
            s.refreshed(now)
        v = view(s, now)
        summary = card_summary(s, v)
        hidden = android_download.list_hidden(workspace, fresh)
        mode = android_download.list_only(workspace)
        return {'bulk': bulk, 'site': site.key, 'name': site.name, 'title': v['title'], 'state': summary['state'],
                'appearance': summary['appearance'], 'message': s.message.text if s.message else '',
                # The download's own word for where it is, so the phone can tell a card read before a press of
                # Start or Pause from one read after it.
                'download': _word((s.account or {}).get('status')) if s.loaded else '',
                'rows': [{'name': row['name'], 'value': row['value'], 'warning': bool(row['warning'])}
                         for row in summary['rows']],
                # The card as the desktop draws it: its pill, its line of status, its one button for what can be
                # done now, Sign out, the site's speed and the episode types it downloads.
                'pill': pill(summary['state']), 'tone': summary['tone'],
                'status': v['signin_status'], 'status_error': bool(v['signin_status_error']),
                'action': v['action'], 'action_text': v['action_text'], 'action_enabled': bool(v['action_enabled']),
                'signout': bool(v['signout_visible']), 'signout_enabled': bool(v['signout_enabled']),
                'speed': v['speed'] if v['speed_visible'] and s.account is not None else '',
                'speed_enabled': bool(v['speed_visible'] and v['speed_enabled']),
                'speeds': [{'key': key, 'label': SPEED_NAMES[key]} for key in SPEEDS],
                'selection': (say('window.download_value', value=say('window.episode_list_only')) if mode
                              else download_choice(s.account if s.loaded else None)[0]),
                'list_only': mode,
                'has_episodes': bool((s.status or {}).get('episode_count')) and not hidden,
                'has_list': bool((s.status or {}).get('episode_count') or (s.status or {}).get('series_count'))
                            and not hidden,
                # What Settings lists for the site: its download issues, and saved and pending by episode type.
                'issues': [{'reason': row['reason'], 'count': row['count']} for row in v['not_downloaded']],
                'types': [{'label': row['label'], 'saved': row['saved'], 'waiting': row['waiting']}
                          for row in v['episode_types']]}

    def _read(self, registry, workspace, s, key, signed_in, now):
        identity = registry.workspace_id
        status = registry.status(LOCAL_OWNER, identity)
        account = download = registry.account_status(LOCAL_OWNER, identity)
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
        bulk = 'on'
        if (workspace.library / '.archive-state/inventory.sqlite3').is_file():
            from . import comic_selection
            from .inventory import Inventory
            with Inventory(workspace.library, read_only=True) as inventory:
                policy = comic_selection.read(inventory.db)
                overrides = comic_selection._episode_table(inventory.db) and inventory.db.execute(
                    'SELECT 1 FROM comic_episode_choices LIMIT 1').fetchone()
                bulk = 'mixed' if policy['exceptions'] or overrides else 'on' if policy['default'] else 'off'
        # recorded is the download as its record has it: one whose downloader has yet to start still says running.
        return {'status': status, 'account': account, 'recorded': download, 'network': network, 'bulk': bulk}
