"""In-process worker host for the Android app (frozen contract; native line implements).

Desktop ``background.start`` spawns a subprocess (``sys.executable -m comic_archive ...``),
which embedded Python must not rely on. The host runs the same worker behaviour in-process
with an explicit session root, so Go-only admission (account_ui.reopen), the
status.json/drain.json protocol and network holds behave exactly as on the desktop.

Workspace rule (reviewed roadmap): explicit roots are preferred over process-global cwd
changes. The beta has exactly one workspace, whose root the bridge sets as the process cwd
once while the worker is stopped; the worker's own queue paths all stay inside the session.
"""
import time
from pathlib import Path

from . import background
from .models import ArchiveError
from .state_paths import SESSION_NAME

_thread = {"value": None}


def android_adapter_factory(cookie_source, cookie_sink=None, *, site_key='tapas'):
    """One shared reader per worker start, with an explicitly selected site's live store.

    The full worker surface — the fail-closed who-am-i admission, library discovery and
    prefetch — over one canonical transport. The app's store object itself takes the
    AndroidTransport route (the request path the metadata pipeline's asset reader needs);
    plain callables, as the tests register them, take the bare Transport seams. No
    separate Android get loop is constructed here; that duplication is what this avoids.
    """
    if not callable(getattr(cookie_source, "headerFor", None)) and not callable(cookie_source):
        raise ArchiveError("The Android reader needs the app sign-in store or its live transport.")
    if callable(cookie_source) and cookie_sink is not None and not callable(cookie_sink):
        raise ArchiveError("The WebView cookie seams must be callables supplied by the app.")

    from . import sites
    if not isinstance(site_key, str) or sites.find(site_key) is None:
        raise ArchiveError("Choose a supported site before starting the worker.")

    def build():
        from .android_readers import reader
        return reader(site_key, cookie_source, cookie_sink)

    return build


def status(session_root) -> dict:
    """Worker status through the existing status.json/.runtime protocol."""
    return background.status(Path(session_root))


def request_stop(session_root, *, wait_seconds: float = 0) -> dict:
    """Cooperative drain request; the one stop path for user Stop and service onTimeout."""
    return background.stop(Path(session_root), wait_seconds=wait_seconds)


def start_in_process(session_root, library_root, cookie_source, cookie_sink, network_status_path) -> dict:
    """Start the embedded worker against explicit roots (native line, before the integration
    build). ``cookie_source`` is the app's store object (the AndroidTapasAdapter route with
    the asset reader) or the live per-URL provider, with ``cookie_sink`` then the own-origin
    Set-Cookie forwarder (see Transport.cookie_source / Transport.cookie_sink); the platform
    store stays the single cookie authority. ``network_status_path`` feeds
    comic_archive.network_android. Only a whole Android sign-in record in the session can
    start the downloader, and Go-only admission is enforced by prepare_workspace's reopen."""
    import threading

    from . import android_session, network_android, sites
    from .locking import LibraryLock

    session = Path(session_root).absolute()
    library = Path(library_root).absolute()
    if session.name != SESSION_NAME or library.name != 'Library' or library.parent != session.parent:
        raise ArchiveError("The worker runs on the workspace's own session and library roots.")
    if not android_session.is_signed_in(session):
        raise ArchiveError("Sign in before starting the downloader.")
    if android_session.signed_in_site(session).key != sites.DEFAULT.key:
        raise ArchiveError("The Android beta downloads from Tapas only.")
    network_android.configure(network_status_path)
    factory = android_adapter_factory(cookie_source, cookie_sink)
    background.root(session, create=True)  # Name and symlink validation, as the desktop path does.
    with LibraryLock(library / '.launcher'):
        current = background.status(session)
        if current['running']:
            return current | {'started': False}
        thread = threading.Thread(target=_worker_main, args=(session, factory),
                                  name='comic-archiver-worker', daemon=True)
        _thread['value'] = thread
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            observed = background.status(session)
            if observed['state'] in {'ready', 'busy'}:
                return observed | {'started': True}
            if not thread.is_alive():
                break  # A start that already ended reports itself through status.json.
            time.sleep(0.25)
    return background.status(session) | {'started': False}


def _worker_main(session, factory):
    from .worker import run_worker
    try:
        run_worker(session, adapter_factory=factory)
    except ArchiveError:
        pass  # status.json already says startup_failed in its public-safe words.
    except Exception as error:
        # Raw frames stay off the bridge; the app reads worker_status, whose codes are public.
        print(f"Embedded worker ended unexpectedly ({type(error).__name__}).", flush=True)
