"""The desktop DOM reader on a native, profile-bound Android browser.

Only the DOM facade from qt_engine is reused: that module imports no Qt package
until its desktop serve/Surface path is called, neither of which is used here.
The native object marshals WebView operations to Main. Python owns the worker,
request rules and cancellation; JavaScript can never call those Python objects.
No browser storage state is reconstructed or written over android.json/state.json.
"""
import collections
import hashlib
import json
import math
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import urlsplit

from . import android_session, qt_engine as dom, sites
from .adapters.tapas import BrowserReadTransport, TapasAdapter
from .adapters.tapas_http import TapasHTTPAdapter
from .android_transport import WebViewCookieSource
from .errors import AuthenticationError
from .fast_import import FAST_INTERVAL, FAST_METADATA_INTERVAL, FAST_WORKERS, FastWorkerSession
from .models import ArchiveError
from .pipeline import MetadataPipeline
from .transport import Transport

_contexts = weakref.WeakSet()
_contexts_lock = threading.RLock()

# Registration removal does not detach an already controlled document. The
# caller discards that entire WebView before trusting any provider content.
_SERVICE_WORKERS = """async clear => {
  if (location.origin !== 'https://tapas.io') throw new Error('Origin');
  if (!('serviceWorker' in navigator)) return {registered:0, controlled:false, cleared:true};
  const workers = await navigator.serviceWorker.getRegistrations();
  if (workers.length > 128 || workers.some(worker => new URL(worker.scope).origin !== location.origin))
    throw new Error('Workers');
  const controlled = !!navigator.serviceWorker.controller;
  const removed = clear ? await Promise.all(workers.map(worker => worker.unregister())) : [];
  return {registered:workers.length, controlled, cleared:removed.every(value => value === true)};
}"""
_CONTROLLED = 'comic-archive:controlled-reading-document'
_WORKER_GUARD_HEAD = """(() => {
  if (navigator.serviceWorker && navigator.serviceWorker.controller)
    return 'comic-archive:controlled-reading-document';
  const answer = ("""
_WORKER_GUARD_TAIL = """);
  if (navigator.serviceWorker && navigator.serviceWorker.controller)
    return 'comic-archive:controlled-reading-document';
  return answer;
})()"""


def interrupt_contexts(factory):
    """Publish typed cancellation before native wakes any blocked script waiter."""
    with _contexts_lock:
        contexts = [context for context in _contexts if context.factory is factory or context.factory == factory]
    for context in contexts:
        context.cancel()
    factory.interrupt()


def close_contexts(factory):
    with _contexts_lock:
        contexts = [context for context in _contexts if context.factory is factory or context.factory == factory]
    failed = False
    for context in contexts:
        try:
            context.close()
        except Exception:
            failed = True
    if factory.closeAll() is not True or failed:
        raise ArchiveError('The reading browser is still closing. Try again after saving stops.')


class _Loop:
    """Native methods dispatch themselves; no desktop UI loop is entered."""
    @staticmethod
    def call(function, seconds=30):
        return function()


class _Surface:
    def __init__(self, context, native):
        self.context, self.native = context, native
        self.notes = collections.deque(maxlen=8)

    def snapshot(self):
        try:
            value = json.loads(self.native.state())
            fields = {'v', 'url', 'ended', 'failures', 'failure', 'closed', 'gone', 'agent'}
            valid = (isinstance(value, dict) and set(value) == fields
                     and type(value['v']) is int and value['v'] == 1
                     and all(type(value[key]) is bool for key in ('closed', 'gone'))
                     and all(type(value[key]) is int and 0 <= value[key] < 2**53 for key in ('ended', 'failures'))
                     and (value['failure'] is None or type(value['failure']) is int)
                     and isinstance(value['url'], str) and len(value['url']) <= 16384
                     and isinstance(value['agent'], str) and 0 < len(value['agent']) <= 2048
                     and value['agent'].isascii() and value['agent'].isprintable())
            if not valid:
                raise ValueError
            return value
        except Exception:
            raise dom.Error('The native reading browser gave no usable state.') from None

    @property
    def ended(self):
        return self.snapshot()['ended']

    @property
    def failures(self):
        return self.snapshot()['failures']

    @property
    def failure(self):
        return self.snapshot()['failure']

    @property
    def gone(self):
        value = self.snapshot()
        return value['gone'] or value['closed']

    def invoke(self, operation, *arguments):
        self.context.check()
        try:
            result = getattr(self.native, operation)(*arguments)
        except Exception:
            self.context.check()
            raise dom.Error('The native reading browser could not finish this operation.') from None
        self.context.check()
        return result

    def load(self, url):
        self.invoke('load', url)

    def pointer(self, kind, x, y):
        if kind not in {'move', 'press', 'release'} or not _coordinates(x, y):
            raise dom.Error('The reading pointer position is unusable.')
        self.invoke('pointer', kind, float(x), float(y))

    def wheel(self, x, y, delta_x, delta_y):
        if not _coordinates(x, y) or not _coordinates(delta_x, delta_y, signed=True):
            raise dom.Error('The reading scroll position is unusable.')
        self.invoke('wheel', float(x), float(y), float(delta_x), float(delta_y))


def _coordinates(*values, signed=False):
    return all(type(value) in (int, float) and math.isfinite(value)
               and (-100000 if signed else 0) <= value <= 100000 for value in values)


class AndroidBrowserPage(dom.Page):
    """The existing CSS/promise/pager facade with native transport, not a Qt window."""
    def blank(self):
        # Native construction starts blank. About:blank intentionally has no
        # injected message authority, so it is observed without running a script.
        value = self.surface.snapshot()
        if value['closed'] or value['gone']:
            raise dom.Error('The native reading browser has closed.')

    def where(self):
        self.context.check()
        value = self.surface.snapshot()
        if value['closed'] or value['gone']:
            return None
        if value['url'] == 'about:blank':
            return {'id': f"blank:{value['ended']}", 'url': 'about:blank',
                    'complete': True, 'ready': True, 'status': 0}
        return super().where()

    def script(self, code, seconds=dom.SCRIPT_SECONDS):
        self.context.check()
        bound = max(1, min(45000, int(seconds * 1000)))
        if self.context.workers_ready:
            code = _WORKER_GUARD_HEAD + code + _WORKER_GUARD_TAIL
        result = self.surface.invoke('execute', code, bound)
        try:
            value = json.loads(result)
        except (TypeError, ValueError):
            value = None
        if not isinstance(value, str):
            raise dom.Error('The reading page gave no text answer.')
        if value == _CONTROLLED:
            self.context.close()
            raise ArchiveError('The reading page is controlled by a cached worker. Pause and try Go again.')
        return value

    def expect_navigation(self, wait_until='load', timeout=dom.DEFAULT_TIMEOUT):
        expected = super().expect_navigation(wait_until, timeout)
        page = self

        class Expected:
            def __enter__(self):
                value = expected.__enter__()
                page.surface.invoke('prepareNavigation')
                return value

            def __exit__(self, *args):
                return expected.__exit__(*args)

        return Expected()

    def wait_for_timeout(self, milliseconds):
        # DOM settling delays remain cancellable while preserving the desktop's
        # actual delay, rather than making up a faster discovery policy.
        deadline = time.monotonic() + max(0, milliseconds) / 1000
        while True:
            self.context.check()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.1, left))
        self.report()

    def route(self, pattern, handler):
        self.context.change_rules(self.handlers, pattern, handler)

    def unroute(self, pattern, handler=None):
        self.context.change_rules(self.handlers, pattern, handler, remove=True)

    def unroute_all(self, behavior=None):
        with self.context.rules_lock:
            self.handlers.clear()


class AndroidBrowserContext:
    """One native page and its worker-owned read rules. Native callbacks use allowRequest."""
    def __init__(self, factory, check):
        self.factory, self.check_callback = factory, check
        self.handlers, self.page, self.methods = [], None, {}
        self.rules_lock = threading.RLock()
        self.closed = False
        self.cancelled = False
        self.workers_ready = False
        self.problem = None
        self.loop = _Loop()
        self.surface = None
        native = None
        try:
            native = factory.open(self)
            self.surface = _Surface(self, native)
            self.agent = self.surface.snapshot()['agent']
            with _contexts_lock:
                _contexts.add(self)
        except Exception:
            if native is not None:
                try:
                    native.close()
                except Exception:
                    pass
            self.closed = True
            self.check_callback()  # Keep a concurrent stop/network hold typed.
            raise ArchiveError('The browser reader could not open. Update Android System WebView and try again.') from None

    def check(self):
        with self.rules_lock:
            problem, self.problem = self.problem, None
            closed = self.closed
            cancelled = self.cancelled
        if problem is not None:
            raise problem
        if cancelled:
            from .cancellation import RunCancelled
            raise RunCancelled('This browser read was stopped; completed files are retained.')
        if closed:
            raise dom.Error('The native reading browser has closed.')
        self.check_callback()

    def allowRequest(self, url, method, resource_type):
        """Called off Main by native interception; no URL/body is returned or logged."""
        try:
            self.check()
            if not isinstance(url, str) or len(url) > 16384 or not isinstance(method, str):
                return False
            parsed = urlsplit(url)
            if (method not in {'GET', 'HEAD', 'OPTIONS'} or parsed.scheme != 'https'
                    or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}):
                return False
            with self.rules_lock:
                page_rules = list(self.page.handlers) if self.page is not None else []
                handlers = [*reversed(page_rules), *reversed(self.handlers)]
            kind = resource_type if resource_type in {'document', 'xhr', 'fetch', 'image', 'script',
                'stylesheet', 'font', 'media', 'other'} else 'other'
            request = dom.Request(url, method, kind)
            for handler in handlers:
                route = dom.Route(request)
                handler(route)
                if route.outcome == 'continue':
                    break
                if route.outcome != 'fallback':
                    return False
            with self.rules_lock:
                if self.closed:
                    return False
                if method != 'GET' and len(self.methods) < 500:
                    self.methods[url] = method
            return True
        except ArchiveError as error:
            with self.rules_lock:
                if self.problem is None:
                    self.problem = error
            return False
        except Exception:
            if self.surface is not None:
                self.surface.notes.append((time.monotonic(), 'a reading request rule failed'))
            return False

    def change_rules(self, handlers, pattern, handler, *, remove=False):
        if pattern != '**/*' or (not remove and not callable(handler)):
            raise dom.Error('The native reader requires one whole-site request rule.')
        with self.rules_lock:
            if remove:
                dom._forget(handlers, handler)
            else:
                handlers.append(handler)

    def route(self, pattern, handler):
        self.change_rules(self.handlers, pattern, handler)

    def unroute(self, pattern, handler=None):
        self.change_rules(self.handlers, pattern, handler, remove=True)

    def new_page(self):
        if self.page is not None:
            raise dom.Error('The native reading session has one page.')
        self.page = AndroidBrowserPage(self)
        return self.page

    def arrived(self, url):
        pass  # The native profile already owns its cookies and origin storage.

    def cookies(self, urls=None):
        return []  # BrowserReadTransport construction must never snapshot a jar.

    def storage_state(self):
        raise ArchiveError('Android sign-in stays in the browser; no copied session may be saved.')

    def cancel(self):
        with self.rules_lock:
            self.cancelled = True

    def close(self):
        with self.rules_lock:
            if self.closed:
                return
            self.closed = True
            self.handlers.clear()
            self.problem = None
        with _contexts_lock:
            _contexts.discard(self)
        if self.surface is not None:
            try:
                self.surface.native.close()
            except Exception:
                raise dom.Error('The native reading browser could not close cleanly.') from None


def _asset_client(fast):
    """The socket boundary is replaceable by disposable-emulator fixtures only."""
    import httpx
    if fast:
        return httpx.Client(timeout=httpx.Timeout(10, connect=30), follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=FAST_WORKERS, max_keepalive_connections=FAST_WORKERS))
    return httpx.Client(timeout=30, follow_redirects=False, trust_env=False)


class AndroidBrowserTransport(BrowserReadTransport):
    """Browser metadata plus the canonical asset client with live native cookies."""
    def __init__(self, context, *, fast=False, interval=1.0, metadata_interval=1.0,
                 network_guard=None, client=None):
        profile = 'fast' if fast else 'normal'
        super().__init__(context.page, context, interval=interval, metadata_interval=metadata_interval,
                         network_guard=network_guard, download_profile=profile)
        self.assets.close()
        try:
            store = WebViewCookieSource(context.factory.cookieStore(), site=sites.DEFAULT)
            if client is None:
                client = _asset_client(fast)
            self.assets = Transport(cookies=(), interval=interval, profile=profile, client=client,
                network_guard=network_guard, agent=context.agent, site=sites.DEFAULT,
                cookie_source=store.header_for, cookie_sink=store.apply)
        except Exception:
            context.page.unroute_all(behavior='wait')
            if client is not None:
                client.close()
            raise ArchiveError('The browser asset reader could not open; saved files are retained.') from None


class AndroidTapasBrowserAdapter(TapasAdapter):
    """Reuse desktop discovery/access/Fast semantics without desktop session startup."""
    def __init__(self, session, factory, *, fast=False, interval=1.0, network_guard=None):
        if not isinstance(session, Path) or type(fast) is not bool:
            raise ArchiveError('The browser reader needs its selected Android session.')
        super().__init__(session, preferred_browser='android', headless=True, interval=interval)
        self.factory, self.fast = factory, fast
        if network_guard is not None:
            self.network_guard = network_guard
        self.context = self.page = None

    def __enter__(self):
        if android_session.signed_in_site(self.session) != sites.DEFAULT:
            raise AuthenticationError('Sign in to Tapas before starting its browser reader.')
        try:
            available = self.factory.available() is True
        except Exception:
            available = False
        if not available:
            raise ArchiveError('Update Android System WebView to use browser discovery and Fast mode.')
        if self.context is not None:
            raise ArchiveError('The browser reader is already open.')
        self._open_context()
        try:
            self._prepare_document()
        except Exception:
            self.close()
            raise
        return self

    def _open_context(self):
        context = AndroidBrowserContext(self.factory, self.remaining_timeout)
        try:
            self.page = context.new_page()
            context.route('**/*', self.network_read_only_request)
        except Exception:
            context.close()
            raise
        self.context = context

    def _worker_status(self, clear):
        value = self.page.evaluate(_SERVICE_WORKERS, clear, timeout=self.remaining_timeout())
        if (not isinstance(value, dict) or set(value) != {'registered', 'controlled', 'cleared'}
                or type(value['registered']) is not int or not 0 <= value['registered'] <= 128
                or type(value['controlled']) is not bool or type(value['cleared']) is not bool
                or not value['cleared']):
            raise ArchiveError('The browser could not clear its cached reading worker. Pause and try Go again.')
        return value

    def _prepare_document(self):
        self.page.goto(sites.DEFAULT.home, wait_until='domcontentloaded', timeout=self.remaining_timeout())
        status = self._worker_status(True)
        if status['registered'] or status['controlled']:
            # Keep the native sign-in profile, but never keep its controlled
            # document, even when unregister() has already resolved successfully.
            self.context.close()
            self._open_context()
            self.page.goto(sites.DEFAULT.home, wait_until='domcontentloaded', timeout=self.remaining_timeout())
            fresh = self._worker_status(False)
            if fresh['registered'] or fresh['controlled']:
                raise ArchiveError('A cached reading worker is still active. Pause and try Go again.')
        self.context.workers_ready = True

    def inspect_library(self, *args, **kwargs):
        if self.context is None:
            self.__enter__()
        return super().inspect_library(*args, **kwargs)

    def automatic_adapter(self):
        if self.context is None:
            self.__enter__()  # Worker startup remains held and performs no browser/network work.
        if self.context is None or self.context.closed:
            raise ArchiveError('Open the Android reading browser before saving.')
        self.context.check()
        interval = FAST_INTERVAL if self.fast else self.interval
        metadata = FAST_METADATA_INTERVAL if self.fast else getattr(self, 'metadata_interval', 1.0)
        if self.transport is None:
            self.transport = AndroidBrowserTransport(self.context, fast=self.fast, interval=interval,
                metadata_interval=metadata, network_guard=self.network_guard)
        self.transport.deadline = getattr(self, 'deadline', None)
        self.transport.cancel_check = getattr(self, 'cancel_check', None)
        self.transport.assets.interval = interval
        self.transport.metadata_interval = metadata
        self.transport.assets.deadline = self.transport.deadline
        self.transport.assets.cancel_check = self.transport.cancel_check
        if getattr(self, 'asset_metrics', None) is not None:
            self.transport.assets.metrics = self.asset_metrics
        self._automatic = TapasHTTPAdapter(transport=self.transport,
            expected_account_id=getattr(self, 'expected_account_id', None))
        return self._automatic

    def begin_task(self):
        # Called only at new queue admission (or a new Fast batch), never as a
        # retry inside the stopped task. A stopped document cannot resume itself.
        context = self.context
        if context is not None and (context.closed or context.cancelled or context.surface.gone):
            self.close()
        self.deadline = self.cancel_check = self.expected_account_id = self.asset_metrics = None

    def capture_automatic(self, url, series_id=None, series_title=None, title=None, order=None):
        self._manifest = self.automatic_adapter().capture(url, series_id, series_title, title, order)
        self.series = self._manifest.series
        self.capture_mode = 'authenticated_html_manifest'
        return self._manifest

    def capture(self, *args, **kwargs):
        raise ArchiveError('Android saving uses the verified automatic reader, without a terminal prompt.')

    def __exit__(self, *args):
        context, self.context = self.context, None
        transport, self.transport = self.transport, None
        try:
            if transport is not None:
                transport.close()
        finally:
            if context is not None:
                context.close()

    def close(self):
        self.__exit__(None, None, None)


class AndroidFastImportAdapter(MetadataPipeline):
    """The same one-episode lookahead and fixed Fast policy on the native engine."""
    download_profile = 'fast'

    def __init__(self, session, factory, network_guard=None):
        self.session = session
        super().__init__(AndroidTapasBrowserAdapter, None, session, factory,
            fast=True, interval=FAST_INTERVAL, network_guard=network_guard)

    def begin_batch(self):
        super().begin_batch()
        self._pool.submit(self._inner.begin_task).result()


class AndroidFastWorkerSession(FastWorkerSession):
    """Reuse batch drain/limits/failure handling; platform cookies remain live per read."""
    def __init__(self, session, factory, network_guard=None):
        super().__init__(session, preferred_browser='android', headless=True)
        self.factory, self.network_guard = factory, network_guard

    def _open_adapter(self):
        return AndroidFastImportAdapter(self.session, self.factory, self.network_guard)

    def _state_signature(self):
        android_session._guard(self.session)
        if android_session.signed_in_site(self.session) != sites.DEFAULT:
            raise AuthenticationError('Restore the Tapas sign-in before continuing Fast mode.')
        # Android's own site/marker replacement records committed re-sign-in.
        # The desktop compatibility state need not exist and is never read here.
        identities = []
        for name in ('site', android_session.MARKER):
            path = self.session / name
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                    raise ValueError
                with path.open('rb') as stream:
                    data = stream.read(4097)
                if len(data) > 4096:
                    raise ValueError
                stat = path.stat()
                identities.append((stat.st_mtime_ns, stat.st_size, stat.st_ino, hashlib.sha256(data).digest()))
            except (OSError, ValueError):
                raise AuthenticationError('The Android sign-in record changed; restore sign-in before Fast mode.') from None
        return tuple(identities)
