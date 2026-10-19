"""Start one fixed headless worker and stop it cooperatively between tasks."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import osdep
from .archive import atomic_json
from .archive_paths import directory
from .locking import LibraryLock
from .models import ArchiveError
from .state_paths import SESSION_NAME


def root(session, create=False):
    session = Path(session).absolute()
    if session.name != SESSION_NAME or any(p.is_symlink() for p in (session, *session.parents)):
        raise ArchiveError("Background mode uses the project's ordinary .comic-session directory.")
    result = session / "worker"
    if any(p.is_symlink() for p in (result, *result.parents)):
        raise ArchiveError("Worker paths must not be symlinked.")
    if create:
        directory(result).chmod(0o700)
    return result


def read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError("Worker control files must not be symlinked.")
    if not path.exists():
        return {}
    try:
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError, RecursionError):
        raise ArchiveError("Worker control state is invalid; no process was stopped.") from None


def running(folder, runtime='.runtime'):
    """Use the runtime lock, not a possibly stale status file or recycled PID."""
    if runtime not in {'.runtime','.backlog-runtime'}:
        raise ArchiveError('Unsupported worker runtime lock.')
    path = folder / runtime / ".archive-state/writer.lock"
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError("Worker runtime lock must not be symlinked.")
    try:
        descriptor = os.open(path, os.O_RDONLY | osdep.NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            osdep.lock(descriptor)
        except BlockingIOError:
            return True
        osdep.unlock(descriptor)
        return False
    finally:
        os.close(descriptor)


def drain_requested(folder, worker_id):
    return read(folder / "drain.json").get("worker_id") == worker_id


def status(session):
    folder = root(session)
    active = running(folder)
    observed = read(folder / "status.json")
    state = observed.get("state")
    if state not in {"starting", "ready", "busy", "stopped"}:
        state = "unknown"
    worker_id = observed.get("worker_id")
    supported = (type(observed.get("worker_version")) is int and observed["worker_version"] >= 36
                 and isinstance(worker_id, str) and bool(re.fullmatch(r"[a-f0-9]{32}", worker_id)))
    legacy_claim = not supported and state in {"starting", "ready", "busy"}
    result = {"running": True if active else None if legacy_claim else False,
              "state": state if active or legacy_claim else "stopped",
              "liveness": "runtime_lock" if active else "legacy_status_only" if legacy_claim else "stopped",
              "headless": observed.get("headless") if type(observed.get("headless")) is bool else None,
              "stop_supported": supported,
              "stop_requested": bool(supported and drain_requested(folder, worker_id))}
    if type(observed.get("worker_version")) is int:
        result["worker_version"] = observed["worker_version"]
    if isinstance(observed.get("task_id"), str) and re.fullmatch(r"job-[a-f0-9]{24}", observed["task_id"]):
        result["task_id"] = observed["task_id"]
    if observed.get("last_error_code") in {"startup_failed"}:
        result["last_error_code"] = observed["last_error_code"]
    return result


def stop(session,*,wait_seconds=0):
    if type(wait_seconds) not in (int,float) or not math.isfinite(wait_seconds) or not 0<=wait_seconds<=7500:
        raise ArchiveError('Worker drain wait must be from 0 to 7500 seconds.')
    from .task_cancellation import admission_lock
    folder = root(session)
    current = status(session)
    if current['running'] is False:
        return current
    if not current['stop_supported']:
        raise ArchiveError("This older worker cannot stop through background controls. Let its batch finish, then stop it in its original terminal.")
    with admission_lock(Path(session)):
        observed = read(folder / "status.json")
        current = status(session)
        if current["running"] is False:
            return current
        if not current["stop_supported"]:
            raise ArchiveError("This older worker cannot stop through background controls. Let its batch finish, then stop it in its original terminal.")
        path = folder / "drain.json"
        atomic_json(path, {"worker_id": observed["worker_id"],
                           "requested_at": datetime.now(timezone.utc).isoformat()})
        path.chmod(0o600)
    current=status(session)
    deadline=time.monotonic()+wait_seconds
    while wait_seconds and current['running'] is not False:
        remaining=deadline-time.monotonic()
        if remaining<=0:return current|{'stop_wait_expired':True}
        time.sleep(min(1,remaining));current=status(session)
    return current


def start(session, browser="firefox", interval=1.0, *, restart=False):
    from .downloads import validate_request_interval
    validate_request_interval(interval)
    if browser not in {"auto", "firefox", "chromium", "qt"} or (type(interval) not in (int, float)
            or type(restart) is not bool):
        raise ArchiveError("Use a supported browser and explicit worker options.")
    folder = root(session, create=True)
    with LibraryLock(folder / ".launcher"):
        current = status(session)
        if current['running'] is True and restart:
            stop(session)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = status(session)
                if current['running'] is False:
                    break
                time.sleep(.1)
            else:
                return current | {'start_deferred':True,'reason':'worker_draining'}
        if current['running'] is True:
            if current['stop_requested']:
                return current | {'start_deferred':True,'reason':'worker_draining'}
            return current | {"already_running": True}
        if current['running'] is None:
            return current | {"start_deferred": True, "reason": "legacy_worker_reported_active"}
        from .session import load_session
        load_session(folder.parent)
        log = folder / "background.log"
        if log.is_symlink():
            raise ArchiveError("Worker log must not be symlinked.")
        descriptor = osdep.open_log(log)
        command = [sys.executable, "-m", "comic_archive", "--session-dir", str(folder.parent),
                   "--browser", browser, "--headless", "--request-interval", str(interval),
                   "browser-worker"]
        try:
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                osdep.private_file(output.fileno())
                process = subprocess.Popen(command, cwd=folder.parent.parent,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    **osdep.detached())
        except OSError:
            raise ArchiveError("Cannot start the background worker; check project permissions.") from None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return status(session) | {"start_failed": True}
            observed = read(folder / "status.json")
            if observed.get("pid") == process.pid and observed.get("state") in {"ready", "busy"}:
                return status(session) | {"already_running": False}
            time.sleep(.1)
        return status(session) | {"startup_pending": True}
