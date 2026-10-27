"""A site's sign-in carried out by the app's own window, without Qt.

The dashboard asks for a sign-in over its local API. The window shows the
site's page, hands over what the page's browser engine holds, and this module
saves it for the downloader exactly as the browser-window sign-in does.
"""
import threading
import time

from . import background, sites, tapas_auth
from .account_ui import DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL
from .app_runtime import LOG, describe
from .errors import AuthenticationError, LibraryBusy
from .models import ArchiveError
from .network import NetworkGuard, NetworkPolicyHold
from .session import cookie_expiry_seconds, forget_session, load_session, store_session
from .transport import Transport

SIGNIN_SECONDS = 900
CODES = {'network_held', 'account_mismatch', 'signin_failed', 'signin_incomplete'}
# Qt names an unset attribute Default and spells None with an underscore; browsers treat unset as Lax.
SAME_SITE = {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None', 'None_': 'None'}
UNSUPPORTED = 'The sign-in cookies have an unsupported shape; credential details are suppressed.'


def storage_state(cookies, local_storage=(), site=sites.DEFAULT):
    """The saved-session shape the downloader reads, from plain cookie records and storage pairs.

    Only the site's own cookies are kept, as in session.store_session. A record's
    'expires' is Unix seconds, or None for a cookie that ends with the session.
    """
    kept = {}
    for cookie in cookies:
        try:
            name, value, domain, path = (cookie[key] for key in ('name', 'value', 'domain', 'path'))
            expires = cookie['expires']
        except (KeyError, TypeError):
            raise ArchiveError(UNSUPPORTED) from None
        if not all(isinstance(text, str) for text in (name, value, domain, path)):
            raise ArchiveError(UNSUPPORTED)
        if not site.keeps_cookie(domain):
            continue
        kept[name, domain, path] = {
            'name': name, 'value': value, 'domain': domain, 'path': path or '/',
            'expires': -1 if expires is None else cookie_expiry_seconds(expires),
            'httpOnly': cookie.get('httpOnly') is True, 'secure': cookie.get('secure') is True,
            'sameSite': SAME_SITE.get(cookie.get('sameSite'), 'Lax')}
    if not kept:
        raise ArchiveError(f'The sign-in left no {site.name} cookies; nothing was saved.')
    entries = {}
    for pair in local_storage:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(text, str) for text in pair):
            entries[pair[0]] = pair[1]
    origins = [{'origin': site.home, 'localStorage': [{'name': name, 'value': entries[name]} for name in sorted(entries)]}]
    return {'cookies': [kept[key] for key in sorted(kept)], 'origins': origins if entries else []}


def saved_signin(workspace, site, agent, client=None):
    """What the site says of the saved sign-in: 'verified', 'expired', or None when it could not be asked.

    Only the site's own answer about who is signed in counts. No network, the network policy, a refusal or an
    unexpected answer leave it unknown: Go checks again.
    """
    try:
        state = load_session(workspace.session)
        transport = Transport(state['cookies'], client=client, site=site, agent=agent,
                              network_guard=NetworkGuard(workspace.library))
    except (ArchiveError, OSError):
        return None
    try:
        if site == sites.DEFAULT:
            from .adapters.tapas_http import TapasHTTPAdapter
            reader = TapasHTTPAdapter(transport=transport)
        elif site.key == 'webtoon':
            from .adapters.webtoon import WebtoonAdapter
            reader = WebtoonAdapter(transport=transport)
        else:
            from .adapters.tappytoon import TappytoonAdapter
            reader = TappytoonAdapter(transport=transport)
        reader.account_identity()
        return 'verified'
    except AuthenticationError:
        return 'expired'
    except Exception as error:
        LOG.info('the saved sign-in could not be checked: %s', describe(error))
        return None
    finally:
        transport.close()


class WindowSignIn:
    """The dashboard's sign-in control, with the same states and codes as tapas_auth.SignIn.

    start, status and close are called from dashboard request threads; take,
    cancel and fail from the window; admit and finish block and belong on a
    worker thread. 'launch' and 'closing' are the runtime's own, so a sign-in
    cannot race its downloader start or outlive the app.
    """

    def __init__(self, workspace, launch=None, closing=None, browser=DESKTOP_BROWSER, site=sites.DEFAULT):
        self.workspace, self.browser, self.site = workspace, browser, site
        self._launch = threading.Lock() if launch is None else launch
        self._closing = threading.Event() if closing is None else closing
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._state = {'status': 'idle'}
        self._pending = self._finishing = False
        self._deadline = 0

    def _waiting(self):
        """Lock held. True while the person may still be signing in; applies the time limit."""
        if self._state['status'] != 'running' or self._finishing:
            return False
        if time.monotonic() >= self._deadline:
            self._end(status='error', code='signin_incomplete')
            return False
        return True

    def _end(self, **state):
        self._cancel.set()
        self._pending = False
        self._state = state

    def status(self):
        with self._lock:
            self._waiting()
            return dict(self._state)

    def check(self, agent, client=None):
        """Blocking: ask the site whether the saved sign-in still works, so the window can say so before Go.

        'agent' is the name the app's browser gives the site, which refuses requests without one. Whatever
        happens to the sign-in meanwhile (a new one, signing out) stands over this answer.
        """
        with self._lock:
            before = self._state
            if before['status'] != 'idle' or not (self.workspace.session / 'state.json').is_file():
                return dict(before)
        answer = saved_signin(self.workspace, self.site, agent, client)
        LOG.info('the saved sign-in was checked: %s', answer or 'no answer')
        with self._lock:
            if answer is not None and self._state is before:
                self._state = {'status': answer}
            return dict(self._state)

    def start(self):
        """Ask the window for a sign-in. A request that is still open is kept as it is."""
        with self._lock:
            if not self._waiting() and not self._finishing:
                self._cancel = threading.Event()
                self._state = {'status': 'running'}
                self._pending = True
                self._deadline = time.monotonic() + SIGNIN_SECONDS
            return dict(self._state)

    def sign_out(self):
        """Blocking: stop the downloader, then forget the saved sign-in. Refused while a sign-in is open or saving."""
        with self._lock:
            if self._waiting() or self._finishing:
                raise LibraryBusy('Finish signing in before signing out.')
        # The runtime's start-up launch and a finishing sign-in hold the same lock: neither starts in between.
        with self._launch:
            if background.stop(self.workspace.session, wait_seconds=35).get('running') is not False:
                raise ArchiveError('The downloader is still stopping.')
            forget_session(self.workspace.session)
        with self._lock:
            self._state = {'status': 'idle'}
        LOG.info('signed out')
        return {'status': 'signed_out'}

    def take(self):
        """True once for each new request: the window should now show the sign-in page."""
        with self._lock:
            pending = self._waiting() and self._pending
            self._pending = False
            return pending

    def cancel(self):
        """The person went back, or the app is closing."""
        with self._lock:
            if self._waiting():
                self._end(status='cancelled', code='signin_incomplete')
            # A final step that is already saving decides its own outcome.
            self._cancel.set()

    def close(self):
        self.cancel()

    def fail(self, code):
        if code not in CODES:
            raise ArchiveError('Unsupported sign-in result.')
        with self._lock:
            if self._waiting():
                self._end(status='error', code=code)

    def admit(self):
        """Blocking. Hold the sign-in before the page opens when the network policy forbids it."""
        try:
            NetworkGuard(self.workspace.library).check()
        except NetworkPolicyHold:
            self.fail('network_held')
        except Exception as error:
            LOG.warning('sign-in could not begin: %s', describe(error))
            self.fail('signin_failed')

    def finish(self, key, cookies, local_storage=()):
        """Blocking, for a worker thread: save the window's sign-in and restart the downloader with it."""
        with self._lock:
            if not self._waiting():
                return dict(self._state)
            self._finishing, cancel = True, self._cancel
        try:
            state = self._save(key, cookies, local_storage, cancel)
        except NetworkPolicyHold:
            state = {'status': 'error', 'code': 'network_held'}
        except AuthenticationError:
            state = {'status': 'error', 'code': 'account_mismatch'}
        except Exception as error:
            # Class and code location only: the text may hold cookies, paths or request data.
            LOG.warning('sign-in could not be finished: %s', describe(error))
            state = {'status': 'error', 'code': 'signin_failed'}
        LOG.info('sign-in ended: %s', state.get('code', state['status']))
        with self._lock:
            self._finishing = False
            self._state = state
            return dict(state)

    def _save(self, key, cookies, local_storage, cancel):
        workspace = self.workspace
        cancelled = {'status': 'cancelled', 'code': 'signin_incomplete'}
        NetworkGuard(workspace.library).check()
        state = storage_state(cookies, local_storage, self.site)

        def stopped():
            return cancel.is_set() or self._closing.is_set()
        # The runtime's start-up launch and its stop() hold the same lock.
        with self._launch:
            if stopped() or not tapas_auth.complete(workspace, key, self.browser,
                                                    lambda: store_session(state, workspace.session, self.site), stopped,
                                                    DESKTOP_REQUEST_INTERVAL):
                return cancelled
            if self._closing.is_set():
                # The window closed while the downloader was starting, after stop() gave up waiting for us.
                try:
                    background.stop(workspace.session, wait_seconds=0)
                except (ArchiveError, OSError) as error:
                    LOG.warning('downloader did not accept the stop request: %s', describe(error))
                return cancelled
        return {'status': 'connected'}
