"""The desktop app's own window: Qt shows the existing loopback dashboard.

Signing in to Tapas happens in the same window, on Tapas's own page, in a
browser profile that shares nothing with the dashboard and keeps nothing.

PySide6 is imported only inside functions, so the package and --version work
without it. Decisions are plain functions; the Qt objects only carry them out.
"""
import argparse
import contextlib
import hashlib
from importlib import metadata
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace

from . import app_runtime, osdep, sites
from .app_runtime import LOG
from .local_web import SESSION_SECONDS
from .models import ArchiveError
from .qt_engine import CookieRecords, common_agent
from .tapas_auth import identity
from .adapters.tappytoon import account as tappytoon_account
from .window_signin import storage_state

APP_NAME = 'Comic Archiver'
DESKTOP_ID = 'comic-archiver'
# The same name as in packaging/windows/comic-archiver.iss, so the taskbar keeps the window with its shortcut.
WINDOWS_APP_ID = 'ComicArchiver.Desktop'
RAISE = b'raise\n'
SELF_TEST_SECONDS = 60
SELF_TEST_SCRIPT = "document.querySelector('#account-download') !== null"
SELF_TEST_COOKIE = 'comic_archive_self_test'
SIGNIN_POLL_MS = 250
SIGNIN_PROBE_MS = 500
SIGNIN_OPEN_SECONDS = 20
SIGNIN_BACK = 'Cancel sign-in'
# {site} is filled in by site_text, so the window learns the site's name from sites.py alone.
SIGNIN_HINT = 'Sign in to {site} below. Comic Archiver carries on by itself once you are signed in.'
SIGNIN_RETRY = 'Try again'
SIGNIN_BAR_WIDTH = 1100  # .wrap in static/archive.css: the header's contents stay in a centred column.
# The dashboard's own colours (static/archive.css), light then dark.
THEME_COLOURS = {'page': ('#f4f6f2', '#111918'), 'surface': ('#fff', '#1a2523'), 'border': ('#d5dfd5', '#3d5149'),
                 'text': ('#183331', '#e3ece8'), 'muted': ('#52695e', '#adbbb3'), 'surface-soft': ('#f0f5f1', '#23332f')}
# The dashboard's own header, so the window keeps one bar from page to page.
SIGNIN_STYLE = '''
#signinHeader { background: %(surface)s; border-bottom: 1px solid %(border)s; }
#signinHeader QLabel { color: %(muted)s; font-size: 14px; }
#signinHeader QLabel#signinTitle { color: %(text)s; font-size: 18px; font-weight: 600; }
#signinHeader QPushButton { min-height: 28px; padding: 8px 12px; border: none; border-radius: 6px;
                            background: transparent; color: %(muted)s; font-size: 14px; font-weight: 500; }
#signinHeader QPushButton:hover { color: %(text)s; background: %(surface-soft)s; }
'''
# Told to each dashboard page before it draws, so a look other than the computer's own never flashes.
# The script runs before the page has its root element, so it waits for that one element.
THEME_SCRIPT = '''(() => {
  const tell = () => { if (document.documentElement) document.documentElement.dataset.theme = '%s'; return !!document.documentElement; };
  if (tell()) return;
  const watch = new MutationObserver(() => { if (tell()) watch.disconnect(); });
  watch.observe(document, {childList: true});
})();'''
SIGNIN_OPENING = 'Opening the {site} sign-in page…'
SIGNIN_OFFLINE = ('The {site} sign-in page could not be opened.\n\n'
                  'Check your internet connection, then choose "Try again".')
SIGNIN_FINISHING = 'Finishing sign-in…\n\nThis can take a minute. Comic Archiver comes back by itself.'
# The page is asked who is signed in, as the downloader asks: a same-origin request by the page itself.
# runJavaScript does not wait for promises, so one script starts the request and a later one reads what it left.
# Both run in an isolated world, where the page's own scripts can neither see nor replace the answer.
# A new page is asked at once, so a finished sign-in is noticed within a second; after that, every three seconds.
SIGNIN_ASK = """(() => {
  const box = window.tapasArchive = window.tapasArchive || {};
  const now = Date.now();
  if (box.busy || now - (box.asked || 0) < 3000) return;
  box.busy = true;
  box.asked = now;
  fetch('/who-am-i', {method: 'GET', credentials: 'same-origin', redirect: 'error', headers: {Accept: 'application/json'}})
    .then(response => response.ok ? response.text() : null)
    .then(text => { box.answer = text; box.busy = false; }, () => { box.answer = null; box.busy = false; });
})()"""
SIGNIN_READ = """(() => {
  const box = window.tapasArchive;
  if (!box || typeof box.answer !== 'string' || box.answer.length > 2097152) return null;
  const answer = box.answer, storage = [];
  box.answer = null;
  try {
    for (let index = 0; index < window.localStorage.length; index++) {
      const name = window.localStorage.key(index);
      storage.push([name, window.localStorage.getItem(name)]);
    }
  } catch (error) {}
  return JSON.stringify({origin: window.location.origin, answer, storage});
})()"""
# Tappytoon's pages carry who is signed in within their page data, as the downloader reads it. The page asks
# for its library page and passes on only the account's number, in the shape that data has.
TAPPYTOON_ASK = SIGNIN_ASK.replace(
    """fetch('/who-am-i', {method: 'GET', credentials: 'same-origin', redirect: 'error', headers: {Accept: 'application/json'}})
    .then(response => response.ok ? response.text() : null)
    .then(text => { box.answer = text; box.busy = false; }""",
    """fetch('/en/me/library/unlocked/comics', {method: 'GET', credentials: 'same-origin', redirect: 'error', headers: {Accept: 'text/html'}})
    .then(response => response.ok ? response.text() : null)
    .then(text => {
      const found = text && /<script id="__NEXT_DATA__"[^>]*>([\\s\\S]*?)<\\/script>/.exec(text);
      let user = null;
      try { user = found ? JSON.parse(found[1]).props.initialState.oauth2.user : null; } catch (error) {}
      box.answer = JSON.stringify({oauth2: {user: {id: user ? user.id : null}}});
      box.busy = false;
    }""")
# How each site's own page tells who is signed in: the question its ask puts, and how its answer names the account.
# A site without one is never taken as signed in, so its sign-in cannot finish.
IDENTITIES = {'tapas': identity, 'tappytoon': tappytoon_account}
ASKS = {'tapas': SIGNIN_ASK, 'tappytoon': TAPPYTOON_ASK}
SIGNIN_FORGET = "(() => { try { window.localStorage.clear(); window.sessionStorage.clear(); } catch (error) {} })()"
STARTING = 'Starting Comic Archiver…'
FINISHING = 'Finishing up. Everything saved so far is kept.\n\nThis window closes by itself in a moment.'
MOVING = 'Switching to the other folder…'
LOCATION_TITLE = 'Choose where Comic Archiver saves'
LOCATION_BUSY = 'Comic Archiver is busy saving or signing in. Pause it or let it finish, then change the folder.'
LOCATION_CONFIRM = ('Save to this folder from now on?\n\n{new}\n\n'
                    'Nothing is moved: episodes already saved stay where they are, in\n{old}')
LOCATION_EXISTING = ('This folder already holds a Comic Archiver library. Use it from now on?\n\n{new}\n\n'
                     'Nothing is moved: episodes saved in the folder used until now stay there, in\n{old}')
LOCATION_FAILED = 'The folder was not changed.\n\n{reason}'
SITE_BUSY = 'Comic Archiver is busy saving or signing in. Pause it or let it finish, then switch to {site}.'
SITE_FAILED = 'Comic Archiver stays on {site}.\n\n{reason}'
LOCATION_MISSING = ('Comic Archiver cannot open the folder it saves to:\n{folder}\n\n'
                    'If it is on a drive that is not connected, choose No, connect the drive and open Comic Archiver again.\n\n'
                    'Save to the standard folder instead?\n{standard}')
LOAD_FAILED = 'Comic Archiver could not show your library. Close this window and open Comic Archiver again.'
RENDER_FAILED = 'The window stopped working. Close it and open Comic Archiver again.'
CLOSE_DOWNLOAD = ('Comic Archiver is still saving your library. Close it and stop saving?\n\n'
                  'Saving stops right away. Everything saved so far is kept. The next time you open '
                  'Comic Archiver, press Go to continue.')
CLOSE_SIGNIN = 'Signing in to {site} is not finished. Close anyway?\n\nYou will need to sign in again.'
ALREADY_RUNNING = 'Comic Archiver is already open. Look for its window, or wait a moment and try again.'
# The raw import error is for the terminal and the log only; people get TOOLKIT_BROKEN.
MISSING_QT = ('Comic Archiver needs the PySide6 window toolkit, which is missing or cannot load.\n'
              'When installed with pip: pip install "comic-archive[desktop]"')
TOOLKIT_BROKEN = ('Comic Archiver is missing a part of your system that its window needs.\n'
                  'Please reinstall Comic Archiver, or ask someone to help you.')
_application = None


def site_text(text, site=sites.DEFAULT):
    return text.format(site=site.name)


def arguments(argv=None):
    parser = argparse.ArgumentParser(prog='comic-archiver', description='Save your own comics library from a desktop window.')
    parser.add_argument('--project-root', metavar='PATH',
                        help='use this existing library folder instead of "Comic Archiver" in your home folder')
    parser.add_argument('--self-test', action='store_true',
                        help='load the window off-screen with an empty temporary library, report JSON and exit')
    parser.add_argument('--version', action='store_true', help='print the version and exit')
    return parser.parse_args(argv)


def version():
    try:
        return metadata.version('comic-archive')
    except metadata.PackageNotFoundError:
        return 'unknown'


def navigation(scheme, host, port, app_port, main_frame=True):
    """allow: load in the window; external: hand to the default browser; deny: drop."""
    if scheme == 'http' and host == '127.0.0.1' and port == app_port:
        return 'allow'
    if main_frame and scheme in {'http', 'https'} and host:
        return 'external'
    return 'deny'


def own_download(url, app_port):
    """Only files the dashboard offers: its own addresses, or a report it built in the page."""
    origin = f'http://127.0.0.1:{app_port}/'
    return url.startswith((origin, 'blob:' + origin))


def signin_navigation(scheme, host, path, main_frame=True, history=False):
    """Whether the sign-in page may load an address. A whole page comes over https or not at all.

    Stepping back through the page's history never returns to its blank start."""
    if scheme == 'about' and not host:
        return (path == 'blank' and not history) or (path == 'srcdoc' and not main_frame)
    if scheme == 'https' and host:
        return True
    # Inside a page, sites also build frames from their own data; files and engine pages stay out.
    return not main_frame and scheme in {'data', 'blob'}


def loopback(host):
    """Names and addresses that mean this computer, where the dashboard's private API listens."""
    name = host.strip('[]').rstrip('.').lower()
    if name == 'localhost' or name.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    address = getattr(address, 'ipv4_mapped', None) or address
    return address.is_loopback or address.is_unspecified


def on_site(scheme, host, site=sites.DEFAULT):
    """Whether a page is the site's own, the only kind that is asked who is signed in."""
    return scheme == 'https' and host == site.host


def host_text(scheme, host):
    """What the bar above the sign-in page shows: the site only, in letters that cannot imitate another."""
    if scheme != 'https' or not host:
        return ''
    try:
        return host.encode('idna').decode('ascii')
    except UnicodeError:
        return ''


def page_spoke(text, site=sites.DEFAULT):
    """Whether the site's own page answered at all, which only a page that really arrived can do."""
    try:
        return json.loads(text)['origin'] == site.home
    except (TypeError, ValueError, KeyError, RecursionError):
        return False


def signed_in(text, site=sites.DEFAULT):
    """(account key, the site's local storage) once its page reports a signed-in account, else None."""
    named = IDENTITIES.get(site.key)
    try:
        value = json.loads(text)
        if value['origin'] != site.home or named is None:
            return None
        key = named(json.loads(value['answer']))
        storage = value['storage']
    except (TypeError, ValueError, KeyError, RecursionError):
        return None
    return (key, storage if isinstance(storage, list) else []) if key else None


def cookie_record(cookie):
    """A QNetworkCookie as plain data, so that nothing of Qt leaves the window's thread."""
    return {'name': bytes(cookie.name().data()).decode('utf-8', 'replace'),
            'value': bytes(cookie.value().data()).decode('utf-8', 'replace'),
            'domain': cookie.domain(), 'path': cookie.path(),
            'expires': None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch(),
            'httpOnly': cookie.isHttpOnly(), 'secure': cookie.isSecure(), 'sameSite': cookie.sameSitePolicy().name}


def close_action(phase, busy):
    """What a request to close the window does in each phase of the app's life."""
    if phase == 'done':
        return 'accept'
    if phase in {'stopping', 'moving'}:
        return 'ignore'
    return 'confirm' if busy else 'finish'


def session_stale(issued, now):
    """The dashboard's local cookie lasts SESSION_SECONDS and the server never renews it."""
    # A clock moved backwards leaves a cookie stamped in the future, which the server also rejects.
    return issued is not None and not 0 <= now - issued <= SESSION_SECONDS + 60


def raise_requested(message):
    return message == RAISE


def server_name(state):
    """A per-user channel name: local socket names are shared by everyone on the computer."""
    return DESKTOP_ID + '-' + hashlib.sha256(os.fsencode(state)).hexdigest()[:16]


def self_test_result(loaded, found, timed_out=False, cookie=False):
    """The one-line report and exit status of --self-test; 'cookie' is the sign-in cookie check."""
    reason = ('timeout' if timed_out else 'dashboard_not_loaded' if not loaded
              else 'dashboard_incomplete' if found is not True
              else 'signin_cookie_check_failed' if cookie is not True else None)
    report = {'ok': False, 'reason': reason} if reason else {'ok': True, 'version': version()}
    return report | {'signin_cookie': cookie is True}, 1 if reason else 0


def standard_instead(q, folder):
    """Asked at start when the chosen library folder is not there: carry on with the standard one?"""
    box = q.QtWidgets.QMessageBox
    buttons = box.StandardButton
    text = LOCATION_MISSING.format(folder=folder, standard=app_runtime.default_root())
    return box.question(None, APP_NAME, text, buttons.Yes | buttons.No, buttons.No) == buttons.Yes


def failure_text(error, log_path):
    # Only ArchiveError carries wording written for the user.
    reason = str(error) if isinstance(error, ArchiveError) else 'Something unexpected went wrong.'
    details = f'\n\nDetails are saved in:\n{log_path}' if log_path else ''
    return f'{APP_NAME} could not start.\n\n{reason}{details}'


def qt():
    """Import Qt on demand. QtWebEngine must be imported before the application exists."""
    from PySide6 import QtCore, QtGui, QtNetwork, QtWebEngineCore, QtWebEngineWidgets, QtWidgets
    return SimpleNamespace(QtCore=QtCore, QtGui=QtGui, QtNetwork=QtNetwork, QtWebEngineCore=QtWebEngineCore,
                           QtWebEngineWidgets=QtWebEngineWidgets, QtWidgets=QtWidgets)


def application(q):
    global _application
    app = q.QtWidgets.QApplication.instance() or q.QtWidgets.QApplication([DESKTOP_ID])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_ID)
    if osdep.WINDOWS:
        windows_identity(q, app)
    # The dashboard is on this computer and a system proxy must never see it.
    # The setting is application-wide, so the Tapas sign-in page connects directly as well.
    q.QtNetwork.QNetworkProxyFactory.setUseSystemConfiguration(False)
    # Qt objects are torn down in a safe order only while the application outlives them.
    _application = app
    return app


def windows_identity(q, app):
    """Give the window the app's own icon and place on the taskbar; Windows otherwise files it under Python."""
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):
        pass
    icon = Path(sys.executable).resolve().parent.parent / 'comic-archiver.ico'
    if icon.is_file():
        app.setWindowIcon(q.QtGui.QIcon(str(icon)))


def toolkit_missing(error, self_test):
    """PySide6 is absent, or a part of it needs a system library that is not installed."""
    global _application
    # An import failure names modules and library files, never account data, and is the only clue.
    print(f'{MISSING_QT}\n\n{error}', file=sys.stderr)
    if self_test:
        print(json.dumps({'ok': False, 'reason': 'window_toolkit_missing'}), flush=True)
        return 1
    log_path = app_runtime.configure_logging()
    LOG.error('window toolkit cannot load: %s', error)
    try:
        # The plain widgets often still load when only the browser engine cannot.
        from PySide6 import QtWidgets
        _application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([DESKTOP_ID])
        QtWidgets.QMessageBox.critical(None, APP_NAME, failure_text(ArchiveError(TOOLKIT_BROKEN), log_path))
    except ImportError:
        pass
    return 1


def notify(q, name):
    """Ask the running instance to show its window; False when nobody answers."""
    socket = q.QtNetwork.QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(2000):
        return False
    socket.write(RAISE)
    delivered = socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    return bool(delivered)


def claim(q, lock, name):
    """True when this process is the one instance; False after handing over to the other."""
    if lock.tryLock(0):
        return True
    if lock.error() != q.QtCore.QLockFile.LockError.LockFailedError:
        raise ArchiveError('Comic Archiver cannot write to its settings folder.')
    if notify(q, name):
        return False
    # Nobody answered. Qt removes the lock only when its owner no longer holds it.
    if lock.removeStaleLockFile() and lock.tryLock(0):
        LOG.info('recovered a lock left by an instance that did not exit cleanly')
        return True
    q.QtWidgets.QMessageBox.information(None, APP_NAME, ALREADY_RUNNING)
    return False


def signin_style(dark):
    return SIGNIN_STYLE % {name: pair[bool(dark)] for name, pair in THEME_COLOURS.items()}


class Shell:
    """One window around one runtime."""

    def __init__(self, q, app, runtime, log_path=None, probe=None):
        self.q, self.app, self.runtime, self.log_path, self.probe = q, app, runtime, log_path, probe
        self.phase = 'starting'
        self.issued = None
        self.renewing = False
        self.crashes = 0
        self.window = self.channel = self.home = None
        # None, or what the sign-in panel shows: 'page', 'offline' or 'finishing'.
        self.signin_stage = self.signin_panel = self.cookie_check = None
        self.theme_choice = self.theme_script = self.signin_header = None
        self.choosing = False
        self.signin_opening = self.signin_committed = self.signin_clearing = False
        self.signin_waiting = None
        self.signin_since = 0
        self.signin_heard = None
        self.cookies = CookieRecords()

    def listen(self, name):
        """Let a second launch ask this one to come forward."""
        kind = self.q.QtNetwork.QLocalServer
        kind.removeServer(name)  # Left behind by a crash; the lock proves no live owner.
        self.channel = kind(self.app)
        self.channel.setSocketOptions(kind.SocketOption.UserAccessOption)
        self.channel.newConnection.connect(self.connection)
        if not self.channel.listen(name):
            LOG.warning('a second launch cannot bring this window forward')

    def connection(self):
        socket = self.channel.nextPendingConnection()
        if socket is None:
            return

        def read():
            if raise_requested(bytes(socket.readAll().data())):
                self.come_forward()
        socket.readyRead.connect(read)
        socket.disconnected.connect(socket.deleteLater)
        if socket.bytesAvailable():
            read()

    def come_forward(self):
        if self.window is None:
            return
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def open(self):
        """Build and show the window; the dashboard itself arrives with load()."""
        q, shell = self.q, self

        class Window(q.QtWidgets.QMainWindow):
            def closeEvent(self, event):
                shell.close_requested(event)

        class Page(q.QtWebEngineCore.QWebEnginePage):
            def acceptNavigationRequest(self, url, kind, main_frame):
                return shell.navigate(url, main_frame)

        self.window = Window()
        self.window.setWindowTitle(APP_NAME)
        self.window.resize(1100, 820)
        self.notice = q.QtWidgets.QLabel(STARTING)
        self.notice.setAlignment(q.QtCore.Qt.AlignmentFlag.AlignCenter)
        self.notice.setWordWrap(True)
        # A profile without a storage name is off the record: only the local cookie, in memory.
        self.profile = q.QtWebEngineCore.QWebEngineProfile(self.app)
        self.profile.downloadRequested.connect(self.download)
        self.page = Page(self.profile, self.window)
        self.page.newWindowRequested.connect(self.new_window)
        self.page.permissionRequested.connect(lambda permission: permission.deny())
        self.page.loadFinished.connect(self.loaded)
        self.page.renderProcessTerminated.connect(self.render_gone)
        # Qt requires the page to be gone before its profile.
        self.app.aboutToQuit.connect(self.page.deleteLater)
        self.view = q.QtWebEngineWidgets.QWebEngineView(self.window)
        self.view.setPage(self.page)
        self.stack = q.QtWidgets.QStackedWidget(self.window)
        self.stack.addWidget(self.notice)
        self.stack.addWidget(self.view)
        self.home = self.notice
        self.window.setCentralWidget(self.stack)
        self.clock = q.QtCore.QTimer(self.window)
        self.clock.setInterval(60_000)
        self.clock.timeout.connect(self.renew)
        self.signin_clock = q.QtCore.QTimer(self.window)
        self.signin_clock.setInterval(SIGNIN_POLL_MS)
        self.signin_clock.timeout.connect(self.signin_tick)
        self.app.styleHints().colorSchemeChanged.connect(self.theme_paint)
        self.theme_apply()
        self.window.show()

    def dark(self):
        return self.app.styleHints().colorScheme() == self.q.QtCore.Qt.ColorScheme.Dark

    def theme_apply(self):
        """Give the window the look chosen in the dashboard: the computer's own, or always light or dark."""
        q = self.q
        self.theme_choice = self.runtime.theme.get()
        theme = self.theme_choice if self.theme_choice in ('light', 'dark') else None
        scripts = self.profile.scripts()
        if self.theme_script is not None:
            scripts.remove(self.theme_script)
            self.theme_script = None
        if theme:
            kind = q.QtWebEngineCore.QWebEngineScript
            self.theme_script = kind()
            self.theme_script.setName('comic-archive-theme')
            self.theme_script.setSourceCode(THEME_SCRIPT % theme)
            self.theme_script.setInjectionPoint(kind.InjectionPoint.DocumentCreation)
            self.theme_script.setWorldId(int(kind.ScriptWorldId.MainWorld))
            scripts.insert(self.theme_script)
        scheme = q.QtCore.Qt.ColorScheme
        # Unknown hands the choice back to the computer's own setting.
        self.app.styleHints().setColorScheme({'light': scheme.Light, 'dark': scheme.Dark}.get(theme, scheme.Unknown))
        self.theme_paint()

    def theme_paint(self, *_):
        """Colour what Qt draws itself; also called when the computer switches between light and dark."""
        dark = self.dark()
        # What shows through before a page has drawn.
        self.page.setBackgroundColor(self.q.QtGui.QColor(THEME_COLOURS['page'][dark]))
        if self.signin_header is not None:
            self.signin_header.setStyleSheet(signin_style(dark))

    def show(self, widget):
        """Put a dashboard widget on screen, or only remember it while sign-in has the window."""
        self.home = widget
        if not self.signin_stage:
            self.stack.setCurrentWidget(widget)

    def background(self, work, done):
        """Keep the window responsive: work runs on a thread, done runs back here."""
        def guarded():
            try:
                work()
            except Exception as error:
                LOG.error('background step failed: %s', app_runtime.describe(error))
        thread = threading.Thread(target=guarded, daemon=True)
        timer = self.q.QtCore.QTimer(self.window)
        timer.setInterval(100)

        def check():
            if not thread.is_alive():
                timer.stop()
                done()
        timer.timeout.connect(check)
        thread.start()
        timer.start()

    def load(self):
        if self.phase != 'starting':
            return  # Closed while the downloader was starting.
        self.phase = 'open'
        self.renewing = True
        self.view.load(self.q.QtCore.QUrl(self.runtime.url))
        self.clock.start()
        self.signin_clock.start()

    def loaded(self, ok):
        if self.phase != 'open':
            return
        if ok:
            if self.renewing:
                self.renewing, self.issued = False, time.time()
            self.show(self.view)
        elif self.issued is None:
            LOG.error('the dashboard did not load in the window')
            self.show_notice(LOAD_FAILED)
        if self.probe:
            if ok:
                self.page.runJavaScript(SELF_TEST_SCRIPT, 0, lambda found: self.probe(True, found))
            else:
                self.probe(False, None)

    def show_notice(self, text):
        self.notice.setText(text + (f'\n\nDetails are saved in:\n{self.log_path}' if self.log_path else ''))

    def render_gone(self, status, code):
        """The page's process died: say so, and try once to bring the dashboard back."""
        normal = self.q.QtWebEngineCore.QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus
        if self.phase != 'open' or status == normal:
            return
        LOG.error('the window process stopped: %s, exit code %s', getattr(status, 'name', status), code)
        self.show_notice(RENDER_FAILED)
        self.show(self.notice)
        if self.crashes < 1:
            self.crashes += 1
            self.view.reload()

    def renew(self):
        # Checked against the wall clock every minute, so a suspended laptop recovers on waking.
        if self.phase == 'open' and session_stale(self.issued, time.time()):
            self.renewing = True
            self.view.reload()

    def navigate(self, url, main_frame=True):
        action = navigation(url.scheme(), url.host(), url.port(), self.runtime.port, main_frame)
        if action == 'external':
            self.q.QtGui.QDesktopServices.openUrl(url)
        return action == 'allow'

    def new_window(self, request):
        url = request.requestedUrl()
        if self.navigate(url):
            request.openIn(self.page)

    def download(self, item):
        if not own_download(item.url().toString(), self.runtime.port):
            LOG.warning('refused a download that did not come from the dashboard')
            item.cancel()
            return
        paths = self.q.QtCore.QStandardPaths
        folder = paths.writableLocation(paths.StandardLocation.DownloadLocation)
        chosen, _ = self.q.QtWidgets.QFileDialog.getSaveFileName(self.window, 'Save file', str(Path(folder) / item.downloadFileName()))
        if not chosen:
            item.cancel()
            return
        item.setDownloadDirectory(str(Path(chosen).parent))
        item.setDownloadFileName(Path(chosen).name)
        item.accept()

    # Signing in to Tapas, in this window.

    def build_signin(self):
        """The sign-in panel, or nothing: a failed build leaves no half-made page or profile behind."""
        try:
            self.make_signin()
        except Exception:
            for name in ('signin_view', 'signin_page', 'signin_profile'):  # The page goes before its profile.
                part = getattr(self, name, None)
                if part is not None:
                    part.deleteLater()
                    setattr(self, name, None)
            self.signin_panel = None
            self.cookies.clear()
            raise

    def make_signin(self):
        """A bar that says where the page comes from, above Tapas's own page."""
        q, core, widgets = self.q, self.q.QtWebEngineCore, self.q.QtWidgets

        class SignInPage(core.QWebEnginePage):
            def acceptNavigationRequest(self, url, kind, main_frame):
                back = kind == core.QWebEnginePage.NavigationType.NavigationTypeBackForward
                return signin_navigation(url.scheme(), url.host(), url.path(), main_frame, back)

        class Guard(core.QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                # May run off the window's thread: it looks at the request and at nothing else.
                if loopback(info.requestUrl().host()):
                    info.block(True)

        self.world = int(core.QWebEngineScript.ScriptWorldId.ApplicationWorld)
        # Its own off-the-record profile: Tapas pages share no cookies or storage with the dashboard,
        # and Qt saves none. state.json stays the only saved sign-in.
        self.signin_profile = core.QWebEngineProfile(self.app)
        # Tapas sees one name for the sign-in and for the downloader that later uses it.
        self.signin_profile.setHttpUserAgent(common_agent(self.signin_profile.httpUserAgent()))
        # No web page may reach the dashboard's API. The profile does not own its interceptor.
        self.signin_guard = Guard(self.signin_profile)
        self.signin_profile.setUrlRequestInterceptor(self.signin_guard)
        self.signin_profile.downloadRequested.connect(lambda item: item.cancel())
        self.signin_profile.clearHttpCacheCompleted.connect(self.cache_cleared)
        self.signin_clearing, self.signin_waiting = False, None
        store = self.signin_profile.cookieStore()
        store.cookieAdded.connect(self.cookie_added)
        store.cookieRemoved.connect(self.cookie_removed)
        store.loadAllCookies()
        self.signin_page = SignInPage(self.signin_profile, self.window)
        self.signin_page.newWindowRequested.connect(self.signin_new_window)
        self.signin_page.permissionRequested.connect(lambda permission: permission.deny())
        self.signin_page.loadingChanged.connect(self.signin_loading)
        self.signin_page.urlChanged.connect(self.signin_where)
        self.signin_page.renderProcessTerminated.connect(self.signin_render_gone)
        self.signin_view = q.QtWebEngineWidgets.QWebEngineView(self.window)
        self.signin_view.setPage(self.signin_page)
        self.back = widgets.QPushButton(SIGNIN_BACK)
        self.back.clicked.connect(self.signin_back)
        self.retry = widgets.QPushButton(SIGNIN_RETRY)
        self.retry.clicked.connect(self.signin_retry)
        self.where = widgets.QLabel('')
        self.hint = widgets.QLabel(site_text(SIGNIN_HINT, self.runtime.site))
        self.signin_message = widgets.QLabel('')
        self.signin_message.setAlignment(q.QtCore.Qt.AlignmentFlag.AlignCenter)
        self.signin_message.setWordWrap(True)
        self.hint.setWordWrap(True)
        panel = widgets.QWidget(self.window)
        header = widgets.QWidget(panel)
        header.setObjectName('signinHeader')
        header.setAttribute(q.QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setStyleSheet(signin_style(self.dark()))
        self.signin_header = header
        self.title = widgets.QLabel(APP_NAME)
        self.title.setObjectName('signinTitle')
        strip = widgets.QWidget(header)
        strip.setMaximumWidth(SIGNIN_BAR_WIDTH)
        # The strip takes all the width it may have; only what is left over goes to the two sides.
        centred = widgets.QHBoxLayout(header)
        centred.setContentsMargins(0, 0, 0, 0)
        centred.setSpacing(0)
        centred.addStretch(1)
        centred.addWidget(strip, 100000)
        centred.addStretch(1)
        # Laid out as the dashboard's header is: the app's name on the left, what can be done on the right.
        bar = widgets.QHBoxLayout(strip)
        bar.setContentsMargins(24, 10, 24, 10)
        bar.setSpacing(12)
        bar.addWidget(self.title)
        bar.addWidget(self.where)
        bar.addWidget(self.hint, 1)
        bar.addWidget(self.retry)
        bar.addWidget(self.back)
        column = widgets.QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(header)
        column.addWidget(self.signin_message, 1)
        column.addWidget(self.signin_view, 1)
        self.signin_probe = q.QtCore.QTimer(self.window)
        self.signin_probe.setInterval(SIGNIN_PROBE_MS)
        self.signin_probe.timeout.connect(self.signin_ask)
        self.stack.addWidget(panel)
        # Last, so that a build that failed has nothing connected to the page it removes.
        self.app.aboutToQuit.connect(self.signin_page.deleteLater)
        self.signin_panel = panel

    def signin_show(self, stage, opening=False):
        """page: Tapas's own page, or a note that it is opening. offline: a message and Try again. finishing: a message and no way back."""
        self.signin_stage, self.signin_opening = stage, opening
        self.signin_since = time.monotonic()
        self.signin_view.setVisible(stage == 'page' and not opening)
        self.where.setVisible(stage == 'page')
        self.hint.setVisible(stage == 'page' and not opening)
        self.signin_message.setText(
            site_text(SIGNIN_OPENING if opening else {'offline': SIGNIN_OFFLINE, 'finishing': SIGNIN_FINISHING}.get(stage, ''), self.runtime.site))
        self.signin_message.setVisible(stage != 'page' or opening)
        self.retry.setVisible(stage == 'offline')
        self.back.setVisible(stage != 'finishing')

    def signin_tick(self):
        """Pick up a sign-in the dashboard asked for, and notice one that ended without this window."""
        if self.phase != 'open':
            return
        if self.runtime.theme.get() != self.theme_choice:
            self.theme_apply()  # Changed in the dashboard's header.
        asked = self.runtime.location.take()
        if asked == 'open':
            self.q.QtGui.QDesktopServices.openUrl(self.q.QtCore.QUrl.fromLocalFile(self.runtime.location.get()))
        elif asked == 'choose':
            self.choose_location()
            return
        site = self.runtime.site_choice.take()
        if site is not None and site.key != self.runtime.site.key:
            self.choose_site(site)
            return
        if site is not None:
            self.runtime.site_choice.settle()  # Already open.
        signin = self.runtime.signin
        if signin.take():
            # The network policy is checked off this thread, before any Tapas page is shown.
            with self.signin_safe():
                self.background(signin.admit, self.signin_open)
        elif self.signin_stage in {'page', 'offline'} and signin.status().get('status') != 'running':
            self.signin_leave()  # Out of time.

    @contextlib.contextmanager
    def signin_safe(self):
        """A step of a sign-in that fails ends it, and the dashboard says so, instead of waiting out the time limit."""
        try:
            yield
        except Exception as error:
            LOG.error('a sign-in step failed: %s', app_runtime.describe(error))
            self.runtime.signin.fail('signin_failed')
            self.signin_leave()

    def signin_open(self):
        if self.phase != 'open' or self.runtime.signin.status().get('status') != 'running':
            return
        with self.signin_safe():
            if self.signin_panel is None:
                self.build_signin()
            self.forget_cookies()
            self.signin_page.history().clear()
            self.signin_heard = None
            self.signin_show('page', opening=True)
            self.stack.setCurrentWidget(self.signin_panel)
            self.signin_probe.start()
            self.signin_load()

    def signin_load(self):
        """Qt drops a page asked for while it is still emptying the cache, so the page waits for that."""
        self.signin_committed = False
        if self.signin_clearing:
            self.signin_waiting = True
        else:
            self.signin_view.load(self.q.QtCore.QUrl(self.runtime.site.signin_url))

    def cache_cleared(self):
        self.signin_clearing, waiting, self.signin_waiting = False, self.signin_waiting, None
        if waiting and self.phase == 'open' and self.signin_stage == 'page':
            self.signin_view.load(self.q.QtCore.QUrl(self.runtime.site.signin_url))

    def signin_where(self, url):
        # Tells the user whose page is showing, and the window that Tapas has answered.
        self.where.setText(host_text(url.scheme(), url.host()))
        if url.scheme() == 'https' and url.host():
            self.signin_committed = True

    def signin_slow(self):
        """The page has not finished opening in time: show what has arrived, or say so. Never an endless wait."""
        if self.signin_committed:
            self.signin_show('page')
            return
        LOG.warning('the Tapas sign-in page did not answer in time')
        self.signin_clearing, self.signin_waiting = False, None
        self.signin_show('offline')
        self.signin_view.stop()

    def signin_loading(self, info):
        if self.phase != 'open' or self.signin_stage != 'page':
            return
        kind = self.q.QtWebEngineCore.QWebEngineLoadingInfo
        status = info.status()
        failed = status == kind.LoadStatus.LoadFailedStatus
        if failed:
            domain = info.errorDomain()
            # The kind of failure and its number only: an address can carry a sign-in code.
            LOG.warning('a sign-in page did not finish loading: %s %s, engine error page: %s, page was showing: %s',
                        getattr(domain, 'name', domain), info.errorCode(), info.isErrorPage(), not self.signin_opening)
        # An error status with a page from the site, such as a visitor check, is still that site's page,
        # and so is a page that is already showing when the engine reports the rest of it as failed.
        if failed and (info.isErrorPage() or (
                self.signin_opening and info.errorDomain() != kind.ErrorDomain.HttpStatusCodeDomain)):
            # Offline, or Tapas cannot be reached: plain words and a way to retry, never a bare error page.
            self.signin_show('offline')
        elif status in (kind.LoadStatus.LoadSucceededStatus, kind.LoadStatus.LoadFailedStatus):
            if self.signin_opening:
                self.signin_show('page')
            self.signin_ask()

    def signin_ask(self):
        """After each page and every few seconds: read the answer to the last question."""
        if self.phase != 'open' or self.signin_stage != 'page':
            return
        if self.signin_opening and time.monotonic() - self.signin_since >= SIGNIN_OPEN_SECONDS:
            self.signin_slow()
            if self.signin_stage != 'page':
                return
        url = self.signin_page.url()
        if on_site(url.scheme(), url.host(), self.runtime.site):
            self.signin_page.runJavaScript(SIGNIN_READ, self.world, self.signin_answer)

    def signin_answer(self, text, waited=False):
        if self.phase != 'open' or self.signin_stage != 'page':
            return  # Qt also delivers this while a page is being torn down.
        found = signed_in(text, self.runtime.site)
        spoke = page_spoke(text, self.runtime.site)
        heard = 'an account' if found else 'no account yet' if spoke else self.signin_heard
        if heard != self.signin_heard:
            self.signin_heard = heard
            LOG.info('the %s page answered: %s', self.runtime.site.name, heard)
        if spoke and self.signin_opening:
            self.signin_show('page')  # It has arrived, whatever the engine still waits for.
        if found is None:
            url = self.signin_page.url()
            ask = ASKS.get(self.runtime.site.key)
            if ask is not None and on_site(url.scheme(), url.host(), self.runtime.site):
                self.signin_page.runJavaScript(ask, self.world)
            return
        key, storage = found
        if not waited and not self.cookies.settled():
            # A cookie the page has just dropped may only have been set again. That is known in a
            # moment, and a cookie Tapas really took away must not be saved.
            self.q.QtCore.QTimer.singleShot(int(CookieRecords.GRACE * 1000) + 100, lambda: self.signin_answer(text, True))
            return
        cookies = self.cookies.current()
        with self.signin_safe():
            self.signin_reset()
            self.signin_show('finishing')
            self.background(lambda: self.runtime.signin.finish(key, cookies, storage), self.signin_finished)

    def signin_finished(self):
        if self.signin_stage == 'finishing':
            self.signin_leave()

    def signin_leave(self):
        """Back to the dashboard, which says in its own words how the sign-in ended."""
        stage, self.signin_stage = self.signin_stage, None
        if stage in {'page', 'offline'}:
            try:
                self.signin_reset()
            except Exception as error:
                LOG.error('the sign-in page could not be cleared: %s', app_runtime.describe(error))
        if self.phase == 'open':
            self.stack.setCurrentWidget(self.home)

    def forget_cookies(self):
        # This empties the window's own copy. It does not sign the account out at Tapas.
        self.signin_profile.cookieStore().deleteAllCookies()
        self.cookies.clear()

    def signin_reset(self):
        """Leave nothing of the account in the window once the page is no longer needed."""
        self.signin_probe.stop()
        url = self.signin_page.url()
        if on_site(url.scheme(), url.host(), self.runtime.site):
            self.signin_page.runJavaScript(SIGNIN_FORGET, self.world)
        self.signin_view.load(self.q.QtCore.QUrl('about:blank'))
        self.forget_cookies()
        # Only here, with the page gone: Qt drops a page asked for while the cache is being emptied.
        self.signin_clearing, self.signin_waiting = True, None
        self.signin_profile.clearHttpCache()
        self.where.setText('')

    def signin_back(self, checked=False):
        if self.signin_stage in {'page', 'offline'}:
            LOG.info('sign-in was cancelled in the window')
            self.runtime.signin.cancel()
            self.signin_leave()

    def signin_retry(self, checked=False):
        if self.signin_stage == 'offline':
            self.signin_show('page', opening=True)
            self.signin_load()

    def signin_new_window(self, request):
        url = request.requestedUrl()
        # One page at a time: a link meant for another window opens here, anything else is dropped.
        if self.signin_stage == 'page' and url.scheme() == 'https' and url.host():
            self.signin_view.load(url)

    def signin_render_gone(self, status, code):
        normal = self.q.QtWebEngineCore.QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus
        if self.phase != 'open' or self.signin_stage not in {'page', 'offline'} or status == normal:
            return
        LOG.error('the sign-in page process stopped: %s, exit code %s', getattr(status, 'name', status), code)
        self.runtime.signin.fail('signin_failed')
        self.signin_leave()

    def cookie_added(self, cookie):
        self.cookies.added(cookie_record(cookie))
        if self.cookie_check:
            self.cookie_check()

    def cookie_removed(self, cookie):
        # Not always a removal: see CookieRecords.
        self.cookies.removed(cookie_record(cookie))

    def signin_check(self, done):
        """--self-test: one synthetic cookie goes through the real cookie store. No Tapas page is loaded."""
        q = self.q
        self.build_signin()
        self.cookie_check = done
        self.cookie_expires = int(time.time()) + 3600
        cookie = q.QtNetwork.QNetworkCookie(q.QtCore.QByteArray(SELF_TEST_COOKIE.encode()), q.QtCore.QByteArray(b'synthetic'))
        cookie.setDomain('.' + sites.DEFAULT.host)
        cookie.setPath('/')
        cookie.setSecure(True)
        cookie.setHttpOnly(True)
        cookie.setExpirationDate(q.QtCore.QDateTime.fromSecsSinceEpoch(self.cookie_expires))
        # A profile's cookie store starts working when the profile shows its first page.
        self.signin_view.load(q.QtCore.QUrl('about:blank'))
        self.signin_profile.cookieStore().setCookie(cookie, q.QtCore.QUrl(sites.DEFAULT.referer))

    def signin_checked(self):
        """Whether that cookie came back and converts to exactly what the downloader would be given."""
        expected = {'name': SELF_TEST_COOKIE, 'value': 'synthetic', 'domain': '.' + sites.DEFAULT.host, 'path': '/',
                    'expires': self.cookie_expires, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}
        try:
            return storage_state(self.cookies.current())['cookies'] == [expected]
        except ArchiveError:
            return False

    def confirm(self, kind):
        box = self.q.QtWidgets.QMessageBox
        buttons = box.StandardButton
        text = site_text(CLOSE_SIGNIN, self.runtime.site) if kind == 'signin' else CLOSE_DOWNLOAD
        return box.question(self.window, APP_NAME, text, buttons.Yes | buttons.No, buttons.No) == buttons.Yes

    def close_requested(self, event):
        kind = self.runtime.busy() if self.phase in {'starting', 'open'} else None
        action = close_action(self.phase, kind)
        if action == 'accept':
            event.accept()
            return
        # The window stays until the runtime has stopped, so it never just freezes or vanishes.
        event.ignore()
        if action == 'finish' or (action == 'confirm' and self.confirm(kind)):
            self.phase = 'stopping'
            self.clock.stop()
            self.signin_clock.stop()
            # Nothing is saved unless the last step of a sign-in had already begun.
            self.runtime.signin.cancel()
            if self.signin_stage:
                self.signin_leave()
            self.notice.setText(FINISHING)
            self.stack.setCurrentWidget(self.notice)
            self.background(self.runtime.stop, self.quit)

    def choose_location(self):
        """Asked for in the dashboard: pick another library folder, in the computer's own folder dialog."""
        if self.choosing:
            return  # The window's clock keeps running under the dialogs below.
        self.choosing = True
        try:
            self.choose_folder()
        finally:
            self.choosing = False

    def choose_folder(self):
        widgets = self.q.QtWidgets
        box, buttons = widgets.QMessageBox, widgets.QMessageBox.StandardButton
        if self.runtime.busy():
            box.information(self.window, APP_NAME, LOCATION_BUSY)
            return
        current = self.runtime.root
        chosen = widgets.QFileDialog.getExistingDirectory(self.window, LOCATION_TITLE, str(current.parent))
        if not chosen:
            return
        root = app_runtime.library_root(chosen)
        if root == current:
            return
        wording = LOCATION_EXISTING if app_runtime.is_library(root) else LOCATION_CONFIRM
        site = self.runtime.site
        text = wording.format(new=sites.folder(root, site) / 'Library', old=sites.folder(current, site) / 'Library')
        if box.question(self.window, APP_NAME, text, buttons.Yes | buttons.No, buttons.Yes) != buttons.Yes:
            return
        # The dialogs above let time pass.
        if self.phase == 'open' and not self.runtime.busy():
            self.relocate(root)

    def choose_site(self, site):
        """Asked for in the dashboard's list of sites: open that site's own library and sign-in behind the same page.

        The page stays in the window and reads the other site's library once the switch has settled.
        """
        choice = self.runtime.site_choice
        if self.runtime.busy():
            self.q.QtWidgets.QMessageBox.information(self.window, APP_NAME, SITE_BUSY.format(site=site.name))
            choice.settle()  # The list shows the site still open.
            return
        previous, outcome = self.runtime.site, []
        self.phase = 'moving'  # Closing waits, and no sign-in starts, until the switch is done.

        def done():
            self.phase = 'open'
            choice.settle()
            if not outcome:
                self.phase = 'failed'
                self.show_notice(LOAD_FAILED)
                self.show(self.notice)
            elif outcome[0]:
                self.q.QtWidgets.QMessageBox.information(self.window, APP_NAME,
                                                         SITE_FAILED.format(site=previous.name, reason=outcome[0]))
        self.background(lambda: outcome.append(self.runtime.change_site(site)), done)

    def relocate(self, root):
        """Carry the window over to another library folder; the dashboard comes back once that one is ready."""
        previous, moved = self.runtime, []
        self.phase = 'moving'
        self.clock.stop()
        self.signin_clock.stop()
        self.notice.setText(MOVING)
        self.show(self.notice)

        def done():
            if not moved:
                # Neither folder could be opened again; closing still works and the log has the reason.
                self.phase = 'failed'
                self.show_notice(LOAD_FAILED)
                return
            self.runtime, problem = moved[0]
            issued = self.issued
            self.issued, self.phase = None, 'starting'
            self.load()
            if problem:
                # Back on the old folder, whose page cookie is still the one issued earlier.
                self.issued, self.renewing = issued, issued is None
                self.q.QtWidgets.QMessageBox.information(self.window, APP_NAME, LOCATION_FAILED.format(reason=problem))
        self.background(lambda: moved.append(app_runtime.move(previous, root)), done)

    def quit(self):
        self.phase = 'done'
        self.app.quit()


def run(q, project_root):
    log_path = app_runtime.configure_logging()
    LOG.info('starting version %s', version())
    app = application(q)
    lock = runtime = shell = None
    try:
        state = app_runtime.state_dir()
        lock = q.QtCore.QLockFile(str(state / 'desktop.lock'))
        lock.setStaleLockTime(0)  # A download runs for days; only a dead owner makes the lock stale.
        name = server_name(state)
        if not claim(q, lock, name):
            return 0
        chosen = None if project_root else app_runtime.saved_root()
        missing = chosen is not None and not app_runtime.present(chosen)
        if missing:
            # A folder chosen in the app can be on a drive that is not connected today. Asked before
            # anything is created, so an empty library never quietly appears in its place.
            LOG.warning('the chosen library folder is not there')
            if not standard_instead(q, chosen):
                return 0
            chosen = None
        runtime = app_runtime.Runtime(Path(project_root).expanduser() if project_root
                                      else chosen or app_runtime.default_root(), site=app_runtime.saved_site())
        shell = Shell(q, app, runtime, log_path)
        shell.listen(name)
        runtime.start()
        if missing:
            app_runtime.remember_root(None)
        shell.open()
        # The dashboard appears only afterwards, so Sign in cannot race the downloader start.
        shell.background(runtime.start_worker, shell.load)
        return app.exec()
    except Exception as error:
        LOG.error('startup failed: %s', app_runtime.describe(error))
        text = failure_text(error, log_path)
    finally:
        if shell is not None:
            runtime = shell.runtime  # Another one after the library folder was changed.
        if runtime is not None:
            # A no-op after an ordinary close, which already stopped the runtime with its full wait.
            runtime.stop(wait_seconds=0)
        if lock is not None:
            lock.unlock()
    q.QtWidgets.QMessageBox.critical(None, APP_NAME, text)
    return 1


def raised_in(error):
    """The app's own module and line an error last came through, for a report that names no one's files.

    The error's message is left out: it may hold a path or a title.
    """
    package = __name__.partition('.')[0]
    own = [f"{frame.f_globals['__name__']}:{line}" for frame, line in traceback.walk_tb(error.__traceback__)
           if str(frame.f_globals.get('__name__')).partition('.')[0] == package]
    return own[-1] if own else None


def self_test(q):
    """Show the real dashboard and build the real sign-in panel: empty temporary library, no downloader, no Tapas."""
    for name, value in (('QT_QPA_PLATFORM', 'offscreen'), ('QT_QUICK_BACKEND', 'software'),
                        ('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')):
        os.environ.setdefault(name, value)
    results, seen = [], []

    def abandon():
        # The event loop itself is stuck, so no orderly exit is possible.
        print(json.dumps(self_test_result(False, None, True)[0]), flush=True)
        os._exit(1)
    watchdog = threading.Timer(SELF_TEST_SECONDS + 15, abandon)
    watchdog.daemon = True
    watchdog.start()
    try:
        with tempfile.TemporaryDirectory(prefix='comic-archiver-self-test-', ignore_cleanup_errors=True) as temporary:
            runtime = app_runtime.Runtime(Path(temporary).resolve() / 'library')
            try:
                app = application(q)

                def settle(timed_out=False):
                    """Report once the dashboard and the sign-in cookie have both answered, or time is up."""
                    if results:
                        return
                    cookie = shell.signin_checked()
                    dashboard = seen[0] if seen else (False, None)
                    if timed_out or (seen and (cookie or dashboard != (True, True))):
                        # Out of time with a good dashboard means the cookie check is what failed.
                        results.append(self_test_result(*dashboard, timed_out and not seen, cookie))
                        shell.quit()

                def answered(loaded, found):
                    seen.append((loaded, found))
                    settle()
                shell = Shell(q, app, runtime, probe=answered)
                runtime.start()
                shell.open()
                q.QtCore.QTimer.singleShot(SELF_TEST_SECONDS * 1000, lambda: settle(True))
                shell.signin_check(settle)
                shell.load()
                app.exec()
            except Exception as error:
                results.insert(0, ({'ok': False, 'reason': type(error).__name__, 'where': raised_in(error)}, 1))
            finally:
                runtime.stop(wait_seconds=0)
    finally:
        watchdog.cancel()
    report, status = results[0] if results else self_test_result(False, None)
    print(json.dumps(report), flush=True)
    return status


def main(argv=None):
    args = arguments(argv)
    if osdep.WINDOWS:
        osdep.python_environment(os.environ)
    if args.version:
        print(f'{APP_NAME} {version()}')
        return 0
    try:
        q = qt()
    except ImportError as error:
        return toolkit_missing(error, args.self_test)
    return self_test(q) if args.self_test else run(q, args.project_root)


if __name__ == '__main__':
    sys.exit(main())
