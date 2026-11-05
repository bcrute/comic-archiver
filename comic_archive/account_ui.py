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
# The speeds a person can choose between. 'fast' is the pace Tapas has let a download keep: the fast importer,
# which sets its own image pace and ignores the two numbers given here. The others are the ordinary downloader
# at its quickest, four images at a time, and at one image a second.
SPEEDS = {
    'fast': {'speed': 'fast', 'download_workers': 3, 'request_interval': 0.25, 'batch_bytes': 8 * 1024**3},
    'medium': {'speed': 'normal', 'download_workers': 4, 'request_interval': 0.25, 'batch_bytes': 1024**3},
    'slow': {'speed': 'normal', 'download_workers': 1, 'request_interval': 1.0, 'batch_bytes': 1024**3},
}
DEFAULT_SPEED = 'fast'
SPEED_FILE = 'download-speed'
# The app's own engine (qt_engine), the one its window signs in with, so the app carries no second browser.
# 'firefox' (Playwright, a separate install) is the engine with the longer record against Tapas.
DESKTOP_BROWSER = 'qt'
# A batch that has only just started cannot be told to stop until it has recorded its run; Pause waits this long for that.
PAUSE_WAIT_SECONDS = 5
PHASES = ('existing_backlog', 'lists', 'series', 'archive', 'verify', 'finished')


def speed(workspace):
    """The speed chosen for this library; the fast one until the person picks another."""
    try:
        # Never through a link: a library folder from elsewhere could hold one here.
        flags = os.O_RDONLY | NOFOLLOW | NONBLOCK | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(workspace.session / SPEED_FILE, flags), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return DEFAULT_SPEED
            text = stream.read(64).decode('utf-8').strip()
    except (OSError, ValueError):
        return DEFAULT_SPEED
    return text if text in SPEEDS else DEFAULT_SPEED


def choose_speed(workspace, value):
    """Keep the choice with the library's private files. Go applies it."""
    if value not in SPEEDS:
        raise ArchiveError('Comic Archiver does not know that speed.')
    path = workspace.session / SPEED_FILE
    temporary = path.with_name(SPEED_FILE + '.new')
    try:
        # Made afresh, never through a link: a library folder from elsewhere could hold one at either name.
        with os.fdopen(open_exclusive(temporary), 'w', encoding='utf-8') as stream:
            stream.write(value + '\n')
        replace_file(temporary, path)
    except OSError:
        raise ArchiveError('Comic Archiver could not save that speed. Check that the library folder can be written to.') from None


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


def status(workspace, since=None):
    result = {'supported': True, 'configured': False, 'coverage_verified': False, 'speed': speed(workspace)}
    try:
        state = download_control.status(workspace.session, workspace.library)
    except ArchiveError:
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
    # The clock counts the present stretch of work, from the last Go (or the window's own start).
    now = time.time()
    started = state.get('started_at')
    if type(started) is float:
        if since is not None:
            started = max(started, since)
        if started <= now:
            result['elapsed_seconds'] = int(now - started)
            try:
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
