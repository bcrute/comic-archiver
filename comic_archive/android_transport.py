"""The Android HTTP seam: the app's WebView cookie store, wired into the canonical Transport.

The core Transport (since the frozen beta contracts) itself asks a live cookie_source for the
Cookie header of each request — after policy admission, per attempt, only for the site's own
cookie hosts — and forwards own-origin raw Set-Cookie strings to a cookie_sink instead of its
jar. This module is only the bridge to the native seam object and a thin constructor: the
Kotlin WebViewCookieStore behind the frozen CookieHeaderSource/SetCookieSink interfaces. The
store stays the single cookie authority; no domain, path, expiry or security attribute is
ever reconstructed or persisted on this side, and nothing here logs a URL, cookie value or
account detail.
"""
from . import sites
from .models import ArchiveError
from .transport import Transport


class WebViewCookieSource:
    """The app's WebView cookie store, reached through the native seam object.

    'bridge' is whatever the app hands the embedded Python: a WebViewCookieStore implementing
    the frozen Kotlin interfaces (headerFor/apply). Both are called from worker threads; the
    native side marshals to the main thread and answers within a bound.
    """

    def __init__(self, bridge, site=sites.DEFAULT):
        for name in ('headerFor', 'apply'):
            if not callable(getattr(bridge, name, None)):
                raise ArchiveError('The app sign-in store is not wired to the downloader.')
        self._bridge, self.site = bridge, site

    def header_for(self, url):
        """cookie_source for the core Transport: the live Cookie header for this exact URL.

        The core asks per request and per attempt and never for a CDN; see Transport.get.
        """
        try:
            value = self._bridge.headerFor(url)
        except Exception:
            # Fixed text only: the store's own message may hold a URL or credential.
            raise ArchiveError('The app sign-in store is unavailable; sign in again.') from None
        if value is None:
            return ''
        if not isinstance(value, str) or any(character in value for character in '\r\n\0'):
            raise ArchiveError('The app sign-in store returned an unusable cookie header.')
        return value

    def apply(self, url, values):
        """cookie_sink for the core Transport: raw Set-Cookie strings to the store's own parser.

        Values pass through verbatim; the store alone reads their attributes. A store that
        cannot take them stays as it is — the authoritative store's own later refusal of a
        stale cookie is the honest failure, never a reconstructed one here.
        """
        if not values:
            return
        try:
            self._bridge.apply(url, list(values))
        except Exception:
            pass


class AndroidTransport(Transport):
    """The canonical Transport constructed for Android: the live store wired in, no jar.

    Every request behaviour — pacing, admission, retries, refusals, byte budgets, redirect
    hops, the site-key boundary — is the core Transport's own (see transport.py); this class
    adds nothing but the bridge validation and the cookie_source/cookie_sink wiring, so there
    is exactly one implementation of the request path.
    """

    def __init__(self, cookie_source, interval=1.0, client=None, metrics=None, network_guard=None,
                 agent=None, site=sites.DEFAULT):
        source = cookie_source if isinstance(cookie_source, WebViewCookieSource) else \
            WebViewCookieSource(cookie_source, site)
        super().__init__(cookies=(), interval=interval, client=client, metrics=metrics,
                         network_guard=network_guard, agent=agent, site=site,
                         cookie_source=source.header_for, cookie_sink=source.apply)
        self.store = source

    @property
    def assets(self):
        """The pipeline's asset reader: this transport itself.

        MetadataPipeline.retrieve reads a non-site page as `transport.assets.get(...)`; the
        canonical request path already refuses the site's cookies to every other origin, so
        the one live transport is the whole answer and no second client exists.
        """
        return self
