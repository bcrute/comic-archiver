"""Explicit public series links; never accept foreign hosts or arbitrary routes."""
import re
from .models import ArchiveError, canonical_url


def series_link(value):
    url = canonical_url(value)
    match = re.fullmatch(r'https://tapas\.io/series/([A-Za-z0-9_-]{1,100})(?:/info)?/?', url or '')
    if not match:
        raise ArchiveError('Paste a public https://tapas.io/series/... link.')
    return match[1]
