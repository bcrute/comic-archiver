"""HTTPS retrieval with shared request spacing; never log URLs or secrets."""
import time
import logging
import threading
from urllib.parse import urlsplit
from .models import ArchiveError
from . import sites
from .errors import AssetReadUnavailable, NetworkUnavailable, RunLimit
from .downloads import validate_request_interval
from .transfer_metrics import TransferMetrics, network_failure_code
TRANSPORT_VERSION = 10
# How much of a refused request's answer is kept with its error.
REFUSAL_BYTES = 4096


class _PrivateHTTPLogs(logging.Filter):
    _comic_private_http_logs = True

    def filter(self, record):
        return False


def protect_http_logs():
    """Suppress raw HTTP library diagnostics in the importer process.

    HTTPX logs complete signed URLs at INFO; HTTPcore DEBUG traces can include
    response cookies. Use our sanitized progress and failure codes instead.
    Existing handlers remain configured, but these library records are dropped.
    """
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and any(name == prefix or name.startswith(prefix + '.') for prefix in ('httpx', 'httpcore')):
            logger.setLevel(logging.CRITICAL + 1)
            if not any(getattr(item, '_comic_private_http_logs', False) for item in logger.filters):
                logger.addFilter(_PrivateHTTPLogs())


class ExpiredAsset(ArchiveError):
    pass


def asset_url(url: str, site=sites.DEFAULT):
    u = urlsplit(url)
    if u.scheme != "https" or u.username or u.password or u.port or not site.asset_host(u.hostname):
        raise ArchiveError(f"Asset is outside the confirmed {site.name} HTTPS host boundary.")
    return url


def cookie_matches(domain, host):
    """Whether a browser would send a cookie set for this domain to this host."""
    domain = domain.lstrip('.')
    return host == domain or host.endswith('.' + domain)


class Transport:
    def __init__(self, cookies=(), interval=1.0, client=None, metrics=None, profile='normal', network_guard=None,
                 agent=None, site=sites.DEFAULT):
        validate_request_interval(interval, profile)
        self.site = site
        if client is None:
            try:
                import httpx
            except ImportError:
                raise ArchiveError("Install httpx to retrieve remote pages.") from None
            client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        protect_http_logs()
        self.client = client
        # The site's own cookies go only to the hosts its profile names, never to a CDN.
        self.cookies = tuple((c["domain"], f"{c['name']}={c['value']}") for c in cookies if site.keeps_cookie(c["domain"]))
        # What the browser that reads the pages calls itself; without one the HTTP client gives its own name.
        self.agent = agent if isinstance(agent, str) and agent.isascii() and agent.isprintable() and agent.strip() else None
        self.interval = interval
        self.last = 0.0
        self._spacing_lock = threading.Lock()
        self._responses = threading.local()
        self.last_response = None
        self._rate_limited = threading.Event()
        self._rate_limit_error = None
        self.deadline = None
        self.cancel_check = None
        self.network_guard = network_guard
        self.metrics = metrics if metrics is not None else TransferMetrics()

    @property
    def last_response(self):
        return getattr(self._responses, 'value', None) if hasattr(self, '_responses') else None

    @last_response.setter
    def last_response(self, value):
        if hasattr(self, '_responses'):
            self._responses.value = value

    def _pace(self):
        # Hold this only until a request starts, never during network transfer.
        # Retries and all image threads share the same aggregate rate ceiling.
        with self._spacing_lock:
            self._check_budget()
            if self._rate_limited.is_set():
                self._raise_rate_limit()
            self._wait(max(0, self.last + self.interval - time.monotonic()))
            if self._rate_limited.is_set():
                self._raise_rate_limit()
            self.last = time.monotonic()

    def get(self, url, max_bytes=None, *, authorization=None, take_cookies=False):
        """A page's bytes. 'authorization' is the site's own key for its API; 'take_cookies' keeps the cookies
        the answer sets, as a browser would, for the requests that follow."""
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise RunLimit("The download byte budget is exhausted.")
        asset_url(url, self.site)
        host = urlsplit(url).hostname
        headers = {"Referer": self.site.referer}
        if self.agent:
            headers["User-Agent"] = self.agent
        cookie = self.cookie_header(host)
        if cookie:
            headers["Cookie"] = cookie
        if authorization is not None:
            # The key goes only where the site's own cookies may go, never to a CDN.
            if (host not in self.site.cookie_hosts or not isinstance(authorization, str) or not authorization.isascii()
                    or not authorization.isprintable()):
                raise ArchiveError(f"The {self.site.name} key may be sent only to {self.site.name} itself.")
            headers["Authorization"] = authorization
        for attempt in range(3):
            self._pace()
            try:
                return self._request(url, headers, max_bytes, attempt, take_cookies)
            except _RetryRequest as retry:
                self._wait(retry.delay)

    def cookie_header(self, host):
        if host not in self.site.cookie_hosts:
            return ''
        return "; ".join(pair for domain, pair in self.cookies if cookie_matches(domain, host))

    def _take_cookies(self, values, host):
        """Keep the cookies a site's host sets for itself; a cookie for another site is ignored."""
        kept = {(domain.lstrip('.'), pair.split('=', 1)[0]): pair for domain, pair in self.cookies}
        for value in values:
            pair, *attributes = value.split(';')
            name, equals, content = pair.strip().partition('=')
            domain = host
            for attribute in attributes:
                key, _, setting = attribute.strip().partition('=')
                if key.lower() == 'domain' and setting.strip():
                    domain = setting.strip().lower()
            if equals and name and self.site.keeps_cookie(domain) and cookie_matches(domain, host):
                kept[(domain.lstrip('.'), name)] = f'{name}={content}'
        self.cookies = tuple((domain, pair) for (domain, _), pair in kept.items())

    def _check_budget(self):
        if self.network_guard is not None:
            self.network_guard.check()
        if callable(self.cancel_check):
            self.cancel_check()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RunLimit('The job time budget is exhausted; cached pages are retained.')

    def _wait(self, seconds):
        self._check_budget()
        if self.deadline is None and self.cancel_check is None and self.network_guard is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_budget()
            time.sleep(min(.1, max(0, end-time.monotonic())))
        self._check_budget()

    def _raise_rate_limit(self):
        error = ArchiveError(f'{self.site.name} rate limit reached; this job has stopped.')
        error.source_http_status = 429
        if self._rate_limit_error is not None:
            delay = getattr(self._rate_limit_error, 'retry_after_seconds', None)
            if delay is not None:
                error.retry_after_seconds = delay
        raise error

    def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
        # Only CDN assets enter these metrics, never same-origin account JSON.
        measured = urlsplit(url).hostname != self.site.host
        started = time.monotonic()
        status = None
        failure = None
        data = bytearray()
        if measured:
            self.metrics.begin(started, retry=attempt > 0)
        try:
            try:
                options = {}
                if self.deadline is not None:
                    import httpx
                    if isinstance(self.client, httpx.Client):
                        remaining = max(.001, self.deadline-time.monotonic())
                        options['timeout'] = httpx.Timeout(**{key:min(remaining, value) if value is not None else remaining
                                                             for key,value in self.client.timeout.as_dict().items()})
                self._check_budget()
                with self.client.stream("GET", url, headers=headers, **options) as response:
                    status = response.status_code
                    self.last_response = {"status": response.status_code,
                                          "content_type": response.headers.get("Content-Type", response.headers.get("content-type", "")).split(";", 1)[0],
                                          # Where the next page of a paged list is, when the site says so.
                                          "link": response.headers.get("Link", response.headers.get("link", ""))}
                    if response.status_code in {401, 403, 404, 410}:
                        raise ExpiredAsset("Page URL expired or access was revoked.")
                    if response.status_code == 429:
                        from .retry_after import retain_retry_after
                        error = retain_retry_after(ArchiveError(f'{self.site.name} rate limit reached; this job has stopped.'), response.headers.get('Retry-After'))
                        error.source_http_status = 429
                        self._rate_limit_error = error
                        self._rate_limited.set()
                        raise error
                    if 500 <= response.status_code < 600:
                        from .retry_after import retry_after_seconds, retain_retry_after
                        if attempt == 2:
                            if response.status_code in {502,503,504}:
                                error=NetworkUnavailable('Image server remains unavailable; resume later.')
                                error.source_http_status=response.status_code
                                raise retain_retry_after(error, response.headers.get('Retry-After'))
                            raise ArchiveError("Server remains unavailable; resume later.")
                        delay = retry_after_seconds(response.headers.get('Retry-After'))
                        wait = max(2 ** (attempt + 1), delay or 0)
                        if wait > 60:
                            error = NetworkUnavailable('Image server requests a longer pause; cached pages are retained.')
                            error.source_http_status = response.status_code
                            error.retry_after_seconds = wait
                            raise error
                        raise _RetryRequest(wait)
                    if response.status_code != 200:
                        error = ArchiveError(f"Page request refused or redirected (HTTP {response.status_code}); refresh in the reader.")
                        error.source_http_status = response.status_code
                        # The start of what the site said, for its reader to tell a refusal it expects from a fault.
                        answer = bytearray()
                        for chunk in response.iter_bytes():
                            answer.extend(chunk)
                            if len(answer) >= REFUSAL_BYTES:
                                break
                        error.source_answer = bytes(answer[:REFUSAL_BYTES])
                        raise error
                    if take_cookies:
                        self._take_cookies(response.headers.get_list('set-cookie'), urlsplit(url).hostname)
                    for chunk in response.iter_bytes():
                        self._check_budget()
                        data.extend(chunk)
                        if max_bytes is not None and len(data) > max_bytes:
                            raise RunLimit("The next page exceeds the remaining download byte budget.")
                        if len(data) > 100 * 1024 * 1024:
                            raise ArchiveError("Page exceeds the 100 MiB limit.")
                    self._check_budget()
                    return bytes(data)
            except _RetryRequest:
                failure = 'http_error'
                raise
            except ArchiveError as error:
                failure = 'download_limit' if isinstance(error,RunLimit) else 'http_error'
                raise
            except Exception as error:
                import httpx
                if not isinstance(error,httpx.RequestError):
                    failure = 'unsupported_response'
                    raise ArchiveError('Page retrieval failed; this job has stopped.') from None
                failure = network_failure_code(error)
                if attempt == 2:
                    kind = AssetReadUnavailable if measured and failure in {'read_timeout','read_error'} else NetworkUnavailable
                    unavailable = kind("Page network request failed; resume later.")
                    unavailable.network_failure_code = failure
                    raise unavailable from None
                raise _RetryRequest(2 ** (attempt + 1)) from None
        finally:
            if measured:
                self.metrics.finish(started,time.monotonic(),status,len(data),failure)

    def close(self):
        self.client.close()


class _RetryRequest(Exception):
    def __init__(self, delay):
        self.delay = delay
