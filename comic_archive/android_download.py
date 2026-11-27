"""Android Go admission and the one-time repair of the prototype's empty list scans."""
from . import account_ui
from .models import ArchiveError
from .messages import say


def selected_speed(workspace):
    # Android starts at Medium until a choice is actually stored. Merely viewing
    # settings is read-only; the first explicit Go commits that displayed default.
    return account_ui.speed(workspace) if (workspace.session / account_ui.SPEED_FILE).exists() else 'medium'


def speed_available(site_key, speed, browser_factory=None):
    if speed not in account_ui.SPEEDS:
        return False
    if speed != 'fast' or site_key != 'tapas':
        return True  # Other sites keep the desktop's ordinary-reader Fast policy.
    from .worker_host import browser_available
    return browser_available(browser_factory)


def start(workspace, *, browser_factory=None):
    from . import sites
    from .worker_host import browser_available
    speed = selected_speed(workspace)
    if (sites.of_session(workspace.session).key == 'tapas' and browser_factory is not None
            and not browser_available(browser_factory)):
        # The phone's own browser is there but too old for the reader the desktop uses. One reader is supported,
        # so the download says what to update instead of saving less with another.
        raise ArchiveError(say('settings.update_android_system_webview_to_save'))
    if not speed_available(sites.of_session(workspace.session).key, speed, browser_factory):
        raise ArchiveError(say('downloads.fast_mode_needs_an_updated_android'))
    if not (workspace.session / account_ui.SPEED_FILE).exists():
        account_ui.choose_speed(workspace, speed)
    account_ui.repair_android_list_reads(workspace)
    if list_only(workspace):
        account_ui.start(workspace, list_only=True)
        hidden = workspace.session / 'episode-list-cleared'
        if hidden.exists():
            hidden.replace(workspace.session / 'episode-list-refreshing')
    else:
        account_ui.start(workspace)
        # An ordinary download reads the lists itself, so a list cleared before it is shown again.
        for marker in ('episode-list-cleared', 'episode-list-refreshing'):
            (workspace.session / marker).unlink(missing_ok=True)


def list_only(workspace):
    try:
        (workspace.session / 'episode-list-only').stat()
    except FileNotFoundError:
        return False
    return True


def list_awaited(workspace):
    """Whether a fetch of the episode list has been started since the list was cleared."""
    return (workspace.session / 'episode-list-refreshing').is_file()


def list_hidden(workspace, account=None):
    """Whether the recorded list is hidden. account is the download as just read from its record, given only by a
    caller that has such a reading: a cleared list is shown again once the fetch started after the clear has
    stopped (finished, paused or held) with something listed. While it runs, and when it stopped with nothing,
    the list stays hidden."""
    from .window_library import list_fetched
    refreshing = workspace.session / 'episode-list-refreshing'
    if refreshing.is_file() and account and list_fetched(account):
        refreshing.unlink(missing_ok=True)
    return refreshing.is_file() or (workspace.session / 'episode-list-cleared').is_file()


def change_list(workspace, action):
    from .locking import LibraryLock
    with LibraryLock(workspace.session / '.account-ui'):
        if account_ui.status(workspace).get('status') == 'running':
            raise ArchiveError('Pause this provider before changing its episode list.')
        mode = workspace.session / 'episode-list-only'
        if action == 'list':
            mode.touch(mode=0o600)
        elif action == 'files':
            mode.unlink(missing_ok=True)
        elif action == 'clear':
            (workspace.session / 'episode-list-cleared').touch(mode=0o600)
            (workspace.session / 'episode-list-refreshing').unlink(missing_ok=True)
        else:
            raise ArchiveError('Choose an episode list action.')


def status(workspace):
    """Public counters from the current durable run; no titles, paths or identities.

    The account plan may be in its series-discovery phase while the backlog is
    actually saving episodes. Prefer the active run's phase, never a previous
    run's leftover progress file. Inventory counts remain useful between batches.
    """
    import sqlite3
    import time

    from . import worker_host
    from .dashboard_model import not_downloaded_counts
    from .inventory import Inventory
    from .monitoring import _progress, _read

    account = account_ui.status(workspace)
    worker = worker_host.status(workspace.session)
    counts, progress = {}, {}
    path = workspace.library / '.archive-state/progress.json'
    raw = _read(path)
    try:
        with Inventory(workspace.library, read_only=True) as inventory:
            counts['saved_episodes'] = inventory.db.execute(
                "SELECT COUNT(*) FROM episodes WHERE status='archived'").fetchone()[0]
            counts['observed_series'] = inventory.db.execute('SELECT COUNT(*) FROM series').fetchone()[0]
            # Why checked episodes were not downloaded, as counts by reason; Settings words them.
            by_status, unsupported = inventory.episode_counts()
            counts['not_downloaded'] = not_downloaded_counts(
                {'episodes_by_status': by_status, 'unsupported_by_reason': unsupported})
            last = inventory.db.execute(
                'SELECT id,status FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
        if (account.get('status') == 'running' and worker.get('running') is True
                and worker.get('state') == 'busy' and last and last['status'] == 'running'
                and raw.get('run_id') == last['id'] and raw.get('status') == 'running'):
            safe = _progress(raw)
            keys = ('completed', 'downloaded_pages', 'downloaded_chapters', 'downloaded_bytes',
                    'reading_list_page_count', 'observed_library_series_count',
                    'series_checks_completed', 'series_check_target_count')
            progress = {k: safe[k] for k in keys if k in safe}
            if safe.get('phase') in {'authentication', 'account_list', 'scan', 'metadata',
                                     'archive', 'history', 'history_series'}:
                progress['phase'] = safe['phase']
            progress['age_seconds'] = max(0, int(time.time() - path.stat().st_mtime))
    except (ArchiveError, OSError, sqlite3.Error):
        # A fresh library or a short-lived index lock must not hide plan state.
        # Missing observations stay missing; no counters or heartbeat are made up.
        pass
    retry = account.get('retry')
    if (isinstance(retry, dict) and retry.get('reason') == 'connection' and worker.get('running') is True
            and worker.get('state') == 'ready' and not worker.get('stop_requested')):
        # Nothing can happen until the phone's connection changes, so the app need not keep the phone awake for
        # this wait. A wait on a connection that may be used (the site could not be reached) ends by the clock.
        from . import network
        try:
            if network.status(workspace.library)['allowed'] is False:
                retry['connection_unusable'] = True
        except (ArchiveError, OSError):
            pass
    return {'account': account, 'worker': worker, 'counts': counts, 'progress': progress}
