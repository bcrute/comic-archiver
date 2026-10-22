"""Android Go admission and the one-time repair of the prototype's empty list scans."""
from . import account_export, account_ui, batches
from .archive import atomic_json
from .locking import LibraryLock
from .models import ArchiveError
from .queue_client import task_status


def start(workspace):
    # The Android HTTP reader is the only engine. Desktop fast mode would try to
    # launch a second browser, both for metadata and for episode archives.
    if account_ui.speed(workspace) == 'fast':
        account_ui.choose_speed(workspace, 'medium')
    root = batches._root(workspace.session, workspace.library)
    marker = workspace.session / 'android-reader-version.json'
    with LibraryLock(root / '.android-reader-upgrade'):
        if not marker.exists():
            with LibraryLock(root / '.account-export-state'):
                value = account_export._load(root, account_export.lists(workspace.session))
                if (value and value['phase'] == 'lists' and value['status'] in {'held', 'stopped'}
                        and any(not check['complete'] and check['observed_series_count'] == 0
                                for check in value['list_checks'])):
                    # The old anchor-only parser recorded the mobile lists as empty,
                    # then RECENT failed with HTTP400. Re-read those lists on explicit
                    # Go. Keep old task receipts and every inventory/archive intact.
                    pending = value['pending']
                    if pending:
                        response = task_status(workspace.session, pending['task_id'])
                        if response['status'] not in {'ok', 'error'}:
                            raise ArchiveError('The previous download is still stopping; try Go again shortly.')
                        value['last_failed_task_id'] = pending['task_id']
                    value.update(category_index=0, list_checks=[], pending=None)
                    account_export._save(root, value)
            atomic_json(marker, {'version': 1})
            marker.chmod(0o600)
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
