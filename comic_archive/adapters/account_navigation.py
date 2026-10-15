"""Normal account navigation policy; no purchase or settings mutation routes."""
import re
from urllib.parse import parse_qsl, urlsplit
from ..models import ArchiveError, canonical_url


def navigation_path(href):
    try:
        canonical_url(href)
        parsed = urlsplit(href)
    except (ArchiveError, TypeError, ValueError):
        return None
    if parsed.hostname != 'tapas.io' or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,100}',parsed.path):
        return None
    if re.search(r'buy|purchase|unlock|charge|checkout|payment|subscribe|logout|signout|opt-out|popup|delete|withdraw|refund',parsed.path,re.I):
        return None
    query = parse_qsl(parsed.query,keep_blank_values=True)
    if any(key != 'trigger' or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value) for key,value in query):
        return None
    return parsed.path
