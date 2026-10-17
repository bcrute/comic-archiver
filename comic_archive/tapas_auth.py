"""Local browser sign-in with automatic completion, without terminal prompts."""
import threading
import time

from . import background
from .errors import AuthenticationError
from .inventory import Inventory
from .models import ArchiveError
from .network import NetworkPolicyHold, NetworkGuard
from .session import browser_api, launch_browser, save_session


def identity(value):
    if not isinstance(value, dict) or value.get('error_details') is not None:
        return None
    data = value.get('data')
    user = data.get('user') if isinstance(data, dict) else None
    key = user.get('id') if isinstance(user, dict) else None
    return str(key) if type(key) is int and key > 0 else None


def complete(workspace, key, browser, save, cancelled, interval):
    """Replace the downloader's session with a verified sign-in; False when cancelled before saving."""
    with Inventory(workspace.library, read_only=True) as inventory:
        inventory.account_binding_state(key)
    result = background.stop(workspace.session, wait_seconds=35)
    if result.get('running') is not False:
        raise ArchiveError('The previous worker is still stopping.')
    if cancelled():
        return False
    save()
    result = background.start(workspace.session, browser, interval)
    if result.get('startup_pending') and result.get('running') is False:
        # Started, but slow to come up, as the first start after installing can be while Windows checks
        # its files. The sign-in is saved and kept; the downloader goes on starting by itself.
        return True
    if result.get('running') is not True or result.get('start_deferred'):
        raise ArchiveError('Downloader startup is pending.')
    return True


class SignIn:
    def __init__(self, workspace, browser='auto'):
        self.workspace, self.browser = workspace, browser
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = {'status':'idle'}

    def status(self):
        with self._lock:
            return dict(self._state)

    def _update(self, **value):
        with self._lock:
            self._state = value

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return dict(self._state)
            self._cancel = threading.Event()
            self._state = {'status':'running'}
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return dict(self._state)

    def close(self):
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        try:
            self._login()
        except NetworkPolicyHold:
            self._update(status='error', code='network_held')
        except AuthenticationError:
            self._update(status='error', code='account_mismatch')
        except Exception:
            # Browser errors may contain request/session data. Never publish them.
            self._update(status='error', code='signin_failed')

    def _login(self):
        workspace = self.workspace
        guard = NetworkGuard(workspace.library)
        guard.check()
        with browser_api()() as driver:
            browser = launch_browser(driver, self.browser)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto('https://tapas.io/account/signin', timeout=30000)
                deadline = time.monotonic() + 900
                while not self._cancel.is_set() and time.monotonic() < deadline and browser.is_connected():
                    guard.check()
                    try:
                        response = context.request.get('https://tapas.io/who-am-i', timeout=10000)
                        key = identity(response.json()) if response.ok else None
                    except Exception:
                        key = None
                    if self._cancel.is_set():
                        break
                    if key:
                        if not complete(workspace, key, self.browser,
                                        lambda: save_session(context, workspace.session), self._cancel.is_set, 0.25):
                            break
                        self._update(status='connected')
                        return
                    page.wait_for_timeout(3000)
                self._update(status='cancelled' if self._cancel.is_set() or not browser.is_connected() else 'error',
                             code='signin_incomplete')
            finally:
                browser.close()
