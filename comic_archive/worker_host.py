"""In-process worker host for the Android app (frozen contract; native line implements).

Desktop ``background.start`` spawns a subprocess (``sys.executable -m comic_archive ...``),
which embedded Python must not rely on. The host runs the same worker behaviour in-process
with an explicit session root, so Go-only admission (account_ui.reopen), the
status.json/drain.json protocol and network holds behave exactly as on the desktop.

Workspace rule (reviewed roadmap): explicit roots are preferred over process-global cwd
changes. One workspace is active, whose root the bridge sets as the process cwd only
after the former worker has fully stopped; queue paths stay inside their own session.
"""
import time
import threading
from pathlib import Path

from . import background
from .models import ArchiveError
from .state_paths import SESSION_NAME

_thread = {"value": None, "session": None, "browser_factory": None}
_launch_lock = threading.RLock()


def android_adapter_factory(cookie_source, cookie_sink=None, *, site_key='tapas', library_root=None,
                            browser_factory=None):
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
        from .network import NetworkGuard
        guard = NetworkGuard(Path(library_root)) if library_root is not None else None
        if site_key == 'tapas' and browser_available(browser_factory):
            if library_root is None:
                raise ArchiveError('The native browser needs its selected library.')
            from .android_browser import AndroidTapasBrowserAdapter
            return AndroidTapasBrowserAdapter(Path(library_root).parent / SESSION_NAME,
                browser_factory, network_guard=guard)
        return reader(site_key, cookie_source, cookie_sink, network_guard=guard)

    return build


def browser_available(factory):
    if factory is None:
        return False
    try:
        return factory.available() is True
    except Exception:
        return False


def status(session_root) -> dict:
    """Worker status through the existing status.json/.runtime protocol."""
    session = Path(session_root).absolute()
    observed = background.status(session)
    thread = _thread['value']
    if (_thread['session'] == session and thread is not None and thread.is_alive()
            and not observed['running']):
        return observed | {'running': True, 'state': 'stopping' if observed.get('stop_requested')
                           or observed.get('state') in {'stopped', 'startup_failed'} else 'starting'}
    return observed


def request_stop(session_root, *, wait_seconds: float = 0) -> dict:
    """Cooperative drain request; the one stop path for user Stop and service onTimeout."""
    session = Path(session_root).absolute()
    deadline = time.monotonic() + wait_seconds
    with _launch_lock:
        thread = _thread['value'] if _thread['session'] == session else None
        if thread is None or not thread.is_alive():
            return background.stop(session, wait_seconds=wait_seconds)
        background.stop(session, wait_seconds=0)  # Durable drain precedes renderer interruption.
        factory = _thread.get('browser_factory')
        if factory is not None:
            from .android_browser import interrupt_contexts
            interrupt_contexts(factory)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=max(0, deadline - time.monotonic()))
        return status(session)


def start_in_process(session_root, library_root, cookie_source, cookie_sink, network_status_path,
                     *, browser_factory=None) -> dict:
    """Start the embedded worker against explicit roots (native line, before the integration
    build). ``cookie_source`` is the app's store object (the AndroidTapasAdapter route with
    the asset reader) or the live per-URL provider, with ``cookie_sink`` then the own-origin
    Set-Cookie forwarder (see Transport.cookie_source / Transport.cookie_sink); the platform
    store stays the single cookie authority. ``network_status_path`` feeds
    comic_archive.network_android. Only a whole Android sign-in record in the session can
    start the downloader, and Go-only admission is enforced by prepare_workspace's reopen."""
    from . import android_session, network_android, sites
    from .locking import LibraryLock

    session = Path(session_root).absolute()
    library = Path(library_root).absolute()
    if session.name != SESSION_NAME or library.name != 'Library' or library.parent != session.parent:
        raise ArchiveError("The worker runs on the workspace's own session and library roots.")
    if not android_session.is_signed_in(session):
        raise ArchiveError("Sign in before starting the downloader.")
    site = android_session.signed_in_site(session)
    if sites.find(site.key) != site:
        raise ArchiveError("Choose a supported site before starting the downloader.")
    if Path(network_status_path).absolute() != session.parent / 'net.json':
        raise ArchiveError("The connection report belongs to this workspace.")
    background.root(session, create=True)  # Name and symlink validation, as the desktop path does.
    with _launch_lock, LibraryLock(library / '.launcher'):
        previous = _thread['value']
        if previous is not None and previous.is_alive() and _thread['session'] != session:
            raise ArchiveError("Stop the previous site's downloader before starting another.")
        current = status(session)
        if current['running']:
            return current | {'started': False}
        factory = android_adapter_factory(cookie_source, cookie_sink, site_key=site.key, library_root=library,
            browser_factory=browser_factory)
        fast_factory = None
        if site == sites.DEFAULT and browser_factory is not None:
            from .android_browser import AndroidFastWorkerSession
            from .network import NetworkGuard
            fast_factory = lambda: AndroidFastWorkerSession(session, browser_factory, NetworkGuard(library))
        network_android.configure(network_status_path)
        thread = threading.Thread(target=_worker_main, args=(session, factory, fast_factory),
                                  name='comic-archiver-worker', daemon=True)
        _thread['value'] = thread
        _thread['session'] = session
        _thread['browser_factory'] = browser_factory if site == sites.DEFAULT else None
        # The embedded worker shares the app's pid, so only its fresh worker_id tells its
        # status apart from an interrupted run's leftover "busy" once it holds the lock.
        status_file = background.root(session) / 'status.json'
        previous_id = background.read(status_file).get('worker_id')
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            fresh = background.read(status_file).get('worker_id') != previous_id
            observed = background.status(session)
            if fresh and observed['state'] in {'ready', 'busy'}:
                return observed | {'started': True}
            if not thread.is_alive():
                break  # A start that already ended reports itself through status.json.
            time.sleep(0.25)
    return status(session) | {'started': False}


def _worker_main(session, factory, fast_factory=None):
    from .worker import run_worker
    try:
        options = {'fast_session_factory': fast_factory} if fast_factory is not None else {}
        run_worker(session, adapter_factory=factory, **options)
    except ArchiveError:
        pass  # status.json already says startup_failed in its public-safe words.
    except Exception as error:
        # Raw frames stay off the bridge; the app reads worker_status, whose codes are public.
        print(f"Embedded worker ended unexpectedly ({type(error).__name__}).", flush=True)
