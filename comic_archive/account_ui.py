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
from .messages import say

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
        raise ArchiveError(say('account.comic_archiver_does_not_know_that'))


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
        raise ArchiveError(say('account.comic_archiver_could_not_save_that')) from None



def change_speed(workspace, value, *, closing, on_stop_requested=None):
    """Change this site's pace, gracefully replacing only a task that captured its old pace."""
    _check_speed(value)
    if value == speed(workspace) and not closing.is_set():
        return
    _change_setting(workspace, lambda: choose_speed(workspace, value), closing=closing,
                    on_stop_requested=on_stop_requested)


def download_types(workspace):
    from .download_selection import TYPES, read
    from .inventory import Inventory
    if not (workspace.library / '.archive-state/inventory.sqlite3').is_file():
        return dict.fromkeys(TYPES, True)
    with Inventory(workspace.library, read_only=True) as inventory:
        return read(inventory.db)


def download_extras(workspace):
    """The extras this site shows beside an episode, each with whether it is saved: {} for a site with none."""
    from . import extras, sites
    from .inventory import Inventory
    offered = extras.offered(sites.of_session(workspace.session).key)
    chosen = dict.fromkeys(extras.KINDS, False)
    if offered and (workspace.library / '.archive-state/inventory.sqlite3').is_file():
        with Inventory(workspace.library, read_only=True) as inventory:
            chosen = extras.read(inventory.db)
    return {kind: chosen[kind] for kind in offered}


def change_download_types(workspace, value, *, closing, on_stop_requested=None):
    from .download_selection import save, validate
    from .inventory import Inventory
    value = validate(value)

    def save_choices():
        with Inventory(workspace.library) as inventory:
            save(inventory.db, value)

    _change_setting(workspace, save_choices, closing=closing, selection=True, on_stop_requested=on_stop_requested)


def change_comics(workspace, chosen, series_id=None, *, closing, on_stop_requested=None, episode_id=None):
    from . import comic_selection
    from .discovery import observed_id
    from .inventory import Inventory
    if (type(chosen) is not bool or series_id is not None and not observed_id(series_id)
            or episode_id is not None and (not observed_id(episode_id) or series_id is None)):
        raise ArchiveError('Choose a recorded comic and an explicit download choice.')

    def save_choices():
        with Inventory(workspace.library) as inventory:
            if episode_id is None:
                comic_selection.change(inventory.db, chosen, series_id)
            else:
                comic_selection.change_episode(inventory.db, chosen, series_id, episode_id)

    _change_setting(workspace, save_choices, closing=closing, selection=True, on_stop_requested=on_stop_requested)


def change_library_comics(base, chosen, provider=None, series_id=None, *, closing, interrupt=None, episode_id=None):
    """Apply Library checkboxes through each provider's existing pause/change protocol."""
    from . import sites
    from .discovery import observed_id
    from .workspace import Workspace
    site = sites.find(provider) if isinstance(provider, str) else None
    if (type(chosen) is not bool or provider is not None and (site is None or not site.available)
            or series_id is not None and (site is None or not observed_id(series_id))
            or episode_id is not None and (series_id is None or not observed_id(episode_id))):
        raise ArchiveError('Choose a provider, a recorded comic and an explicit download choice.')
    applied = []
    for selected in ([site] if site is not None else [entry for entry in sites.SITES if entry.available]):
        workspace = Workspace(id='default', root=sites.folder(Path(base), selected))
        with LibraryLock(workspace.session / '.account-ui'):
            callback = interrupt if selected == sites.DEFAULT else None
            options = {'episode_id': episode_id} if episode_id is not None else {}
            change_comics(workspace, chosen, series_id, closing=closing, on_stop_requested=callback, **options)
        applied.append(selected.key)
    return applied


def _change_setting(workspace, save_choice, *, closing, selection=False, on_stop_requested=None):
    """on_stop_requested is called once the running step has been asked to stop, as for pause: the phone ends
    the page read its browser is in the middle of, which would otherwise outlast the wait."""
    from .errors import LibraryBusy, SpeedChangeError, DownloadSelectionChangeError
    error_type = DownloadSelectionChangeError if selection else SpeedChangeError
    if closing.is_set():
        raise error_type('closing')
    asked = []

    def wait(task):
        from . import background
        from .task_cancellation import admission_lock, end_unstarted, request_running_stop
        deadline = time.monotonic() + SPEED_CHANGE_WAIT_SECONDS
        while not closing.is_set():
            try:
                # Stop queued work under the same lock the worker takes before starting it.
                request_running_stop(workspace.session, task, include_queued=True)
                if on_stop_requested is not None and not asked:
                    asked.append(True)
                    on_stop_requested()
                _interrupted(workspace.session, workspace.library, task)
                # No downloader runs: one ended with the app (as the phone's is when the app is closed mid-step)
                # leaves its last word, busy, behind. Nothing will answer the step or say it is ready, so the
                # step is ended unrun, as at a start of the app, instead of being waited for until the change fails.
                idle = not background.running(background.root(workspace.session))
                if idle:
                    end_unstarted(workspace.session, workspace.library, task)
                with admission_lock(workspace.session):
                    response = task_status(workspace.session, task)
                    worker = _read(workspace.session / 'worker/status.json')
                    if response['status'] in ('ok', 'error') and (idle or worker.get('state') == 'ready'):
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


def start(workspace, *, list_only=False):
    """Go: the download continues from where it stopped, or starts; the chosen speed holds from its next step."""
    if list_only:
        download_control.start(workspace.session, workspace.library, list_only=True)
    else:
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
        status = task_status(session, task)['status']
        if status == 'queued':
            # Sent but not taken up yet: stopped before a downloader starts it.
            request_running_stop(session, task, include_queued=True)
        if status != 'running':
            return True
    except (ArchiveError, OSError):
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
              'download_types': None, 'download_extras': {}}
    try:
        result['download_types'] = download_types(workspace)
        result['download_extras'] = download_extras(workspace)
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
