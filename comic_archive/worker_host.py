"""In-process worker host for the Android app (frozen contract; native line implements).

Desktop ``background.start`` spawns a subprocess (``sys.executable -m comic_archive ...``),
which embedded Python must not rely on. The host runs the same worker behaviour in-process
with an explicit session root, so Go-only admission (account_ui.reopen), the
status.json/drain.json protocol and network holds behave exactly as on the desktop.

Workspace rule (reviewed roadmap): explicit roots are preferred over process-global cwd
changes. If a cwd is used temporarily, at most one workspace is active at a time, the worker
stops before switching, and actual path isolation is part of acceptance.
"""
from pathlib import Path

from . import background


def status(session_root) -> dict:
    """Worker status through the existing status.json/.runtime protocol."""
    return background.status(Path(session_root))


def request_stop(session_root, *, wait_seconds: float = 0) -> dict:
    """Cooperative drain request; the one stop path for user Stop and service onTimeout."""
    return background.stop(Path(session_root), wait_seconds=wait_seconds)


def start_in_process(session_root, library_root, cookie_source, cookie_sink, network_status_path) -> dict:
    """Start the embedded worker against explicit roots (native line, before the integration
    build). ``cookie_source`` is the live per-URL provider and ``cookie_sink`` the own-origin
    Set-Cookie forwarder (see Transport.cookie_source / Transport.cookie_sink); the platform
    store stays the single cookie authority. ``network_status_path`` feeds
    comic_archive.network_android. Raises NotImplementedError until the wiring lands with its
    fixtures."""
    raise NotImplementedError("Native line: in-process worker start lands with the integration build.")
