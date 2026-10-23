"""Fixed fast-import policy, using the same reader and authorization gates.

Normal imports keep their existing configurable pacing. Fast imports choose
one bounded policy internally; queue callers cannot supply its low-level caps.
"""
from pathlib import Path
from contextlib import contextmanager
import hashlib

from .adapters.tapas import BrowserReadTransport, TapasAdapter
from .adapters.tapas_http import TapasHTTPAdapter
from .models import ArchiveError
from .pipeline import MetadataPipeline

FAST_INTERVAL = 1 / 128
FAST_METADATA_INTERVAL = 1 / 2
FAST_WORKERS = 64
FAST_DEFAULTS = {'limit': 200, 'max_pages': 1000, 'max_bytes': 8 * 1024**3, 'max_seconds': 900}
FAST_OPERATIONS = {'archive_discovered'}


def validate_speed(value):
    if type(value) is not str or value not in ('normal', 'fast'):
        raise ArchiveError('Import speed must be normal or fast.')
    return value


def fast_options(options):
    """Keep explicit limits; fill absent ones without expanding a saved budget."""
    result = dict(options)
    for key, default in FAST_DEFAULTS.items():
        if result.get(key) is None:
            result[key] = default
    result.pop('speed', None)
    result.pop('request_interval', None)
    result['download_workers'] = FAST_WORKERS
    return result


class FastBrowserAdapter(TapasAdapter):
    def automatic_adapter(self):
        if not getattr(self, '_read_only_context_installed', False):
            self.context.route('**/*', self.network_read_only_request)
            self._read_only_context_installed = True
        if self.transport is None:
            import httpx
            self.transport = BrowserReadTransport(self.page, self.context, interval=FAST_INTERVAL,
                metadata_interval=FAST_METADATA_INTERVAL, download_profile='fast',
                deadline=getattr(self, 'deadline', None), metrics=getattr(self, 'asset_metrics', None),
                network_guard=getattr(self, 'network_guard', None))
            self.transport.assets.client.close()
            self.transport.assets.client = httpx.Client(timeout=httpx.Timeout(10, connect=30), follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=FAST_WORKERS, max_keepalive_connections=FAST_WORKERS))
        self.transport.deadline = getattr(self, 'deadline', None)
        self.transport.cancel_check = getattr(self, 'cancel_check', None)
        self.transport.assets.interval = FAST_INTERVAL
        self.transport.assets.deadline = self.transport.deadline
        self.transport.assets.cancel_check = self.transport.cancel_check
        if getattr(self, 'asset_metrics', None) is not None:
            self.transport.assets.metrics = self.asset_metrics
        # A pooled image client never replaces fresh identity/access checks.
        self._automatic = TapasHTTPAdapter(transport=self.transport,
            expected_account_id=getattr(self, 'expected_account_id', None))
        return self._automatic


class FastImportAdapter(MetadataPipeline):
    download_profile = 'fast'

    def __init__(self, session, preferred_browser='auto', headless=False):
        if not isinstance(session, Path):
            raise ArchiveError('A fast import needs the existing private session directory.')
        self.session = session
        super().__init__(FastBrowserAdapter, None, session, preferred_browser,
                         headless=headless, interval=FAST_INTERVAL)


class FastWorkerSession:
    """One lazy browser/asset pool per worker; no manifests cross batch gates."""

    def __init__(self, session, preferred_browser='firefox', *, headless=False):
        self.session = session
        self.preferred_browser = preferred_browser
        self.headless = headless
        self.adapter = None
        self.saved_state = None

    def __enter__(self):
        return self

    def _state_signature(self):
        path = self.session / 'state.json'
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ArchiveError('Refusing a symlink in saved session storage.')
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            raise ArchiveError('Saved session is unavailable; restore ordinary login before importing.') from None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino, digest

    def close(self, problem=None):
        adapter, self.adapter = self.adapter, None
        self.saved_state = None
        if adapter is not None:
            adapter.__exit__(type(problem) if problem is not None else None, problem,
                             problem.__traceback__ if problem is not None else None)

    def __exit__(self, kind, problem, trace):
        self.close(problem)

    def _open_adapter(self):
        return FastImportAdapter(self.session, self.preferred_browser, headless=self.headless)

    @contextmanager
    def batch(self):
        # An ordinary login/session import or another browser's capture must
        # be adopted before new work; a cached browser cannot hide that change.
        if self.adapter is not None:
            try:
                changed = self._state_signature() != self.saved_state
            except ArchiveError:
                self.close()
                raise
            if changed:
                self.close()
        if self.adapter is None:
            candidate = self._open_adapter()
            candidate.__enter__()
            self.adapter = candidate
        self.adapter.begin_batch()
        try:
            yield self.adapter
        except BaseException as problem:
            try:
                self.close(problem)
            except Exception:
                # Keep the original typed receipt if browser cleanup also fails.
                pass
            raise
        else:
            if not self.adapter.reusable:
                try:
                    self.close()
                except Exception:
                    pass  # Cleanup cannot replace the job's exact terminal result.
            else:
                try:
                    self.saved_state = self._state_signature()
                except ArchiveError:
                    try:
                        self.close()
                    except Exception:
                        pass  # Next admission must load the saved login again.
