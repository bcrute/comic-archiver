"""The Android HTTP seam: cookies asked live from the app's WebView store, never stored here.

The desktop Transport carries a cookie jar it read from the saved session. On Android the
app's WebView cookie store stays the only authority: this transport asks it, for each request
and each redirect hop, for exactly the Cookie header a browser would send to that full URL,
and hands raw Set-Cookie strings back for the store's own parser. No cookie domain, path,
expiry or security attribute is ever reconstructed on this side, and nothing here logs a
URL, cookie value or account detail.
"""
import threading
from urllib.parse import urlsplit
from . import sites
from .errors import RunLimit
from .models import ArchiveError
from .transport import Transport, asset_url, _RetryRequest  # The retry signal is the package's own.


class WebViewCookieSource:
    """The app's WebView cookie store, reached through the native seam object.

    'bridge' is whatever the app hands the embedded Python: a WebViewCookieStore. It must
    offer cookie_header(url) and set_session_cookies(url, values); both are called from
    worker threads, and the native side marshals them to the main thread with a bound.
    """

    def __init__(self, bridge, site=sites.DEFAULT):
        for name in ('cookie_header', 'set_session_cookies'):
            if not callable(getattr(bridge, name, None)):
                raise ArchiveError('The app sign-in store is not wired to the downloader.')
        self._bridge, self.site = bridge, site

    def cookie_header(self, url):
        """The Cookie request header for this exact URL, live from the store; '' when none."""
        try:
            value = self._bridge.cookie_header(url)
        except Exception:
            # Class-free text only: the store's own message may hold a URL or credential.
            raise ArchiveError('The app sign-in store is unavailable; sign in again.') from None
        if value is None:
            return ''
        if not isinstance(value, str) or any(character in value for character in '\r\n\0'):
            raise ArchiveError('The app sign-in store returned an unusable cookie header.')
        return value

    def set_session_cookies(self, url, values):
        """Hand raw Set-Cookie strings to the store unchanged; its parser reads the attributes.

        True when the store took them; a refusal leaves the store as the sign-in left it.
        """
        if not values:
            return True
        try:
            return self._bridge.set_session_cookies(url, list(values)) is True
        except Exception:
            return False


class AndroidTransport(Transport):
    """A Transport whose Cookie header is asked live, per URL, from the app's WebView store.

    Constructed by the app and injected into the existing adapters (TapasHTTPAdapter takes
    a transport), so no session file is read and no cookie jar is built here. The inherited
    pacing, refusal, retry, budget and redirect behaviour is kept; only the cookie header
    differs: it is re-read from the store for every request and every retry, and it goes
    only to the site's own cookie hosts — never to a CDN or another origin.
    """

    def __init__(self, cookie_source, interval=1.0, client=None, metrics=None, network_guard=None,
                 agent=None, site=sites.DEFAULT):
        for name in ('cookie_header', 'set_session_cookies'):
            if not callable(getattr(cookie_source, name, None)):
                raise ArchiveError('The Android transport needs the app live cookie source.')
        super().__init__(cookies=(), interval=interval, client=client, metrics=metrics,
                         network_guard=network_guard, agent=agent, site=site)
        self.cookie_source = cookie_source
        # Set-Cookie strings a response handed over, per thread, until the request is done.
        self._observed = threading.local()

    def get(self, url, max_bytes=None, *, authorization=None, take_cookies=False):
        """A page's bytes, with a live cookie header; see Transport.get for the rest."""
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise RunLimit("The download byte budget is exhausted.")
        asset_url(url, self.site)
        host = urlsplit(url).hostname
        headers = {"Referer": self.site.referer}
        if self.agent:
            headers["User-Agent"] = self.agent
        if authorization is not None:
            # The key goes only where the site's own cookies may go, never to a CDN.
            if (host not in self.site.cookie_hosts or not isinstance(authorization, str) or not authorization.isascii()
                    or not authorization.isprintable()):
                raise ArchiveError(f"The {self.site.name} key may be sent only to {self.site.name} itself.")
            headers["Authorization"] = authorization
        self._observed.values = []
        try:
            for attempt in range(3):
                self._pace()
                # Re-read for every attempt as well: a refused request may have rotated
                # the session between tries, and the store's answer is the only authority.
                cookie = self.cookie_header_for(url, host)
                if cookie:
                    headers["Cookie"] = cookie
                else:  # No header at all, never an empty one, when the store has none for this URL.
                    headers.pop("Cookie", None)
                try:
                    return self._request(url, headers, max_bytes, attempt, take_cookies)
                except _RetryRequest as retry:
                    self._wait(retry.delay)
        finally:
            self._forward_observed(url)

    def cookie_header_for(self, url, host):
        """The live Cookie header for this full URL — the site's own hosts only, never a CDN."""
        if host not in self.site.cookie_hosts:
            return ''
        return self.cookie_source.cookie_header(url)

    def _take_cookies(self, values, host):
        """Keep only the raw Set-Cookie strings a cookie host set; nothing is parsed or stored."""
        if host in self.site.cookie_hosts and values:
            observed = getattr(self._observed, 'values', None)
            self._observed.values = [*observed, *values] if observed else list(values)

    def _forward_observed(self, url):
        """Give the finished request's Set-Cookie strings to the app's store, verbatim."""
        values = getattr(self._observed, 'values', None)
        self._observed.values = []
        if values:
            self.cookie_source.set_session_cookies(url, values)
