"""One-episode lookahead; one thread owns every browser call."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from urllib.parse import urlsplit

from .errors import AccessDenied, RunLimit, UnsupportedContent
from .cancellation import RunCancelled
from .models import ArchiveError


class MetadataPipeline:
    supports_novels = True
    supports_comic_series_classification = True
    supports_series_metadata = True

    def __init__(self, factory, profile, *args, **kwargs):
        self.factory, self.profile, self.args, self.kwargs = factory, profile, args, kwargs
        self.deadline = self.cancel_check = self.asset_metrics = self.expected_account_id = None
        self._pending = None
        self.reusable = True

    def __enter__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tapas-metadata')
        def open_browser():
            self._inner = self.factory(*self.args, **self.kwargs)
            self._inner.__enter__()
        try:
            self._pool.submit(open_browser).result()
        except BaseException:
            self._pool.shutdown(wait=True, cancel_futures=True)
            raise
        return self

    def __exit__(self, *args):
        try:
            self.finish_batch(args[1] if len(args) > 1 else None)
        finally:
            try:
                self._pool.submit(self._inner.__exit__, *args).result()
            finally:
                self._pool.shutdown(wait=True, cancel_futures=True)

    def begin_batch(self):
        if self._pending is not None or not self.reusable:
            raise ArchiveError('Previous browser work must finish before another batch.')
        self.deadline = self.cancel_check = self.asset_metrics = self.expected_account_id = None

    @contextmanager
    def batch_boundary(self):
        problem = None
        try:
            yield
        except BaseException as error:
            problem = error
            raise
        finally:
            self.finish_batch(problem)

    def finish_batch(self, problem=None):
        # Drain while the job still owns its inventory, counters and failure
        # handlers. A late refusal must retain the completed batch's receipt.
        limited = isinstance(problem, RunLimit) and not isinstance(problem, RunCancelled)
        if problem is not None and not limited:
            self.reusable = False
        if self._pending is None:
            return
        pending, self._pending = self._pending, None
        try:
            pending[1].result()
        except RunCancelled:
            self.reusable = False
            if problem is None or limited:
                raise
        except (AccessDenied, RunLimit, UnsupportedContent):
            pass
        except BaseException:
            self.reusable = False
            if problem is None or limited:
                raise

    @property
    def transport(self):
        return self._inner.transport

    def _submit(self, method, *args, **kwargs):
        state = {key: getattr(self, key) for key in
                 ('deadline', 'cancel_check', 'asset_metrics', 'expected_account_id')}
        def invoke():
            for key, value in state.items():
                setattr(self._inner, key, value)
            return getattr(self._inner, method)(*args, **kwargs)
        return self._pool.submit(invoke)

    def account_identity(self):
        return self._submit('account_identity').result()

    def episode_url(self, key):
        from .adapters.tapas_http import TapasHTTPAdapter
        return TapasHTTPAdapter.episode_url(self, key)

    def prefetch_episode(self, url):
        if self._pending is not None:
            raise ArchiveError('Only one next episode may be prepared.')
        self._pending = (url, self._submit('capture_automatic', url))
        if self.profile is not None:
            self.profile.calls['prefetch_submitted'] += 1

    def capture_automatic(self, url):
        with self.profile.phase('capture_wait') if self.profile is not None else nullcontext():
            if self._pending is None:
                return self._submit('capture_automatic', url).result()
            pending, self._pending = self._pending, None
            if pending[0] != url:
                raise ArchiveError('Prepared metadata does not match the next selected episode.')
            return pending[1].result()

    def asset_request(self, page, max_bytes=None):
        self._check_prepared_failure()
        from .adapters.tapas_http import TapasHTTPAdapter
        return TapasHTTPAdapter.asset_request(self, page, max_bytes=max_bytes)

    def _check_prepared_failure(self):
        if self._pending is not None and self._pending[1].done():
            error = self._pending[1].exception()
            if error is not None and not isinstance(error, (AccessDenied, UnsupportedContent)):
                raise error

    def retrieve(self, page, max_bytes=None):
        self._check_prepared_failure()
        if urlsplit(page.location).hostname == 'tapas.io':
            return self._submit('retrieve', page, max_bytes=max_bytes).result()
        return self.transport.assets.get(page.location, max_bytes=max_bytes)

    def series_metadata(self, key):
        return self._submit('series_metadata', key).result()

    def series_identity(self, key):
        return self._submit('series_identity', key).result()

    def inspect_series(self, key, **kwargs):
        return self._submit('inspect_series', key, **kwargs).result()

    def episode_access(self, url):
        return self._submit('episode_access', url).result()

    def retrieve_cover(self, series, max_bytes=None):
        return self._submit('retrieve_cover', series, max_bytes=max_bytes).result()

    def refresh(self, episode):
        updated = self._submit('capture_automatic', episode.source_url, series_id=episode.series_id,
                               title=episode.title, order=episode.order).result().episodes[0]
        if (len(updated.pages) != len(episode.pages) or any(
                (urlsplit(a.location).hostname, urlsplit(a.location).path) !=
                (urlsplit(b.location).hostname, urlsplit(b.location).path)
                for a, b in zip(updated.pages, episode.pages))):
            raise ArchiveError('Reader page manifest changed; restart capture to preserve ordering.')
        return updated
