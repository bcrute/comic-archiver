"""Fixed account-wide actions for the apps: Go, Pause, the speed, a start of the app, and what they see.

The download itself is download_control's; this never contacts a site itself or decides what is next.
"""
import os
from pathlib import Path
import sqlite3
import stat
import time

from . import download_control
from .archive import atomic_json
from .locking import LibraryLock
from .monitoring import _read, _progress
from .models import ArchiveError
from .osdep import NOFOLLOW, NONBLOCK, open_exclusive, replace_file
from .queue_client import task_status

# The downloader's own pace, for reading the lists; what a download adds to it is the chosen speed below.
DESKTOP_REQUEST_INTERVAL = 1.0
# Named speeds select fixed worker policies. Medium and Slow admit approximately
# half and one quarter of Fast's aggregate image requests and concurrent reads.
SPEEDS = {
    'fast': {'speed': 'fast', 'download_workers': 3, 'request_interval': 0.25, 'batch_bytes': 8 * 1024**3},
    'medium': {'speed': 'medium', 'download_workers': 4, 'request_interval': 0.5, 'batch_bytes': 1024**3},
    'slow': {'speed': 'slow', 'download_workers': 2, 'request_interval': 1.0, 'batch_bytes': 1024**3},
}
DEFAULT_SPEED = 'fast'
SPEED_FILE = 'download-speed'
# The app's own engine (qt_engine), the one its window signs in with, so the app carries no second browser.
# 'firefox' (Playwright, a separate install) is the engine with the longer record against Tapas.
DESKTOP_BROWSER = 'qt'
# A batch that has only just started cannot be told to stop until it has recorded its run; Pause waits this long for that.
PAUSE_WAIT_SECONDS = 5
SPEED_CHANGE_WAIT_SECONDS = 30
PHASES = ('existing_backlog', 'lists', 'series', 'archive', 'verify', 'finished')


def speed(workspace):
    """The speed chosen for this library; the fast one until the person picks another."""
    try:
        # Never through a link: a library folder from elsewhere could hold one here.
        path = workspace.session / SPEED_FILE
        original = path.lstat()
        if not stat.S_ISREG(original.st_mode):
            return DEFAULT_SPEED
        flags = os.O_RDONLY | NOFOLLOW | NONBLOCK | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino):
                return DEFAULT_SPEED
            text = stream.read(64).decode('utf-8').strip()
    except (OSError, ValueError):
        return DEFAULT_SPEED
    return text if text in SPEEDS else DEFAULT_SPEED


def _check_speed(value):
    """Refuse unsupported choices before touching a saved download."""
    if value not in SPEEDS:
        raise ArchiveError('Comic Archiver does not know that speed.')


def choose_speed(workspace, value):
    """Keep the choice with the library's private files; newly reserved steps apply it."""
    _check_speed(value)
    path = workspace.session / SPEED_FILE
    temporary = path.with_name(SPEED_FILE + '.new')
    try:
        # Made afresh, never through a link: a library folder from elsewhere could hold one at either name.
        with os.fdopen(open_exclusive(temporary), 'w', encoding='utf-8') as stream:
            stream.write(value + '\n')
        replace_file(temporary, path)
    except OSError:
        raise ArchiveError('Comic Archiver could not save that speed. Check that the library folder can be written to.') from None



def change_speed(workspace, value, *, closing):
    """Change this site's pace, gracefully replacing only a task that captured its old pace."""
    _check_speed(value)
    if value == speed(workspace) and not closing.is_set():
        return
    _change_setting(workspace, lambda: choose_speed(workspace, value), closing=closing)


def download_types(workspace):
    from .download_selection import TYPES, read
    from .inventory import Inventory
    if not (workspace.library / '.archive-state/inventory.sqlite3').is_file():
        return dict.fromkeys(TYPES, True)
    with Inventory(workspace.library, read_only=True) as inventory:
        return read(inventory.db)


def change_download_types(workspace, value, *, closing):
    from .download_selection import save, validate
    from .inventory import Inventory
    value = validate(value)

    def save_choices():
        with Inventory(workspace.library) as inventory:
            save(inventory.db, value)

    _change_setting(workspace, save_choices, closing=closing, selection=True)


def _change_setting(workspace, save_choice, *, closing, selection=False):
    from .errors import LibraryBusy, SpeedChangeError, DownloadSelectionChangeError
    error_type = DownloadSelectionChangeError if selection else SpeedChangeError
    if closing.is_set():
        raise error_type('closing')

    def wait(task):
        from .task_cancellation import admission_lock, request_running_stop
        deadline = time.monotonic() + SPEED_CHANGE_WAIT_SECONDS
        while not closing.is_set():
            try:
                # Stop queued work under the same lock the worker takes before starting it.
                request_running_stop(workspace.session, task, include_queued=True)
                _interrupted(workspace.session, workspace.library, task)
                with admission_lock(workspace.session):
                    response = task_status(workspace.session, task)
                    worker = _read(workspace.session / 'worker/status.json')
                    if response['status'] in ('ok', 'error') and worker.get('state') == 'ready':
                        with LibraryLock(workspace.library):
                            return response
            except LibraryBusy:
                pass  # Admission or writer cleanup is still in progress.
            if time.monotonic() >= deadline:
                raise error_type('stopping')
            time.sleep(0.25)
        raise error_type('closing')

    def save():
        try:
            save_choice()
        except (ArchiveError, OSError):
            raise error_type('save') from None

    try:
        download_control.change_speed(workspace.session, workspace.library,
                                      save, wait, lambda: not closing.is_set(), **({'selection': True} if selection else {}))
    except (LibraryBusy, SpeedChangeError, DownloadSelectionChangeError):
        raise
    except (ArchiveError, OSError, sqlite3.Error):
        raise error_type('unavailable') from None


def start(workspace):
    """Go: the download continues from where it stopped, or starts; the chosen speed holds from its next step."""
    download_control.start(workspace.session, workspace.library)


def pause(workspace, *, on_stop_requested=None):
    """Stop further steps and the one in progress, which left alone runs for up to half an hour."""
    session, library = workspace.session, workspace.library
    task = download_control.outstanding(session, library)
    download_control.request_stop(session, library)
    if on_stop_requested is not None:
        on_stop_requested()
    if task is None:
        return
    deadline = time.monotonic() + PAUSE_WAIT_SECONDS
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
    going = download_control.reopen(workspace.session, workspace.library, idle=idle)
    if going and not idle:
        pause(workspace)  # A downloader outlived the app: what it is on is stopped as Pause stops it.
    return going


def _interrupted(session, library, task):
    """False only while the task is running and has not accepted a stop yet; the caller asks again."""
    from .cancellation import request_stop
    from .monitoring import _read
    from .task_cancellation import request_running_stop
    try:
        if task_status(session, task)['status'] != 'running':
            return True
    except ArchiveError:
        return True
    try:
        # Left with the task first: a batch still getting ready stops as soon as it records its run.
        if request_running_stop(session, task)['status'] != 'cancellation_requested':
            return True
    except (ArchiveError, OSError):
        return False
    run = _read(Path(library) / '.archive-state/progress.json').get('run_id')
    try:
        # A run that has ended is an earlier one, or this task's own; either way the stop left above covers it.
        stopped = request_stop(library, run)
        return stopped.get('status') == 'cancellation_requested' or stopped.get('stop_requested') is False
    except (ArchiveError, OSError, sqlite3.Error):
        return False


def _transfer(workspace, state, progress, path, now):
    """Count completed batches even when several finish between dashboard refreshes.

    Only this outstanding task's checkpoint may supplement its completed receipts.
    Nothing here reads archives, recounts the library or contacts a provider.
    """
    counter = state.get('_transfer') or {}
    total, scope = counter.get('completed_bytes'), counter.get('scope')
    if type(total) is not int or total < 0 or type(scope) is not float or not 0 < scope <= now:
        return None
    task = counter.get('task') or {}
    saved = progress.get('downloaded_bytes')
    if task.get('id'):
        worker = _read(workspace.session / 'worker/status.json')
        try:
            if worker.get('task_id') == task['id']:
                if not task['published_at'] <= path.stat().st_mtime <= now:
                    saved = None
            else:
                # The worker may publish its receipt before the controller settles it. Count that exact
                # task's receipt during the handoff instead of momentarily dropping its transferred bytes.
                receipt = task_status(workspace.session, task['id'])
                result = receipt.get('result')
                saved = result.get('downloaded_bytes') if isinstance(result, dict) else None
            if type(saved) is int and saved >= 0:
                total += saved
        except (ArchiveError, OSError, KeyError, TypeError):
            pass
    return {'scope': scope, 'downloaded_bytes': total}


def status(workspace, since=None):
    result = {'supported': True, 'configured': False, 'coverage_verified': False, 'speed': speed(workspace),
              'download_types': None}
    try:
        result['download_types'] = download_types(workspace)
        state = download_control.status(workspace.session, workspace.library)
    except (ArchiveError, OSError, sqlite3.Error):
        state = {'configured': True, 'status': 'held', 'phase': 'lists', 'code': 'controller_failed'}
    if not state.get('configured'):
        return result
    result['configured'] = True
    # Only the fixed words and counts the apps know reach them; nothing a receipt carried in passes through.
    for key, choices in (('status', ('running', 'held', 'stopped', 'finished')), ('phase', PHASES),
                         ('code', download_control.HOLDS)):
        if state.get(key) in choices:
            result[key] = state[key]
    for key in ('lists', 'retry', 'go_after_seconds'):
        if key in state:
            result[key] = state[key]
    if isinstance(state.get('audit'), dict):
        result['audit'] = {key: state['audit'][key] for key in download_control.AUDIT_KEYS
                           if type(state['audit'].get(key)) is int and state['audit'][key] >= 0}
    purchase = state.get('purchase_history')
    if isinstance(purchase, dict) and purchase.get('status') in ('pending', 'partial', 'checked'):
        from .purchase_history import CODES
        public = {'status': purchase['status']}
        code = purchase.get('code')
        if code is None or isinstance(code, str) and code in CODES | {'history_checkpoint_unreadable'}:
            public['code'] = code
        public.update({key: purchase[key] for key in ('pages', 'discovered', 'unresolved')
                       if type(purchase.get(key)) is int and purchase[key] >= 0})
        result['purchase_history'] = public
    # The clock counts the present stretch of work, from the last Go (or the window's own start).
    now = time.time()
    path = workspace.library / '.archive-state/progress.json'
    progress = _progress(_read(path))
    transfer = _transfer(workspace, state, progress, path, now)
    if transfer is not None:
        result['transfer'] = transfer
    started = state.get('started_at')
    if type(started) is float:
        if since is not None:
            started = max(started, since)
        if started <= now:
            result['elapsed_seconds'] = int(now - started)
            try:
                if state.get('phase') == 'verify':
                    worker = _read(workspace.session / 'worker/status.json')
                    audit_path = workspace.session / 'worker/progress.json'
                    audit = _read(audit_path)
                    if (worker.get('state') == 'busy' and worker.get('task_id')
                            and worker['task_id'] == audit.get('task_id') and audit.get('phase') == 'verify'):
                        path = audit_path
                        progress = _progress(audit)
                if 'status' in progress:
                    changed = path.stat().st_mtime
                    if started <= changed <= now:
                        # A step that has ended is not a step that has gone quiet: the page tells the two apart.
                        key = 'progress_age_seconds' if progress['status'] == 'running' else 'between_steps_seconds'
                        result[key] = int(now - changed)
            except (ArchiveError, OSError, TypeError, ValueError, AttributeError):
                pass
    return result


def repair_android_list_reads(workspace):
    """Once per phone library: lists the prototype's reader recorded as empty are read again at this Go."""
    root = download_control._root(workspace.session, workspace.library)
    marker = workspace.session / 'android-reader-version.json'
    with LibraryLock(root / '.android-reader-upgrade'):
        if not marker.exists():
            download_control.forget_empty_list_reads(workspace.session, workspace.library)
            atomic_json(marker, {'version': 1})
            marker.chmod(0o600)


def source_recovery(session, library):
    """The reader retry the download is waiting on, for the monitor; None when there is none."""
    try:
        return download_control.source_recovery(session, library)
    except ArchiveError:
        return None
