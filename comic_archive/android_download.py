"""Android Go admission and the one-time repair of the prototype's empty list scans."""
from . import account_ui
from .models import ArchiveError


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
    speed = selected_speed(workspace)
    if not speed_available(sites.of_session(workspace.session).key, speed, browser_factory):
        raise ArchiveError('Fast mode needs an updated Android System WebView. Choose Medium or Slow in Settings.')
    if not (workspace.session / account_ui.SPEED_FILE).exists():
        account_ui.choose_speed(workspace, speed)
    account_ui.repair_android_list_reads(workspace)
    account_ui.start(workspace)


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
    return {'account': account, 'worker': worker, 'counts': counts, 'progress': progress}
