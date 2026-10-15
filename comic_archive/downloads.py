"""Small asset-only download capability; completion always runs on the owner thread."""
from dataclasses import dataclass, field
from typing import Callable
import math
from .models import ArchiveError

MAX_PAGE_BYTES = 100 * 1024 * 1024
MIN_IMAGE_INTERVAL = 0.25
MAX_DOWNLOAD_WORKERS = 4


def adapter_profile(adapter):
    value = getattr(adapter, 'download_profile', 'normal')
    return 'fast' if type(value) is str and value == 'fast' else 'normal'


def validate_request_interval(value, profile='normal'):
    if profile not in ('normal', 'fast'):
        raise ArchiveError('Use a known download profile.')
    minimum = 1 / 128 if profile == 'fast' else MIN_IMAGE_INTERVAL
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 60:
        raise ArchiveError(f'Image request spacing must be finite and from {minimum:g} to 60 seconds.')
    return value


def validate_download_workers(value, profile='normal'):
    if profile not in ('normal', 'fast'):
        raise ArchiveError('Use a known download profile.')
    maximum = 64 if profile == 'fast' else MAX_DOWNLOAD_WORKERS
    if type(value) is not int or not 1 <= value <= maximum:
        raise ArchiveError(f'Image download workers must be from 1 to {maximum}.')
    return value


@dataclass
class AssetDownload:
    fetch: Callable[[], bytes] = field(repr=False)
    accepted: Callable[[bytes], None] = field(default=lambda data: None, repr=False)
    released: Callable[[], None] = field(default=lambda: None, repr=False)
    finished: bool = False

    def finish(self, data):
        if not self.finished:
            try:
                self.accepted(data)
            finally:
                self.cancel()

    def cancel(self):
        if not self.finished:
            self.finished = True
            self.released()
