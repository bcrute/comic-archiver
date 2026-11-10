"""Read the normal transaction pager without retaining financial row contents.

The website's history handler replaces .body-holder with data.body, advances
pagination.index and disables Older only when pagination.has_next is false.
This is candidate discovery, not an independent proof of all account ownership.
"""
import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..errors import AuthenticationError, MetadataError
from ..models import ArchiveError

LIMIT = 4 * 1024 * 1024
REASONS = frozenset({'row_structure', 'row_balance', 'row_size', 'response_size', 'response_encoding',
                     'response_shape', 'pagination_shape', 'row_incomplete', 'row_empty'})


def invalid(reason, message):
    error = MetadataError(message)
    error.history_reason = reason
    return error


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
CONTENT = re.compile(r'/(?:series/[A-Za-z0-9_-]{1,100}(?:/info)?|episode/[1-9][0-9]{0,19})')


class HistoryRows(HTMLParser):
    """Recognize history rows; never infer an empty history from unknown markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.row = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        is_row = tag == 'tr' or 'tr' in (attrs.get('class') or '').split()
        if is_row:
            if self.row is not None or len(self.rows) >= 1000:
                raise invalid('row_structure', 'History rows have an unsupported structure or count.')
            self.row = {'text': '', 'links': set(), 'depth': len(self.stack)}
        if self.row is not None and tag == 'a':
            href = attrs.get('href')
            if isinstance(href, str):
                try:
                    parts = urlsplit(urljoin('https://tapas.io/', href))
                except ValueError:
                    parts = None
                if parts and parts.scheme == 'https' and parts.netloc == 'tapas.io' and CONTENT.fullmatch(parts.path):
                    self.row['links'].add(urlunsplit(('https', 'tapas.io', parts.path, '', '')))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            raise invalid('row_balance', 'History rows have an unbalanced structure.')
        self.stack.pop()
        if self.row is not None and len(self.stack) == self.row['depth']:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, text):
        if self.row is not None:
            self.row['text'] += text
            if len(self.row['text']) > 16384:
                raise invalid('row_size', 'A history row exceeds the supported size.')


def read_page(raw, index):
    """Validate a server-indexed answer before its cursor or candidates are saved."""
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        raise invalid('response_size', 'History metadata exceeds the supported response size.')
    try:
        answer = json.loads(raw)
    except (ValueError, UnicodeError):
        raise invalid('response_encoding', 'History metadata is not a readable account response.') from None
    if not isinstance(answer, dict) or type(answer.get('code')) is not int:
        raise invalid('response_shape', 'History metadata has an unsupported response shape.')
    if answer['code'] == 401:
        raise AuthenticationError('Tapas requires a fresh browser login to read purchase history.')
    if answer['code'] != 200:
        error = ArchiveError('Tapas refused the normal purchase-history read.')
        if answer['code'] in {403, 429, 502, 503, 504}:
            error.source_http_status = answer['code']
        raise error
    data = answer.get('data')
    pagination = data.get('pagination') if isinstance(data, dict) else None
    if (not isinstance(pagination, dict) or type(pagination.get('index')) is not int
            or pagination['index'] != index or type(pagination.get('has_next')) is not bool
            or not isinstance(data.get('body'), str)):
        raise invalid('pagination_shape', 'History pagination did not confirm the requested page and boundary.')
    body = data['body']
    parser = HistoryRows()
    parser.feed(body)
    parser.close()
    if parser.stack or parser.row is not None:
        raise invalid('row_incomplete', 'History rows have an incomplete structure.')
    empty = not body.strip() and not pagination['has_next']
    result = {'authenticated': True, 'scope_verified': True, 'page_identity': f'index:{index}',
              'record_ids': [], 'links': [], 'unresolved_rows': 0,
              'next_cursor': {'contract': 'tapas-transactions-index-v1', 'index': index + 1} if pagination['has_next'] else None,
              'boundary_verified': not pagination['has_next'], 'empty_verified': empty}
    links = set()
    for row in parser.rows:
        text = ' '.join(row['text'].split())
        row_links = row['links']
        if not text and not row_links:
            raise invalid('row_empty', 'History contains an unreadable transaction row.')
        series = {url for url in row_links if '/series/' in url}
        row_links = series or row_links
        # Ink purchases and support payments do not identify archived episodes.
        # An unassociated content-unlock row must remain visible as incomplete.
        if not row_links and re.search(r'\b(?:episode|chapter|unlocked|unlock|series)\b', text, re.IGNORECASE):
            result['unresolved_rows'] += 1
        identity = json.dumps([text, sorted(row_links)], ensure_ascii=True)
        result['record_ids'].append(hashlib.sha256(identity.encode()).hexdigest())
        links.update(row_links)
    result['links'] = sorted(links)
    return result
