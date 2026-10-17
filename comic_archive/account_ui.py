"""Fixed account-wide actions for the simple local dashboard.

This wraps existing durable controllers; it never contacts Tapas itself or
resets recovery limits. Detailed CLI and per-series workflows remain available.
"""
import math
import os
from pathlib import Path
import sqlite3
import time

from . import account_export, batches, completion
from .locking import LibraryLock
from .monitoring import _read, _progress
from .models import ArchiveError
from .osdep import replace_file
from .queue_client import task_status

# The downloader's own pace, for reading the lists; what a download adds to it is the chosen speed below.
DESKTOP_REQUEST_INTERVAL = 1.0
# The speeds a person can choose between. 'fast' is the pace Tapas has let a download keep: the fast importer,
# which sets its own image pace and ignores the two numbers given here. The others are the ordinary downloader
# at its quickest, four images at a time, and at one image a second.
SPEEDS = {
    'fast': {'speed': 'fast', 'download_workers': 3, 'request_interval': 0.25, 'batch_bytes': 8 * 1024**3},
    'medium': {'speed': 'normal', 'download_workers': 4, 'request_interval': 0.25, 'batch_bytes': batches.DEFAULTS['batch_bytes']},
    'slow': {'speed': 'normal', 'download_workers': 1, 'request_interval': 1.0, 'batch_bytes': batches.DEFAULTS['batch_bytes']},
}
DEFAULT_SPEED = 'fast'
SPEED_FILE = 'download-speed'
# The app's own engine (qt_engine), the one its window signs in with, so the app carries no second browser.
# 'firefox' (Playwright, a separate install) is the engine with the longer record against Tapas.
DESKTOP_BROWSER = 'qt'
# A batch that has only just started cannot be told to stop until it has recorded its run; Pause waits this long for that.
PAUSE_WAIT_SECONDS = 5


def speed(workspace):
    """The speed chosen for this library; the fast one until the person picks another."""
    try:
        text = (workspace.session / SPEED_FILE).read_text(encoding='utf-8').strip()
    except (OSError, ValueError):
        return DEFAULT_SPEED
    return text if text in SPEEDS else DEFAULT_SPEED


def choose_speed(workspace, value):
    """Keep the choice with the library's private files. Go applies it."""
    if value not in SPEEDS:
        raise ArchiveError('Comic Archiver does not know that speed.')
    path = workspace.session / SPEED_FILE
    try:
        path.with_name(SPEED_FILE + '.new').write_text(value + '\n', encoding='utf-8')
        replace_file(path.with_name(SPEED_FILE + '.new'), path)
    except OSError:
        raise ArchiveError('Comic Archiver could not save that speed. Check that the library folder can be written to.') from None


def _apply_speed(session, library, pacing):
    """A saved download keeps the pace it was started with, so the present choice is written into it here."""
    root = batches._root(session, library)
    with LibraryLock(root / '.account-export-state'):
        value = account_export._load(root, account_export.lists(session))
        if value is not None and value['status'] != 'finished':
            value['window_budgets'] = batches.budgets(value['window_budgets'] | pacing)
            account_export._save(root, value)
    with LibraryLock(root / '.completion-state'), LibraryLock(root / '.backlog-state'):
        plan = completion._load(root)
        if plan is not None:
            plan['window_budgets'] = batches.budgets(plan['window_budgets'] | pacing)
            completion._save(root, plan)
        window = batches._load(root)
        # A batch that is still out was asked for at the earlier pace and is checked against it when it comes back;
        # the choice then holds from the next round of batches.
        if window is not None and window['pending'] is None:
            window['budgets'] = batches.budgets(window['budgets'] | pacing)
            batches._save(root, window)


def start(workspace):
    session, library = workspace.session, workspace.library
    pacing = SPEEDS[speed(workspace)]
    state = account_export.status(session, library)
    if state.get('status') == 'running':
        return
    held = completion.status(session, library).get('status') in {'paused', 'stopped'}
    window = batches.status(session, library) if held else {}
    if window.get('configured') and _cancelled(session, window.get('pending_task_id')):
        # Read after resuming, that receipt would hold the download again.
        batches.step(session, library, observe_only=True)
    _apply_speed(session, library, pacing)
    if held:
        if window.get('configured'):
            with LibraryLock(batches._root(session, library) / '.backlog-state'):
                batches._start(batches._root(session, library), None, True)
        completion.configure(session, library, resume=True)
    resume = state.get('configured', False) and state.get('status') != 'finished'
    account_export.configure(session, library, resume=resume, window_budgets=pacing)


def _cancelled(session, task):
    """Whether this batch was stopped by Pause or by closing the app and has ended as 'cancelled'."""
    if task is None:
        return False
    try:
        value = task_status(session, task)
    except ArchiveError:
        return False
    result = value.get('result')
    return value['status'] == 'ok' and isinstance(result, dict) and result.get('status') == 'cancelled'


def pause(workspace):
    """Stop further batches and the one in progress, which left alone runs for up to half an hour."""
    session, library = workspace.session, workspace.library
    account_export.request_stop(session, library)
    tasks = {account_export.status(session, library).get('pending_task_id'),
             batches.status(session, library).get('pending_task_id')} - {None}
    deadline = time.monotonic() + PAUSE_WAIT_SECONDS
    for task in sorted(tasks):
        while not _interrupted(session, library, task) and time.monotonic() < deadline:
            time.sleep(0.25)


def interrupt(workspace):
    """Stop whatever the downloader is doing now, for closing the app. The saved download is left as it is."""
    from .monitoring import _read
    session, library = workspace.session, workspace.library
    deadline = time.monotonic() + PAUSE_WAIT_SECONDS
    while True:
        worker = _read(Path(session) / 'worker/status.json')
        task = worker.get('task_id') if worker.get('state') == 'busy' else None
        if not isinstance(task, str) or _interrupted(session, library, task) or time.monotonic() >= deadline:
            return
        time.sleep(0.25)


def reopen(workspace, *, idle=True):
    """What a start of the app does to the saved download; True when one that was running now waits for Go.

    Nothing downloads until the person presses Go, so a download that closing the app or a power cut
    left marked as running is paused. `idle` says that no downloader is running: a task still queued
    from then is ended unrun, or the downloader that starts with the sign-in would work through it.
    """
    from .task_cancellation import end_unstarted
    session, library = workspace.session, workspace.library
    state = account_export.status(session, library)
    if not state.get('configured'):
        return False
    running = state.get('status') == 'running'
    if running:
        account_export.request_stop(session, library)
    if idle:
        for task in sorted({state.get('pending_task_id'), batches.status(session, library).get('pending_task_id')} - {None}):
            end_unstarted(session, library, task)
    elif running:
        pause(workspace)  # A downloader outlived the app: what it is on is stopped as Pause stops it.
    return running


def _interrupted(session, library, task):
    """False only while the task is running and has not accepted a stop yet; the caller asks again."""
    from .cancellation import request_stop
    from .monitoring import _read
    try:
        if task_status(session, task)['status'] != 'running':
            return True
    except ArchiveError:
        return True
    run = _read(Path(library) / '.archive-state/progress.json').get('run_id')
    try:
        return request_stop(library, run).get('status') == 'cancellation_requested'
    except (ArchiveError, OSError, sqlite3.Error):
        return False


def status(workspace, since=None):
    state = account_export.status(workspace.session, workspace.library)
    result = {'supported':True, 'configured':state.get('configured') is True,
              'coverage_verified':False, 'speed':speed(workspace)}
    for key, choices in [('status', account_export.STATES), ('phase', account_export.PHASES),
                         ('code', batches.CODES | completion.REASONS | {'backlog_held', 'controller_failed',
                           'archive_incomplete', 'list_checkpoint_incomplete', 'scan_no_progress', 'task_result_invalid'})]:
        if state.get(key) in choices:
            result[key] = state.get(key)
    checks = state.get('list_checks')
    if isinstance(checks, list):
        categories = account_export.lists(workspace.session)
        checked = min(len(checks), len(categories))
        result['lists'] = {'checked':checked, 'total':len(categories)}
        if state.get('phase') == 'lists' and checked < len(categories):
            result['lists']['current'] = categories[checked]
    # Use the existing durable checkpoint's modification time. This works with
    # resident workers and never fabricates a heartbeat or exposes raw messages.
    now = time.time()
    try:
        started = batches._instant(state.get('started_at')).timestamp()
        if since is not None:
            # The time the run was first started says nothing about how long it has been working now.
            started = max(started, since)
        if started <= now:
            result['elapsed_seconds'] = int(now - started)
            path = workspace.library / '.archive-state/progress.json'
            progress = _progress(_read(path))
            if 'status' in progress:
                changed = path.stat().st_mtime
                if started <= changed <= now:
                    # A step that has ended is not a step that has gone quiet: the page tells the two apart.
                    key = 'progress_age_seconds' if progress['status'] == 'running' else 'between_steps_seconds'
                    result[key] = int(now - changed)
    except (ArchiveError, OSError, TypeError, ValueError, AttributeError):
        pass
    if result.get('configured'):
        try:
            window = batches._load(batches._root(workspace.session, workspace.library, require_queue=False)) or {}
            if result.get('status') == 'running':
                # A wait the download has set itself before it tries again, so the page need not call it a stall.
                for key, reason in (('network_retry_after', 'connection'), ('source_recovery_wait', 'source'),
                                    ('asset_retry_after', 'images')):
                    left = batches._instant(window[key]).timestamp() - now if window.get(key) else 0
                    if left > 0:
                        result['retry'] = {'reason': reason, 'seconds': math.ceil(left)}
            elif window.get('source_retry_after'):
                # The site asked for a wait that Go cannot cut short: the page says how long, not that Go failed.
                left = batches._instant(window['source_retry_after']).timestamp() - now
                if left > 0:
                    result['go_after_seconds'] = math.ceil(left)
        except (ArchiveError, OSError, TypeError, ValueError, AttributeError):
            pass
    # Raw audit/controller receipts can contain source identities and paths.
    # Project only the fixed completion counts needed by the interface.
    audit = state.get('audit')
    if isinstance(audit, dict):
        result['audit'] = {key:audit[key] for key in ('verified_inventory_episode_count',
            'missing_recorded_accessible_episode_count', 'inventory_index_problem_count', 'unindexed_archive_count')
            if type(audit.get(key)) is int and audit[key] >= 0}
    return result
