"""Qt-free lifecycle of the desktop app: dashboard server, downloader and log.

The window layer only starts and stops this. It reuses the existing local
dashboard and background worker and never contacts Tapas itself.
"""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback

from . import background, osdep, sites
from .account_ui import DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL
from .errors import LibraryBusy
from .local_web import LOCAL_OWNER, LocalApplication, LocalWorkspaceRegistry, create_server
from .models import ArchiveError
from .session import load_session, store_session
from .state_paths import session_path
from .storage import safe_root
from .web import LOCATION_ACTIONS, THEMES
from .worker import WORKER_VERSION

LOG = logging.getLogger('comic_archive.desktop')
LOG.addHandler(logging.NullHandler())
WINDOWS = osdep.WINDOWS
FOLDER_NAME = 'Comic Archiver'
RETRY_SECONDS = 2
# How long closing waits for a downloader that had to be ended.
END_WAIT_SECONDS = 5
UNWRITABLE = ('The library folder cannot be opened or created. Check that it is a folder you can write to '
              'and that the disk is not full.')


def default_root():
    """One fixed, findable library folder; never the directory the app started in."""
    # Resolved because storage paths refuse symlinked parents.
    return Path.home().resolve() / FOLDER_NAME


def state_dir():
    """Private per-user folder for the app log and the single-instance lock."""
    name, default = ('LOCALAPPDATA', 'AppData/Local') if WINDOWS else ('XDG_STATE_HOME', '.local/state')
    base = Path(os.environ.get(name) or '')
    folder = (base if base.is_absolute() else Path.home() / default) / 'comic-archiver'
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise ArchiveError('Comic Archiver cannot create its settings folder in your home folder.') from None
    return folder


def library_root(chosen):
    """The library folder meant by a folder picked in a dialog.

    An existing library is used as it is, whether the person picked it or the folder of saved files inside
    it. An empty folder is used as it is. Anything else gets a 'Comic Archiver' folder inside it, so a
    library is never mixed in with other files.
    """
    try:
        # Resolved because storage paths refuse symlinked parents.
        folder = Path(chosen).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(chosen)
    if folder.name == 'Library' and is_library(folder.parent):
        return folder.parent
    if folder.name == FOLDER_NAME or is_library(folder):
        return folder
    return folder if unused(folder) else folder / FOLDER_NAME


def is_library(folder):
    """True for a folder Comic Archiver has saved into before, from any site; a folder merely called Library does not count."""
    try:
        return any((sites.folder(folder, site) / 'Library' / '.archive-state').is_dir() for site in sites.SITES)
    except (OSError, ValueError):
        return False


def present(folder):
    try:
        return folder.is_dir()
    except (OSError, ValueError):
        return False


def unused(folder):
    """True for a folder that does not exist yet or has nothing in it."""
    try:
        return not any(folder.iterdir())
    except FileNotFoundError:
        return True
    except OSError:
        return False


def saved_root():
    """The library folder chosen in the app, or None for the standard one."""
    try:
        text = (state_dir() / 'library').read_text(encoding='utf-8').rstrip('\n')
    except (ArchiveError, OSError, ValueError):
        return None
    return Path(text) if text and '\0' not in text and Path(text).is_absolute() else None


def remember_root(root):
    """Keep the chosen library folder for the next start; None goes back to the standard one."""
    try:
        folder = state_dir()
        if root is None or root == default_root():
            (folder / 'library').unlink(missing_ok=True)
        else:
            (folder / 'library.new').write_text(str(root) + '\n', encoding='utf-8')
            osdep.replace_file(folder / 'library.new', folder / 'library')
    except (ArchiveError, OSError, ValueError):
        LOG.warning('the library folder choice could not be saved; it lasts until the app closes')


def saved_site():
    """The site chosen in the app, or the first one; a site that can no longer be chosen counts as none."""
    try:
        key = (state_dir() / 'site').read_text(encoding='utf-8').strip()
    except (ArchiveError, OSError, ValueError):
        return sites.DEFAULT
    return sites.find(key) or sites.DEFAULT


def remember_site(site):
    """Keep the chosen site for the next start."""
    try:
        folder = state_dir()
        if site.key == sites.DEFAULT.key:
            (folder / 'site').unlink(missing_ok=True)
        else:
            (folder / 'site.new').write_text(site.key + '\n', encoding='utf-8')
            osdep.replace_file(folder / 'site.new', folder / 'site')
    except (ArchiveError, OSError, ValueError):
        LOG.warning('the site choice could not be saved; it lasts until the app closes')


class SiteChoice:
    """A site the dashboard has asked the window to switch to; the page waits until the window has settled it."""

    def __init__(self):
        self._asked = None
        self._waiting = False
        self._guard = threading.Lock()

    def ask(self, site):
        with self._guard:
            self._asked, self._waiting = site, True

    def take(self):
        """The latest request, once: a site, or None."""
        with self._guard:
            asked, self._asked = self._asked, None
        return asked

    def settle(self):
        """The window has switched, or stayed: the page reads the open site again."""
        with self._guard:
            if self._asked is None:
                self._waiting = False

    def waiting(self):
        with self._guard:
            return self._waiting


class Location:
    """Where episodes are saved, and what the dashboard has asked the window to do about it."""

    def __init__(self, folder):
        self.folder = folder
        self._asked = None
        self._guard = threading.Lock()

    def get(self):
        return str(self.folder)

    def ask(self, action):
        if action not in LOCATION_ACTIONS:
            raise ArchiveError('Comic Archiver does not know that folder action.')
        with self._guard:
            self._asked = action

    def take(self):
        """The latest request, once: 'open', 'choose' or None."""
        with self._guard:
            asked, self._asked = self._asked, None
        return asked


class Theme:
    """The look chosen for the app: the computer's own light or dark setting unless the person picks one."""

    def __init__(self):
        self.value = None
        self._guard = threading.Lock()

    def get(self):
        with self._guard:
            if self.value is None:
                try:
                    text = (state_dir() / 'theme').read_text(encoding='utf-8').strip()
                except (ArchiveError, OSError, ValueError):
                    text = ''
                self.value = text if text in THEMES else THEMES[0]
            return self.value

    def set(self, value):
        if value not in THEMES:
            raise ArchiveError('Comic Archiver does not know that theme.')
        with self._guard:
            self.value = value
            try:
                folder = state_dir()
                (folder / 'theme.new').write_text(value + '\n', encoding='utf-8')
                osdep.replace_file(folder / 'theme.new', folder / 'theme')
            except (ArchiveError, OSError):
                LOG.warning('the theme choice could not be saved; it lasts until the app closes')


def describe(error):
    """Class and code location only: exception text can hold cookies, paths or URLs."""
    frames = traceback.extract_tb(error.__traceback__)[-4:]
    where = ' < '.join(f'{Path(frame.filename).name}:{frame.lineno}' for frame in reversed(frames))
    return type(error).__name__ + (' at ' + where if where else '')


def configure_logging():
    """Write the app log to a private rotating file; return its path, or None."""
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    try:
        path = state_dir() / 'desktop.log'
        if path.is_symlink():
            raise OSError
        os.close(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=2, encoding='utf-8')
    except (OSError, ArchiveError):
        # The app still runs; it only cannot explain itself afterwards.
        LOG.addHandler(logging.NullHandler())
        return None
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    return path


class Runtime:
    """The local dashboard server and the downloader behind one app window."""

    def __init__(self, project_root, theme=None, site=None):
        self.project_root = project_root
        self.site = site or sites.DEFAULT
        self.server = self.thread = self.session = self.state = self.url = self.port = self.signin = None
        self.root = self.location = self.site_choice = None
        self.theme = theme or Theme()
        self._closing = threading.Event()
        self._launch = threading.Lock()
        self._retry = None
        self._stopped = False

    def _place(self, site):
        """The site's own library, sign-in and downloader, inside the library folder."""
        try:
            root = safe_root(self.project_root)
            root.mkdir(mode=0o700, exist_ok=True)
            place = safe_root(sites.folder(root, site))
            place.mkdir(mode=0o700, exist_ok=True)
            session = session_path(place)
            return root, place, session, safe_root(session / 'state.json')
        except ArchiveError:
            raise ArchiveError(f'Comic Archiver cannot use "{self.project_root}" because it, or a folder around it, '
                               'is a shortcut (symlink) to somewhere else. Choose a normal folder.') from None
        except OSError:
            raise ArchiveError(UNWRITABLE) from None

    def start(self):
        """Serve the dashboard on a free loopback port and return its address."""
        root, place, session, state = self._place(self.site)
        try:
            server = create_server(place, 0)
        except ArchiveError as error:
            if not str(error).startswith('Cannot bind'):
                raise
            raise ArchiveError('Comic Archiver could not open its private page on this computer. '
                               'Restart your computer and try again.') from None
        except (OSError, sqlite3.Error) as error:
            LOG.error('library cannot be opened: %s', describe(error))
            raise ArchiveError(UNWRITABLE) from None
        self.root, self.server, self.port = root, server, server.server_port
        self.site_choice = SiteChoice()
        self._serve(server.get_app(), session, state)
        self.thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .25}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.port}/'
        LOG.info('dashboard is listening on loopback port %d', self.port)
        return self.url

    def _serve(self, app, session, state):
        """Answer the page from this site's own library from now on."""
        # Sign-in happens in the app's own window. The downloader it starts uses the same engine, unseen.
        from .window_signin import WindowSignIn
        registry = app.registry
        registry.theme = self.theme
        registry.sites = sites.choices(self.site)
        registry.site_choice = self.site_choice
        self.location = registry.location = Location(registry.resolve(LOCAL_OWNER, registry.workspace_id).library)
        self._reopen(registry, session)
        self.signin = registry.site_signin = WindowSignIn(registry.resolve(LOCAL_OWNER, registry.workspace_id),
                                                           self._launch, self._closing, site=self.site)
        self.session, self.state = session, state
        self.server.set_app(app)

    def change_site(self, site):
        """Open the same library folder on another site, behind the same page and port; that site's own sign-in.

        The page keeps its cookie and reads the other site's library on its next request. Returns None, or the
        reason in words when the other site's folder cannot be opened; the open site then stays as it was.
        """
        try:
            _, place, session, state = self._place(site)
            app = LocalApplication(LocalWorkspaceRegistry(place), self.port, cookie_key=self.server.get_app().cookie_key)
        except ArchiveError as error:
            LOG.warning('the site was not changed: %s', describe(error))
            return str(error)
        except (OSError, sqlite3.Error) as error:
            LOG.warning('the site was not changed: %s', describe(error))
            return UNWRITABLE
        self._leave()
        self.site = site
        self._serve(app, session, state)
        remember_site(site)
        LOG.info('the site was changed')
        self.start_worker()
        return None

    def _leave(self, wait_seconds=20):
        """Stop the open site's downloader and sign-in; the page is still served."""
        self._closing.set()
        self._closing = threading.Event()  # The next site's own.
        deadline = time.monotonic() + wait_seconds
        held = self._launch.acquire(timeout=wait_seconds)
        try:
            self._stop_downloader(deadline)
        finally:
            if held:
                self._launch.release()
        registry = self.server.get_app().registry
        registry.cloud_signin.close()
        registry.site_signin.close()

    def _reopen(self, registry, session):
        """A download waits for Go at every start; one that was running when the app closed does not go on by itself."""
        from .account_ui import reopen
        try:
            if reopen(registry.resolve(LOCAL_OWNER, registry.workspace_id), idle=background.status(session).get('running') is False):
                LOG.info('the download that was running when the app closed waits for Go')
        except (ArchiveError, OSError, sqlite3.Error) as error:
            LOG.warning('the saved download was not set to wait for Go: %s', describe(error))

    def start_worker(self):
        """Start or reuse the downloader for a saved sign-in; can block for about a minute."""
        if self.state is None or not self.state.is_file():
            return 'signed_out'
        outcome = self._start_once()
        if outcome == 'deferred':
            self._retry = threading.Thread(target=self._start_when_free, daemon=True)
            self._retry.start()
        return outcome

    def _start_once(self, closing=None):
        closing = closing or self._closing  # A wait that began before a switch to another site starts nothing after it.
        with self._launch:
            if closing.is_set():
                return 'closing'
            try:
                current = background.status(self.session)
                upgrade = current.get('running') is True and current.get('worker_version') != WORKER_VERSION
                result = background.start(self.session, DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL, restart=upgrade)
            except LibraryBusy:
                outcome = 'deferred'
            except (ArchiveError, OSError) as error:
                # The dashboard reports the downloader as offline and offers Sign in.
                LOG.warning('downloader did not start: %s', describe(error))
                return 'failed'
            else:
                outcome = ('deferred' if result.get('start_deferred')
                           else 'running' if result.get('running') is True else 'failed')
                if closing.is_set() and outcome == 'running':
                    # The window closed while this was starting, after stop() gave up waiting for us.
                    self._drain()
                    outcome = 'closing'
        LOG.info('downloader start: %s', outcome)
        return outcome

    def _signing_in(self):
        return self.server.get_app().registry.site_signin.status().get('status') == 'running'

    def _start_when_free(self):
        """An earlier downloader is finishing its task; start ours once it has gone."""
        closing = self._closing  # A switch to another site ends this wait.
        while not closing.wait(RETRY_SECONDS):
            try:
                current = background.status(self.session)
            except (ArchiveError, OSError) as error:
                LOG.warning('downloader state is unreadable: %s', describe(error))
                if isinstance(error, ArchiveError):
                    return
                continue  # A mount that is briefly unavailable.
            if current.get('running') is True and not current.get('stop_requested'):
                return  # Sign-in started a fresh downloader meanwhile.
            # Sign-in replaces the downloader itself and holds the same launch lock.
            if current.get('running') is not False or self._signing_in():
                continue
            self._start_once(closing)
            return

    def busy(self):
        """'download' or 'signin' while closing the window would interrupt one, else None."""
        if self.server is None:
            return None
        registry = self.server.get_app().registry
        try:
            if (registry.account_status(LOCAL_OWNER, registry.workspace_id).get('status') == 'running'
                    or background.status(self.session).get('state') == 'busy'):
                return 'download'
            return 'signin' if self._signing_in() else None
        except (ArchiveError, OSError) as error:
            LOG.warning('download state is unreadable: %s', describe(error))
            return None

    def _drain(self):
        try:
            background.stop(self.session, wait_seconds=0)
        except (ArchiveError, OSError) as error:
            LOG.warning('downloader did not accept the stop request: %s', describe(error))

    def _ask(self, deadline):
        """Ask the downloader to leave. It is briefly busy while it takes up a job; then it is asked again."""
        while True:
            try:
                return background.stop(self.session, wait_seconds=0)
            except LibraryBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def _interrupt(self):
        from .account_ui import interrupt
        registry = self.server.get_app().registry
        try:
            interrupt(registry.resolve(LOCAL_OWNER, registry.workspace_id))
        except (ArchiveError, OSError, sqlite3.Error) as error:
            LOG.warning('the download in progress was not interrupted: %s', describe(error))

    def _stop_downloader(self, deadline):
        """Stop the open site's downloader; returns what is known of it then, and whether it refused."""
        result, refused = {}, False
        try:
            try:
                # Asked to leave first: a downloader that ends its batch then goes without reading that batch's receipt.
                result = self._ask(deadline)
                if result.get('running') is not False:
                    self._interrupt()
                    result = gone(self.session, deadline - time.monotonic())
            except (ArchiveError, OSError) as error:
                LOG.warning('downloader did not accept the stop request: %s', describe(error))
                refused = True
            # Also when it could not even be asked: nothing is left running behind the closed window.
            if result.get('running') is not False and end_downloader(self.session):
                result = gone(self.session, END_WAIT_SECONDS)
        except (ArchiveError, OSError) as error:
            LOG.warning('downloader could not be ended: %s', describe(error))
            refused = True
        return result, refused

    def stop(self, wait_seconds=20):
        """Close the dashboard and stop the downloader now; nothing is left running behind the closed window.

        The batch in progress is interrupted after the image being saved. A
        downloader that still has not gone when the wait is over is ended. The
        saved account download is left as it is; the next start sets it to
        wait for Go.
        """
        if self._stopped:
            return 'stopped_before'
        self._stopped = True
        self._closing.set()
        deadline = time.monotonic() + wait_seconds
        if self.server is None:
            return 'not_started'
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        # A downloader that is still starting must be visible before it is asked to stop.
        held = self._launch.acquire(timeout=max(0, deadline - time.monotonic()))
        try:
            result, refused = self._stop_downloader(deadline)
        finally:
            if held:
                self._launch.release()
        if refused and result.get('running') is not False:
            return 'failed'
        outcome = 'stopped' if result.get('running') is False else 'finishing'
        LOG.info('dashboard closed; downloader %s', outcome)
        return outcome


def gone(session, seconds):
    """Wait for the downloader to have left, and return what is known of it then."""
    deadline = time.monotonic() + seconds
    while True:
        result = background.status(session)
        if result.get('running') is False or time.monotonic() >= deadline:
            return result
        time.sleep(.1)


def end_downloader(session):
    """Last resort when closing: end a downloader that did not stop when asked, with the browser it started.

    What it was saving is picked up again like after a power cut. Only the
    process recorded by a running downloader is ended, and only if its command
    line is that of the downloader for this library.
    """
    folder = background.root(session)
    pid = background.read(folder / 'status.json').get('pid')
    if type(pid) is not int or pid <= 1 or not background.running(folder):
        return False
    try:
        if not osdep.runs_command(pid, ('browser-worker', str(folder.parent))):
            return False
        osdep.end_process(pid)
    except OSError:
        return False
    LOG.warning('the downloader did not stop when asked and was ended')
    return True


def move(previous, root):
    """Stop one runtime and start another on a different library folder; saved episodes are never moved.

    Returns the runtime to use from now on and None, or, when the new folder cannot be used, a runtime
    back on the old folder and the reason in words for the person.
    """
    empty = unused(Path(root))
    fresh, problem = replace(previous, Runtime(root, previous.theme, previous.site), 'library folder')
    if problem is None:
        if empty:
            carry(previous, fresh)
        remember_root(fresh.root)
    return start_worker(fresh), problem


def replace(previous, fresh, change):
    """Stop the previous runtime and start the fresh one, or start the previous one's place again if that fails."""
    previous.stop()
    try:
        fresh.start()
    except ArchiveError as error:
        LOG.warning('the %s was not changed: %s', change, describe(error))
        fresh.stop(0)
        back = Runtime(previous.project_root, previous.theme, previous.site)
        back.start()
        return back, str(error)
    LOG.info('the %s was changed', change)
    return fresh, None


def start_worker(runtime):
    runtime.start_worker()
    return runtime


def carry(previous, fresh):
    """A folder with nothing in it starts signed in as before; an existing library keeps its own sign-in."""
    if previous.state is None or not previous.state.is_file() or fresh.state.is_file():
        return
    try:
        store_session(load_session(previous.session), fresh.session, sites.of_session(previous.session))
    except (ArchiveError, OSError) as error:
        LOG.warning('the sign-in was not carried to the new folder: %s', describe(error))
