"""The hidden downloader's browser, on the Qt web engine the app's window already uses.

The Tapas adapter is written against Playwright's synchronous interface. This
module offers the part of that interface the desktop app's download uses
(pages, scripts, element reads, real pointer clicks, request refusal), so the
adapter runs unchanged and the app carries no second browser.

Qt belongs to the process's main thread. serve() gives that thread to Qt and
runs the caller's work on another; each engine call hands its Qt part to the
main thread and waits for the answer. Scripts run in a world of their own:
they read the page's document and send requests as the page would, and the
page's own scripts cannot see them.

Not offered: response bodies (Qt exposes none), text and role selectors,
frames, keyboard input. A caller that needs those uses Playwright.
"""
import collections
import ctypes
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from types import SimpleNamespace

VIEWPORT = (1280, 720)
DEFAULT_TIMEOUT = 30000
# Seconds between looks at the page while waiting for it.
STEP = 0.03
SCRIPT_SECONDS = 10
# Playwright waits for a page script for ever; a page that never answers is reported instead.
EVALUATE_SECONDS = 45
# Seconds Qt is given to close down once the work is over.
LINGER = 20
# What Qt asks through a page's own confirm when a page wants to keep its reader from leaving.
LEAVING = 'Are you sure you want to leave this page? Changes that you made may not be saved.'
ORIGIN_ROUTE = '**/*'
SAME_SITE = {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None', 'None_': 'None'}
# Qt's request kinds in Playwright's words. Qt reports a page's fetch() as Xhr.
RESOURCE_KINDS = {
    'ResourceTypeMainFrame': 'document', 'ResourceTypeSubFrame': 'document',
    'ResourceTypeNavigationPreloadMainFrame': 'document', 'ResourceTypeNavigationPreloadSubFrame': 'document',
    'ResourceTypeStylesheet': 'stylesheet', 'ResourceTypeScript': 'script', 'ResourceTypeImage': 'image',
    'ResourceTypeFavicon': 'image', 'ResourceTypeFontResource': 'font', 'ResourceTypeMedia': 'media',
    'ResourceTypeXhr': 'xhr', 'ResourceTypePing': 'ping', 'ResourceTypeCspReport': 'cspreport',
    'ResourceTypeWebSocket': 'websocket'}
# These variables can turn off Chromium's sandbox or other browser boundaries.
# The app supplies its few required engine switches directly instead of trusting
# ambient process configuration inherited from a shell or desktop launcher.
UNSAFE_QT_ENVIRONMENT = ('QTWEBENGINE_DISABLE_SANDBOX', 'QTWEBENGINE_CHROMIUM_FLAGS',
                         'QTWEBENGINE_REMOTE_DEBUGGING', 'QTWEBENGINEPROCESS_PATH')
# A page's reads: a refused one is worth a note for the log. Qt reports fetch() as Xhr.
READS = ('document', 'xhr', 'fetch')
# What started a request, as the page's own timing records name it, in Playwright's words.
INITIATORS = {'navigation': 'document', 'iframe': 'document', 'frame': 'document', 'script': 'script',
              'fetch': 'fetch', 'xmlhttprequest': 'xhr', 'img': 'image', 'image': 'image', 'beacon': 'ping'}
UNSUPPORTED_SELECTOR = re.compile(r'>>|^(?:text|xpath|role|id|css|data-testid)=|^//|:(?:has-text|text|text-is|nth-match)\(|:visible')

# Where a page is and how far its load has got. DOMContentLoaded has run once its end time is set.
WHERE = """(() => {
  const entry = performance.getEntriesByType('navigation')[0];
  const state = document.readyState;
  return JSON.stringify({id: performance.timeOrigin, url: location.href, complete: state === 'complete',
    ready: state === 'complete' || (state === 'interactive' && (!entry || entry.domContentLoadedEventEnd > 0)),
    status: entry ? entry.responseStatus || 0 : 0});
})()"""
# What this document has requested since the last look: address, what started it and the status. No contents.
REQUESTS = """(() => {
  const from = performance.timeOrigin === %s ? %d : 0;
  const entry = performance.getEntriesByType('navigation')[0];
  return JSON.stringify({id: performance.timeOrigin, url: location.href, status: entry ? entry.responseStatus || 0 : 0,
    rows: performance.getEntriesByType('resource').slice(from).map(e => [e.name, e.initiatorType, e.responseStatus || 0])});
})()"""
STORAGE = """(() => {
  try {
    if (!/^https?:$/.test(location.protocol)) return 'null';
    const rows = [];
    for (let index = 0; index < localStorage.length; index++) {
      const name = localStorage.key(index);
      rows.push([name, localStorage.getItem(name)]);
    }
    return JSON.stringify([location.origin, rows]);
  } catch (error) { return 'null'; }
})()"""
RESTORE = """(() => {
  const saved = %s[location.origin];
  if (!saved) return;
  try {
    for (const [name, value] of saved) if (localStorage.getItem(name) === null) localStorage.setItem(name, value);
  } catch (error) {}
})()"""
# Calls a script as Playwright does: a function is called with the arguments, anything else is the value.
# A promise is parked under a number and collected by TAKE once it settles. Only an error's name leaves the page.
CALL = """(() => {
  const box = Object.hasOwn(globalThis, 'comicArchive') ? globalThis.comicArchive : (globalThis.comicArchive = {next: 0, done: {}});
  const pack = value => JSON.stringify({value: value === undefined ? null : value});
  const fail = error => JSON.stringify({error: String((error && error.name) || 'Error')});
  const keep = (id, make) => { try { box.done[id] = make(); } catch (error) { box.done[id] = fail(error); } };
  try {
    %(prepare)s
    const made = (%(source)s);
    const result = typeof made === 'function' ? made(%(arguments)s) : made;
    if (result && typeof result.then === 'function') {
      const id = ++box.next;
      result.then(value => keep(id, () => pack(value)), error => keep(id, () => fail(error)));
      return JSON.stringify({pending: id});
    }
    return pack(result);
  } catch (error) { return fail(error); }
})()"""
# 'gone' when the document that was asked has been replaced: its promise will never be heard from.
TAKE = """(() => {
  const box = Object.hasOwn(globalThis, 'comicArchive') ? globalThis.comicArchive : null, id = %d;
  if (!box) return 'gone';
  if (!(id in box.done)) return '';
  const text = box.done[id];
  delete box.done[id];
  return text;
})()"""
# The elements a locator names: CSS from the document down, a step to the parent, or one by position.
FIND = """const find = steps => {
  let nodes = [document];
  for (const [kind, value] of steps) {
    if (kind === 'css') {
      const found = new Set();
      for (const node of nodes) for (const element of node.querySelectorAll(value)) found.add(element);
      const ordered = nodes.length > 1;
      nodes = [...found];
      if (ordered) nodes.sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    } else if (kind === 'parent') {
      nodes = [...new Set(nodes.map(node => node.parentElement).filter(Boolean))];
    } else {
      const at = value < 0 ? nodes.length + value : value;
      nodes = at >= 0 && at < nodes.length ? [nodes[at]] : [];
    }
  }
  return nodes;
};"""
ONE = 'nodes => nodes.length === 1 ? {found: true, value: (%s)(nodes[0])} : {found: false, count: nodes.length}'
VISIBLE = """e => {
  const box = e.getBoundingClientRect();
  return box.width > 0 && box.height > 0 && getComputedStyle(e).visibility === 'visible';
}"""
BOX = """e => {
  const box = e.getBoundingClientRect();
  return box.width > 0 || box.height > 0 ? {x: box.x, y: box.y, width: box.width, height: box.height} : null;
}"""
# Scrolls only when the element is not already wholly in view, as Playwright does.
SCROLL = """e => {
  const box = e.getBoundingClientRect();
  if (box.top < 0 || box.left < 0 || box.bottom > innerHeight || box.right > innerWidth)
    e.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  return true;
}"""
# Where to press: the middle of the element's first visible box, and whether a press there would reach it.
AIM = """e => {
  const boxes = () => [...e.getClientRects()].filter(box => box.width > 0 && box.height > 0);
  let box = boxes()[0];
  if (!box) return {ready: false};
  if (box.top < 0 || box.left < 0 || box.bottom > innerHeight || box.right > innerWidth) {
    e.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    box = boxes()[0];
    if (!box) return {ready: false};
  }
  const x = Math.min(Math.max(box.left + box.width / 2, 0), innerWidth - 1);
  const y = Math.min(Math.max(box.top + box.height / 2, 0), innerHeight - 1);
  const hit = document.elementFromPoint(x, y);
  return {x, y, width: box.width, height: box.height,
          ready: getComputedStyle(e).visibility === 'visible' && !e.disabled && !!hit && (hit === e || e.contains(hit))};
}"""
# Arms the page for one press at the place AIM gave, if the element is still there: until the click has been
# and gone, a press that lands on anything else is stopped before the page or the site hears of it.
# 'box' is CALL's own; box.hit tells afterwards whether the press reached the element.
GUARD = """e => {
  const aim = (%s)(e), wanted = %s;
  if (!aim.ready || aim.x !== wanted.x || aim.y !== wanted.y) return false;
  if (box.release) box.release();
  const kinds = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick', 'contextmenu'];
  const watch = event => {
    if (!(event.target instanceof Node) || !e.contains(event.target)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      box.hit = false;
    } else if (event.type === 'click' && box.hit === 'armed') box.hit = true;
    if (event.type === 'click' && box.release) box.release();
  };
  const timer = setTimeout(() => box.release && box.release(), 2000);
  box.release = () => {
    clearTimeout(timer);
    for (const kind of kinds) removeEventListener(kind, watch, true);
    box.release = null;
  };
  for (const kind of kinds) addEventListener(kind, watch, true);
  box.hit = 'armed';
  return true;
}"""

_serving = None
# Qt's objects are torn down in a safe order only while the application outlives them.
_application = None


class Error(Exception):
    """An engine failure. Its text never carries page content, an address or a credential."""


class TimeoutError(Error):
    """Named as Playwright names it: callers tell a timeout from other failures by this name."""


def secure_qt_environment(environ):
    """Remove inherited switches that can weaken or replace the embedded browser."""
    for name in UNSAFE_QT_ENVIRONMENT:
        environ.pop(name, None)


def serve(work, arguments=()):
    """Run work() on a second thread while this, the main thread, belongs to Qt; return what it returns.

    Qt is started only if the work launches the engine. 'arguments' are extra
    engine arguments for a test harness; the app passes none.
    """
    global _serving
    if _serving is not None or threading.current_thread() is not threading.main_thread():
        raise Error('The built-in engine is served once, from the main thread.')
    secure_qt_environment(os.environ)
    loop = _serving = Loop(arguments)
    outcome, left = {}, threading.Event()

    def worker():
        try:
            outcome['value'] = work()
        except BaseException as error:
            outcome['error'] = error
        finally:
            loop.finish()
            # Qt has been asked to end. Should it hang instead, a process whose work is done, and which
            # nothing watches any more, must not stay behind.
            if not left.wait(LINGER):
                _abandon(outcome)

    # A daemon: a second interrupt ends the process without waiting for work that is blocked on the engine.
    thread = loop.work = threading.Thread(target=worker, name='tapas-downloader', daemon=True)
    try:
        thread.start()
        loop.run()
    finally:
        _serving = None
        left.set()
    thread.join()
    if 'error' in outcome:
        raise outcome['error']
    return outcome.get('value')


def _abandon(outcome):
    """End the process with the status its work earned, without waiting for Qt."""
    error, value = outcome.get('error'), outcome.get('value')
    if isinstance(error, SystemExit):
        error, value = None, error.code
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(1 if error is not None or not isinstance(value, (int, type(None))) else value or 0)


class Loop:
    """The main thread's part: bring Qt up when asked, then run what other threads hand over."""

    def __init__(self, arguments=()):
        self.arguments = list(arguments)
        self.thread = threading.current_thread()
        self.requests = queue.SimpleQueue()
        self.started = threading.Event()
        self.guard = threading.Lock()
        self.headless = self.failure = self.q = self.app = self.bridge = self.tick = self.work = None
        self.interrupted = self.forced = self.over = self.ended = False
        self.surfaces = []

    def run(self):
        previous = signal.signal(signal.SIGINT, self.interrupt)
        try:
            headless = self.requests.get()
            if headless is None:
                return
            try:
                self.open(headless)
            except Exception as error:
                self.failure = error
            self.started.set()
            if self.failure is not None:
                self.requests.get()
            else:
                # Work that ended while Qt was still starting has nobody left to end the loop.
                if not self.over:
                    self.app.exec()
                self.close()
            if self.forced:
                raise KeyboardInterrupt
        finally:
            signal.signal(signal.SIGINT, previous)

    def interrupt(self, *_):
        """The first interrupt goes to the work, which then ends as it would without the engine; a second ends Qt at once."""
        work = self.work
        # Work that is over is only waiting for Qt to end; an interrupt then is for Qt.
        if not self.interrupted and not self.over and work is not None and work.is_alive():
            self.interrupted = True
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(work.ident), ctypes.py_object(KeyboardInterrupt))
            return
        self.forced = True
        if self.app is None:
            raise KeyboardInterrupt
        # Queued: Qt ignores a quit that is asked for before its loop runs.
        self.q.QtCore.QTimer.singleShot(0, self.app.quit)

    def open(self, headless):
        global _application
        from PySide6 import QtCore, QtGui, QtNetwork, QtWebEngineCore, QtWebEngineWidgets, QtWidgets
        self.q = SimpleNamespace(QtCore=QtCore, QtGui=QtGui, QtNetwork=QtNetwork, QtWebEngineCore=QtWebEngineCore,
                                 QtWebEngineWidgets=QtWebEngineWidgets, QtWidgets=QtWidgets)
        arguments = ['comic-archiver-downloader']
        # Qt hands the engine only the arguments that follow this marker; those before it are Qt's own.
        switches = ['--webEngineArgs']
        if headless:
            # No window system and no graphics card are needed to read pages; without them the
            # engine starts the same way on every computer. Arguments, not environment variables:
            # those would pass to every program the downloader starts.
            from PySide6.QtQuick import QQuickWindow
            QQuickWindow.setSceneGraphBackend('software')
            arguments += ['-platform', 'offscreen']
            switches.append('--disable-gpu')
        # Never '--no-sandbox': the engine starts without it wherever this has been tried, and pages are web content.
        self.app = _application = QtWidgets.QApplication(arguments + switches + self.arguments)
        self.app.setQuitOnLastWindowClosed(False)
        # The sign-in window connects directly, and the session it saved belongs to that route.
        QtNetwork.QNetworkProxyFactory.setUseSystemConfiguration(False)

        class Bridge(QtCore.QObject):
            run = QtCore.Signal(object)

            def __init__(self):
                super().__init__()
                self.run.connect(self.carry, QtCore.Qt.ConnectionType.QueuedConnection)

            @QtCore.Slot(object)
            def carry(self, job):
                job()

        self.bridge = Bridge()
        # Python sees an interrupt only while it runs; this keeps it running now and then.
        self.tick = QtCore.QTimer()
        self.tick.timeout.connect(lambda: None)
        self.tick.start(200)

    def close(self):
        """After the loop has ended: pages go before their profiles, and both before the application."""
        # Work that is still going (after an interrupt) must not reach a page that is being taken down.
        self.ended = True
        for surface in list(self.surfaces):
            surface.close()
        deferred = self.q.QtCore.QEvent.Type.DeferredDelete.value
        for _ in range(3):
            self.app.sendPostedEvents(None, deferred)
            self.app.processEvents()

    def start(self, headless, seconds=15):
        """From a work thread: have the main thread bring Qt up. The first caller decides hidden or shown."""
        with self.guard:
            if self.headless is None:
                self.headless = bool(headless)
                self.requests.put(self.headless)
        if not self.started.wait(seconds):
            raise Error('The built-in engine did not start in time.')
        if self.failure is not None:
            missing = isinstance(self.failure, ImportError)
            raise Error('The built-in engine is not installed.' if missing else 'The built-in engine could not start.')
        if bool(headless) != self.headless:
            raise Error('The built-in engine is already running ' + ('hidden.' if self.headless else 'in a window.'))

    def finish(self):
        """From the work thread, when the work is over."""
        with self.guard:
            asked = self.headless is not None
            self.headless = False if self.headless is None else self.headless
        # Qt may still be starting. It then sees this and does not enter its loop, which nobody would end.
        self.over = True
        self.requests.put(None)
        if asked and self.started.wait(LINGER) and self.failure is None:
            self.bridge.run.emit(self.app.quit)

    def call(self, function, seconds=30):
        """Run function on Qt's thread and return what it returns."""
        if threading.current_thread() is self.thread:
            return function()
        if self.bridge is None or self.ended:
            raise Error('The built-in engine is not running.')
        box, done = {}, threading.Event()

        def job():
            try:
                if self.ended:
                    raise Error('The built-in engine is not running.')
                box['value'] = function()
            except BaseException as error:
                box['error'] = error
            done.set()

        self.bridge.run.emit(job)
        if not done.wait(seconds):
            raise TimeoutError('The built-in engine did not answer.')
        if 'error' in box:
            raise box['error']
        return box.get('value')

    def ask(self, begin, seconds):
        """begin(answer) runs on Qt's thread and starts something; the value later given to answer comes back."""
        box, done, deadline = [], threading.Event(), time.monotonic() + seconds

        def answer(value=None):
            box.append(value)
            done.set()

        # One limit for both parts, the hand-over to Qt and the answer.
        self.call(lambda: begin(answer), seconds)
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError('The page did not answer.')
        return box[0]


class CookieRecords(dict):
    """The cookies a store holds, by (name, domain, path), kept from its added and removed signals.

    Qt reports a cookie that a site sets again unchanged as removed twice and
    never as added. So a removal is held back: a second one for the same cookie
    means it is still there, and only a removal that stays single is real.
    """
    # How long a removal stays in doubt. The two reports of one re-set arrive together.
    GRACE = 1.0

    def __init__(self, clock=time.monotonic):
        super().__init__()
        self.leaving, self.clock = {}, clock

    @staticmethod
    def key(record):
        return record['name'], record['domain'], record['path']

    def added(self, record):
        key = self.key(record)
        self.leaving.pop(key, None)
        self[key] = record

    def removed(self, record):
        key = self.key(record)
        if key in self:
            self.leaving[key] = (self.pop(key), self.clock())
        elif key in self.leaving:
            held, since = self.leaving.pop(key)
            if held.get('value') == record.get('value') and self.clock() - since < self.GRACE:
                self[key] = record

    def settled(self):
        """Whether every removal has been decided. Until then current() also lists what may yet prove to be gone."""
        now = self.clock()
        return not any(now - since < self.GRACE for _, since in self.leaving.values())

    def current(self):
        now = self.clock()
        return [*self.values(), *(record for record, since in self.leaving.values() if now - since < self.GRACE)]

    def clear(self):
        super().clear()
        self.leaving.clear()


def saved_cookie(cookie):
    """A QNetworkCookie in the saved session's shape."""
    return {'name': bytes(cookie.name().data()).decode('utf-8', 'replace'),
            'value': bytes(cookie.value().data()).decode('utf-8', 'replace'),
            'domain': cookie.domain(), 'path': cookie.path() or '/',
            'expires': -1 if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch(),
            'httpOnly': cookie.isHttpOnly(), 'secure': cookie.isSecure(),
            'sameSite': SAME_SITE.get(cookie.sameSitePolicy().name, 'Lax')}


def common_agent(text):
    """What the engine calls itself, without Qt's own tag: the name of the browser it is built from."""
    return re.sub(r' QtWebEngine/\S+', '', text)


class Surface:
    """One off-the-record profile with its page and view. Everything here runs on Qt's thread."""

    def __init__(self, loop, refuse_workers=False):
        q, core = loop.q, loop.q.QtWebEngineCore
        surface = self

        class Refusals(core.QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                if surface.refuses(info):
                    info.block(True)

        class Quiet(core.QWebEnginePage):
            # A page's own dialogs would wait for a person who is not there.
            def javaScriptAlert(self, origin, message):
                pass

            def javaScriptConfirm(self, origin, message):
                # Qt puts a page's wish to keep its reader from leaving through here as well; nobody is reading.
                return message == q.QtCore.QCoreApplication.translate('QWebEnginePage', LEAVING)

            def javaScriptPrompt(self, origin, message, default):
                return False, ''

            def javaScriptConsoleMessage(self, level, message, line, source):
                pass

            def chooseFiles(self, mode, old, accepted):
                return []

        self.loop, self.refuse_workers = loop, refuse_workers
        self.decide = lambda request: False
        self.cookies, self.failures, self.gone, self.closed, self.storage = CookieRecords(), 0, False, False, None
        self.ended, self.failure = 0, None
        # Requests this app's own rules refused lately, for the log: fixed words, never an address.
        self.notes = collections.deque(maxlen=8)
        self.same_site = {}
        self.world = int(core.QWebEngineScript.ScriptWorldId.ApplicationWorld)
        # Qt saves nothing of this profile: state.json stays the only saved sign-in.
        self.profile = core.QWebEngineProfile(loop.app)
        # Many people use the browser this engine is built from; few use anything that carries Qt's tag.
        self.agent = common_agent(self.profile.httpUserAgent())
        self.profile.setHttpUserAgent(self.agent)
        # The profile does not own its interceptor.
        self.requests = Refusals(self.profile)
        self.profile.setUrlRequestInterceptor(self.requests)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self.cookie_added)
        store.cookieRemoved.connect(self.cookie_removed)
        self.page = Quiet(self.profile, loop.app)
        self.page.permissionRequested.connect(lambda permission: permission.deny())
        self.page.renderProcessTerminated.connect(self.lost)
        self.page.loadingChanged.connect(self.loading)
        self.page.setAudioMuted(True)
        # A link to another program (mail, an app) opens nothing.
        self.page.settings().setUnknownUrlSchemePolicy(
            core.QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)
        # A page that is not shown stops drawing and timing; hidden runs show it on the off-screen platform.
        self.view = q.QtWebEngineWidgets.QWebEngineView()
        self.view.setPage(self.page)
        self.view.resize(*VIEWPORT)
        self.view.show()
        loop.surfaces.append(self)

    def refuses(self, info):
        try:
            kind = info.resourceType().name
            if self.refuse_workers and kind == 'ResourceTypeServiceWorker':
                return True
            request = Request(bytes(info.requestUrl().toEncoded()).decode('ascii', 'replace'),
                              bytes(info.requestMethod()).decode('ascii', 'replace'), RESOURCE_KINDS.get(kind, 'other'))
            refused = bool(self.decide(request))
        except Exception as error:
            # A rule that fails has not allowed the request.
            self.note(f'a request rule failed: {type(error).__name__}')
            return True
        if refused and request.method == 'GET' and request.resource_type in READS:
            self.note('a request rule refused a read')
        return refused

    def note(self, text):
        self.notes.append((time.monotonic(), text))

    def cookie_added(self, cookie):
        self.cookies.added(self.record(cookie))

    def cookie_removed(self, cookie):
        self.cookies.removed(self.record(cookie))

    def record(self, cookie):
        record = saved_cookie(cookie)
        # Qt never says how a cookie is limited across sites. One that came from the saved session keeps what was saved.
        if cookie.sameSitePolicy().name == 'Default':
            record['sameSite'] = self.same_site.get(CookieRecords.key(record), record['sameSite'])
        return record

    def set_cookie(self, record):
        q = self.loop.q
        cookie = q.QtNetwork.QNetworkCookie(record['name'].encode(), record['value'].encode())
        domain, secure = record['domain'], record.get('secure') is True
        # A leading dot marks a cookie for the whole domain; without one it belongs to that host alone,
        # which Qt takes from the address it is set for.
        if domain.startswith('.'):
            cookie.setDomain(domain)
        cookie.setPath(record.get('path') or '/')
        cookie.setSecure(secure)
        cookie.setHttpOnly(record.get('httpOnly') is True)
        policy = {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None_'}.get(record.get('sameSite'))
        if policy is not None:
            # The engine refuses a cookie marked for every site that is not also a secure one. Firefox
            # saves that mark for any cookie whose site gave none, so there it is left off.
            if policy != 'None_' or secure:
                cookie.setSameSitePolicy(getattr(q.QtNetwork.QNetworkCookie.SameSite, policy))
            self.same_site[record['name'], domain, cookie.path()] = record['sameSite']
        expires = record.get('expires')
        if expires is not None and expires != -1:
            cookie.setExpirationDate(q.QtCore.QDateTime.fromSecsSinceEpoch(int(expires)))
        origin = ('https://' if secure else 'http://') + domain.lstrip('.') + '/'
        self.profile.cookieStore().setCookie(cookie, q.QtCore.QUrl(origin))

    def restore_storage(self, origins):
        """Put saved page storage back before the page's own scripts first run."""
        core = self.loop.q.QtWebEngineCore
        script = core.QWebEngineScript()
        script.setName('comic-archive-storage')
        script.setInjectionPoint(core.QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(self.world)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(RESTORE % json.dumps(origins))
        self.page.scripts().insert(script)
        self.storage = script

    def storage_restored(self):
        """Once is enough: a later page must be free to remove what it stored."""
        if self.storage is not None:
            self.page.scripts().remove(self.storage)
            self.storage = None

    def load(self, url):
        # A page whose process has ended is given a new one by loading.
        self.gone = False
        self.page.load(self.loop.q.QtCore.QUrl(url))

    def loading(self, info):
        # A load that failed, not one stopped to make way for the next. A site's own error page counts
        # here too, but it arrives as a document and is read as one.
        kinds = self.loop.q.QtWebEngineCore.QWebEngineLoadingInfo.LoadStatus
        if info.status() != kinds.LoadStartedStatus:
            self.ended += 1
        if info.status() == kinds.LoadFailedStatus:
            self.failures += 1
            # Which failure, as two numbers (Qt's kind of error and the engine's code): no address, no text.
            try:
                self.failure = f'{int(info.errorDomain().value)}/{int(info.errorCode())}'
            except (AttributeError, TypeError, ValueError):
                self.failure = None

    def lost(self, *_):
        self.gone = True

    def script(self, code, answer):
        self.page.runJavaScript(code, self.world, answer)

    def target(self):
        """The widget that receives input for the page."""
        return self.view.focusProxy() or self.view

    def pointer(self, kind, x, y):
        """A real pointer event, as from a mouse: the page cannot tell it from a person's."""
        q = self.loop.q
        event, buttons = q.QtCore.QEvent.Type, q.QtCore.Qt.MouseButton
        kinds = {'move': (event.MouseMove, buttons.NoButton, buttons.NoButton),
                 'press': (event.MouseButtonPress, buttons.LeftButton, buttons.LeftButton),
                 'release': (event.MouseButtonRelease, buttons.LeftButton, buttons.NoButton)}
        target, spot = self.target(), q.QtCore.QPointF(x, y)
        kind, button, held = kinds[kind]
        q.QtWidgets.QApplication.sendEvent(target, q.QtGui.QMouseEvent(
            kind, spot, target.mapToGlobal(spot), button, held, q.QtCore.Qt.KeyboardModifier.NoModifier))

    def wheel(self, x, y, delta_x, delta_y):
        q = self.loop.q
        target, spot = self.target(), q.QtCore.QPointF(x, y)
        turn = q.QtCore.QPoint(-int(delta_x), -int(delta_y))
        q.QtWidgets.QApplication.sendEvent(target, q.QtGui.QWheelEvent(
            spot, target.mapToGlobal(spot), turn, turn, q.QtCore.Qt.MouseButton.NoButton,
            q.QtCore.Qt.KeyboardModifier.NoModifier, q.QtCore.Qt.ScrollPhase.NoScrollPhase, False))

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self in self.loop.surfaces:
            self.loop.surfaces.remove(self)
        self.view.close()
        # In this order: a profile must outlive its page.
        self.view.deleteLater()
        self.page.deleteLater()
        self.profile.deleteLater()


class Request:
    def __init__(self, url, method='GET', resource_type='other'):
        self.url, self.method, self.resource_type = url, method, resource_type


class Route:
    """One handler's say on a request. Qt can refuse a request or let it go, not change it."""

    def __init__(self, request):
        self.request, self.outcome = request, None

    def settle(self, outcome):
        if self.outcome is not None:
            raise Error('Route is already handled!')
        self.outcome = outcome

    def abort(self, *_):
        self.settle('abort')

    def continue_(self, **changes):
        if changes:
            raise Error('The built-in engine cannot change a request.')
        self.settle('continue')

    def fallback(self, **changes):
        if changes:
            raise Error('The built-in engine cannot change a request.')
        self.settle('fallback')


class Response:
    """A response's address and status. Qt gives no headers and no contents."""

    def __init__(self, url, status, resource_type='document', method='GET'):
        self.url, self.status, self.ok, self.headers = url, status, 200 <= status < 300, {}
        self.request = Request(url, method, resource_type)

    def body(self):
        raise Error('The built-in engine cannot read a response body.')

    text = json = body


class Driver:
    """What session.browser_api hands out for this engine: Playwright's start, launcher and stop in one."""

    def start(self):
        return self

    def stop(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()

    @property
    def qt(self):
        return self

    def launch(self, headless=False, timeout=15000):
        loop = _serving
        if loop is None:
            raise Error('The built-in engine runs only inside the downloader.')
        loop.start(headless, timeout / 1000)
        return Browser(loop)


class Browser:
    def __init__(self, loop):
        self.loop, self.contexts = loop, []

    def new_context(self, storage_state=None, service_workers='allow'):
        context = Context(self.loop, storage_state, refuse_workers=service_workers == 'block')
        self.contexts.append(context)
        return context

    def close(self):
        contexts, self.contexts = self.contexts, []
        for context in contexts:
            context.close()


class Context:
    """A signed-in session of its own: cookies, page storage, request rules and one page."""

    def __init__(self, loop, state=None, *, refuse_workers=False, surface=None):
        self.loop, self.handlers, self.page, self.methods = loop, [], None, {}
        self.origins = [entry for entry in (state or {}).get('origins') or [] if isinstance(entry, dict)]
        self.surface = surface if surface is not None else loop.call(lambda: Surface(loop, refuse_workers))
        self.surface.decide = self.refuses
        # Not Playwright's: what this browser calls itself, for requests made beside it.
        self.agent = self.surface.agent
        self.restore(state or {})

    def restore(self, state):
        now, wanted = time.time(), {}
        for cookie in state.get('cookies') or []:
            expires = cookie.get('expires')
            if expires is None or expires == -1 or expires > now:
                wanted[cookie['name'], cookie['domain'], cookie.get('path') or '/'] = cookie
        if wanted:
            self.loop.call(lambda: [self.surface.set_cookie(cookie) for cookie in wanted.values()])
            # The store takes cookies in its own time. One it refuses must not hold the download up for long:
            # a session that is short of a cookie is caught by the signed-in check that follows.
            deadline = time.monotonic() + 5
            while len(self.surface.cookies) < len(wanted) and time.monotonic() < deadline:
                time.sleep(STEP)
        saved = {}
        for entry in self.origins:
            rows = [[row['name'], row['value']] for row in entry.get('localStorage') or []
                    if isinstance(row, dict) and isinstance(row.get('name'), str) and isinstance(row.get('value'), str)]
            if isinstance(entry.get('origin'), str) and rows:
                saved[entry['origin']] = rows
        if saved:
            self.loop.call(lambda: self.surface.restore_storage(saved))
        self.saved_origins = set(saved)

    def refuses(self, request):
        """Playwright's order: the page's handlers before the context's, the newest first; the first to decide ends it."""
        page = self.page.handlers if self.page is not None else []
        for handler in [*reversed(page), *reversed(self.handlers)]:
            route = Route(request)
            handler(route)
            if route.outcome == 'continue':
                break
            if route.outcome != 'fallback':
                # Refused, or left undecided: Playwright would leave that request waiting for ever.
                return True
        if request.method != 'GET' and len(self.methods) < 500:
            self.methods[request.url] = request.method
        return False

    def route(self, pattern, handler):
        _whole_site(pattern)
        self.loop.call(lambda: self.handlers.append(handler))

    def unroute(self, pattern, handler=None):
        _whole_site(pattern)
        self.loop.call(lambda: _forget(self.handlers, handler))

    def new_page(self):
        if self.page is not None:
            raise Error('The built-in engine has one page for each session.')
        self.page = Page(self)
        return self.page

    def cookies(self, urls=None):
        # A removal still in doubt is decided within the grace; a cookie the site has dropped is not handed on.
        deadline = time.monotonic() + CookieRecords.GRACE * 2
        while not self.loop.call(self.surface.cookies.settled) and time.monotonic() < deadline:
            time.sleep(STEP)
        return self.loop.call(lambda: [dict(record) for record in self.surface.cookies.current()])

    def storage_state(self):
        origins = {entry['origin']: entry for entry in self.origins if isinstance(entry.get('origin'), str)}
        current = self.page.storage() if self.page is not None else None
        if current is not None:
            origin, rows = current
            origins.pop(origin, None)
            if rows:
                origins[origin] = {'origin': origin,
                                   'localStorage': [{'name': name, 'value': value} for name, value in sorted(rows)]}
        return {'cookies': self.cookies(), 'origins': list(origins.values())}

    def arrived(self, url):
        """A page of a restored origin has loaded, so its storage is back."""
        if self.saved_origins and any(url == origin or url.startswith(origin + '/') for origin in self.saved_origins):
            self.saved_origins = set()
            self.loop.call(self.surface.storage_restored)

    def close(self):
        try:
            self.loop.call(self.surface.close)
        except Error:
            # The engine has already gone, and the page with it.
            pass


def _whole_site(pattern):
    if pattern != ORIGIN_ROUTE:
        raise Error('The built-in engine applies a request rule to every request.')


def _forget(handlers, handler):
    if handler is None:
        handlers.clear()
    elif handler in handlers:
        handlers.remove(handler)


class Navigation:
    """Playwright's expect_navigation: the page that an action inside the block leads to."""

    def __init__(self, page, complete, timeout):
        self.page, self.complete, self.timeout, self.value = page, complete, timeout, None

    def __enter__(self):
        self.before, self.failures = self.page.settled(), self.page.surface.failures
        return self

    def __exit__(self, kind, error, trace):
        if kind is None:
            self.value = self.page.arrive(self.before, self.failures, self.timeout, self.complete)
        return False


class Mouse:
    def __init__(self, page):
        self.page, self.at = page, (0, 0)

    def send(self, kind, *where):
        self.page.loop.call(lambda: getattr(self.page.surface, kind)(*where))

    def move(self, x, y):
        self.at = (x, y)
        self.send('pointer', 'move', x, y)

    def down(self):
        self.send('pointer', 'press', *self.at)

    def up(self):
        self.send('pointer', 'release', *self.at)

    def click(self, x, y):
        self.move(x, y)
        # Each on its own turn of Qt's loop, as a hand's press and release arrive.
        self.down()
        self.up()

    def wheel(self, delta_x, delta_y):
        self.send('wheel', *self.at, delta_x, delta_y)


class Page:
    # Qt hands over no response contents, a visited page's included; the adapter reads such a page another way.
    navigation_bodies = False

    def __init__(self, context):
        self.context, self.loop, self.surface = context, context.loop, context.surface
        self.handlers, self.listeners, self.seen = [], [], (None, 0)
        self.mouse = Mouse(self)
        self.blank()

    def blank(self):
        """An empty page to start from, so that there is always a document to ask."""
        ended = self.surface.ended
        self.loop.call(lambda: self.surface.load('about:blank'))
        deadline = time.monotonic() + SCRIPT_SECONDS
        # Not before that load has ended: until then the answer can come from a document it is about to replace,
        # and the page asked for next would be taken to have arrived when the empty one does.
        while self.surface.ended == ended or self.where() is None:
            if time.monotonic() >= deadline:
                raise Error('The built-in engine opened no page.')
            time.sleep(STEP)

    @property
    def url(self):
        """The address of the document that is there, as Playwright gives it.

        Qt's own names the page that was asked for, also when it failed to load or has yet to arrive.
        """
        try:
            return self.settled()['url']
        except Error:
            # No document answers, as after the page's process has ended: nothing is loaded.
            return 'about:blank'

    def script(self, code, seconds=SCRIPT_SECONDS):
        """One script's value, as the text it returns."""
        if self.surface.gone:
            raise Error('The page process has ended.')
        text = self.loop.ask(lambda answer: self.surface.script(code, answer), seconds)
        if not isinstance(text, str):
            raise Error('The page gave no answer.')
        return text

    def where(self):
        """The current document's identity, address, status and load state, or None while there is none to ask."""
        try:
            return json.loads(self.script(WHERE, 2))
        except (Error, ValueError):
            return None

    def settled(self):
        """where(), tried a few times: between two documents there is briefly nothing to ask."""
        for _ in range(5):
            now = self.where()
            if now is not None:
                return now
            time.sleep(STEP)
        raise Error('The page gave no answer.')

    def arrive(self, before, failures, timeout, complete=False):
        """Wait for a document other than 'before' to load; its response, as Playwright's navigation gives it."""
        deadline, failed = time.monotonic() + max(timeout, 1) / 1000, None
        while True:
            now = self.where()
            fresh = now is not None and now['id'] != before['id']
            if fresh and now['complete' if complete else 'ready']:
                break
            if not fresh and self.surface.failures > failures:
                # The load has failed. Allow a moment for an error page to show, then stop waiting.
                failed = failed or time.monotonic()
                if time.monotonic() - failed > 2:
                    raise Error(self.unloaded())
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Timeout {int(timeout)}ms exceeded.')
            time.sleep(STEP)
        self.context.arrived(now['url'])
        self.report()
        web = now['url'].startswith(('http://', 'https://'))
        if now['url'].startswith('chrome-error:') or (web and not now['status']):
            raise Error(self.unloaded())
        return Response(now['url'], now['status']) if web else None

    def unloaded(self):
        # The numbers say which failure it was; they are for the log.
        code = self.loop.call(lambda: self.surface.failure)
        return 'The page could not be loaded' + (f' (error {code}).' if code else '.')

    def goto(self, url, wait_until='load', timeout=DEFAULT_TIMEOUT):
        if wait_until not in {'load', 'domcontentloaded'}:
            raise Error('The built-in engine waits for a page to be ready or to be complete.')
        self.report()
        if self.surface.gone:
            self.blank()
        before, failures = self.settled(), self.surface.failures
        self.loop.call(lambda: self.surface.load(url))
        return self.arrive(before, failures, timeout, wait_until == 'load')

    def expect_navigation(self, wait_until='load', timeout=DEFAULT_TIMEOUT):
        if wait_until not in {'load', 'domcontentloaded'}:
            raise Error('The built-in engine waits for a page to be ready or to be complete.')
        return Navigation(self, wait_until == 'load', timeout)

    def wait_for_timeout(self, milliseconds):
        time.sleep(max(0, milliseconds) / 1000)
        self.report()

    def call(self, source, arguments='', prepare='', timeout=None):
        """Call a script as Playwright's evaluate does and wait for its value, a promise's included."""
        seconds = EVALUATE_SECONDS if timeout is None else max(timeout, 1) / 1000
        started = time.monotonic()
        deadline = started + seconds
        answer = self.answer(self.script(CALL % {'prepare': prepare, 'source': source, 'arguments': arguments}, seconds))
        while 'pending' in answer:
            text = self.script(TAKE % answer['pending'], 5)
            if text == 'gone':
                raise Error('The page changed before its script finished.')
            if text:
                answer = self.answer(text)
            elif time.monotonic() >= deadline:
                raise TimeoutError('The page script did not finish in time.')
            else:
                time.sleep(STEP)
        if 'error' in answer:
            name = answer['error'] if re.fullmatch(r'[A-Za-z]{1,40}', str(answer['error'])) else 'Error'
            # Whether a rule of this app refused a request while the script ran: the page itself only says 'TypeError'.
            seen = dict.fromkeys(text for when, text in self.loop.call(lambda: list(self.surface.notes)) if when >= started)
            raise Error(f"The page script failed ({'; '.join([name, *seen])}).")
        return answer.get('value')

    @staticmethod
    def answer(text):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise Error('The page gave no answer.')
        return value

    def evaluate(self, expression, arg=None, timeout=None):
        return self.call(expression, json.dumps(arg), timeout=timeout)

    def locator(self, selector):
        return Locator(self, []).locator(selector)

    def route(self, pattern, handler):
        _whole_site(pattern)
        self.loop.call(lambda: self.handlers.append(handler))

    def unroute(self, pattern, handler=None):
        _whole_site(pattern)
        self.loop.call(lambda: _forget(self.handlers, handler))

    def unroute_all(self, behavior=None):
        # Handlers run on Qt's thread, one at a time, so none is still at work when this returns.
        self.loop.call(self.handlers.clear)

    def on(self, event, handler):
        if event != 'response':
            raise Error('The built-in engine reports responses only.')
        if not self.listeners:
            # Only what the page requests from now on is reported, as with Playwright.
            self.look()
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners:
            self.listeners.remove(handler)

    def look(self):
        """Requests of the current document not yet looked at, as responses: address, kind and status."""
        try:
            seen = json.loads(self.script(REQUESTS % (json.dumps(self.seen[0]), self.seen[1]), 2))
        except (Error, ValueError):
            return []
        fresh = seen['id'] != self.seen[0]
        self.seen = (seen['id'], len(seen['rows']) + (0 if fresh else self.seen[1]))
        found = []
        if fresh and seen['status'] and seen['url'].startswith(('http://', 'https://')):
            found.append(Response(seen['url'], seen['status']))
        for url, initiator, status in seen['rows']:
            # Status 0 is a request that was refused or whose site does not share its timing.
            if status:
                found.append(Response(url, status, INITIATORS.get(initiator, 'other'), self.context.methods.get(url, 'GET')))
        return found

    def report(self):
        if self.listeners:
            for response in self.look():
                for listener in list(self.listeners):
                    listener(response)

    def storage(self):
        """(origin, [[name, value], ...]) of the current page's storage, or None where it cannot be read."""
        try:
            value = json.loads(self.script(STORAGE, 5))
        except (Error, ValueError):
            return None
        return (value[0], value[1]) if value else None

    def close(self):
        self.context.close()


class Locator:
    """Elements named by CSS from the document down, read afresh on every use as Playwright's are."""

    def __init__(self, page, steps):
        self.page, self.steps = page, steps

    def locator(self, selector):
        if selector == '..':
            return Locator(self.page, [*self.steps, ['parent', None]])
        if not isinstance(selector, str) or UNSUPPORTED_SELECTOR.search(selector):
            raise Error('The built-in engine finds elements by CSS only.')
        return Locator(self.page, [*self.steps, ['css', selector]])

    def nth(self, index):
        return Locator(self.page, [*self.steps, ['nth', index]])

    @property
    def first(self):
        return self.nth(0)

    @property
    def last(self):
        return self.nth(-1)

    def all(self, source, arg=None, timeout=None):
        return self.page.call(source, f'find({json.dumps(self.steps)}), {json.dumps(arg)}', FIND, timeout)

    def count(self):
        return self.all('nodes => nodes.length')

    def evaluate_all(self, expression, arg=None):
        return self.all(expression, arg)

    def one(self, action, timeout=None):
        """The action's value for the single element named, waiting for the element as Playwright does."""
        limit = DEFAULT_TIMEOUT if timeout is None else max(timeout, 1)
        deadline = time.monotonic() + limit / 1000
        while True:
            answer = self.all(ONE % action)
            if answer['found']:
                return answer.get('value')
            if answer['count'] > 1:
                raise Error(f"strict mode violation: {answer['count']} elements")
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Timeout {int(limit)}ms exceeded.')
            time.sleep(STEP * 3)

    def is_visible(self):
        answer = self.all(ONE % VISIBLE)
        if not answer['found'] and answer['count'] > 1:
            raise Error(f"strict mode violation: {answer['count']} elements")
        return answer['found'] and answer.get('value') is True

    def inner_text(self, timeout=None):
        return self.one('e => e.innerText', timeout)

    def get_attribute(self, name, timeout=None):
        return self.one(f'e => e.getAttribute({json.dumps(name)})', timeout)

    def evaluate(self, expression, arg=None, timeout=None):
        return self.one(f'e => ({expression})(e, {json.dumps(arg)})', timeout)

    def bounding_box(self, timeout=None):
        return self.one(BOX, timeout)

    def scroll_into_view_if_needed(self, timeout=None):
        self.one(SCROLL, timeout)

    def click(self, timeout=None):
        """Press the element with the pointer once it is in view, still and not covered."""
        limit = DEFAULT_TIMEOUT if timeout is None else max(timeout, 1)
        deadline, last = time.monotonic() + limit / 1000, None
        # Before aiming: only the pointer's own journey lies between the last look and the press.
        self.page.report()
        while True:
            left = max(1, (deadline - time.monotonic()) * 1000)
            aim = self.one(AIM, left)
            # In the same place twice running: it has stopped moving.
            if aim['ready'] and aim == last:
                self.page.mouse.move(aim['x'], aim['y'])
                # The pointer's arrival can change a page as well. One more look, and only this element may take the press.
                if self.one(GUARD % (AIM, json.dumps(aim)), left):
                    self.page.mouse.down()
                    self.page.mouse.up()
                    if self.landed():
                        return
                aim = None
            last = aim
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Timeout {int(limit)}ms exceeded.')
            time.sleep(STEP * 2)

    def landed(self):
        """Whether the press reached the element. False only when the page stopped it for landing elsewhere."""
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                hit = self.page.call('box.hit', timeout=2000)
            except Error:
                # The page is on its way to another, which is what a press is for.
                return True
            if hit != 'armed':
                return hit is not False
            time.sleep(STEP)
        return True
