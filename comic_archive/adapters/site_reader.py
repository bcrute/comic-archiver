"""What the readers of sites other than Tapas share: they read over HTTPS with one downloader, which keeps to
the job's pace, time limit and stop check."""
from ..models import ArchiveError
from ..transport import asset_url

COVER_LIMIT = 10 * 1024 * 1024


class SiteReader:
    site = None
    supports_series_metadata = True
    expected_account_id = None
    _manifest = None

    # A job sets its pace, time limit, stop check and transfer counts on the reader; the downloader keeps to them.
    @property
    def interval(self):
        return self.transport.interval

    @interval.setter
    def interval(self, value):
        self.transport.interval = value

    @property
    def deadline(self):
        return self.transport.deadline

    @deadline.setter
    def deadline(self, value):
        self.transport.deadline = value

    @property
    def cancel_check(self):
        return self.transport.cancel_check

    @cancel_check.setter
    def cancel_check(self, value):
        self.transport.cancel_check = value

    @property
    def asset_metrics(self):
        return self.transport.metrics

    @asset_metrics.setter
    def asset_metrics(self, value):
        if value is not None:
            self.transport.metrics = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.transport.close()

    def manifest(self):
        return self._manifest

    def retrieve(self, page, max_bytes=None):
        return self.transport.get(page.location, max_bytes=max_bytes) if max_bytes is not None else self.transport.get(page.location)

    def retrieve_cover(self, series, max_bytes=None):
        if not series.cover_url:
            return None
        limit = min(max_bytes, COVER_LIMIT) if max_bytes is not None else COVER_LIMIT
        return self.transport.get(asset_url(series.cover_url, self.site), max_bytes=limit)

    def refresh(self, episode):
        new = self.capture_automatic(episode.source_url).episodes[0]
        if [page.location for page in new.pages] != [page.location for page in episode.pages]:
            raise ArchiveError(f'The episode changed on {self.site.name} while it was being saved; it is saved again from the start.')
        return new
