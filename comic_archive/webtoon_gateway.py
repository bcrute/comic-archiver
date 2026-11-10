"""Webtoon's read-only mobile protocol and its narrow website-session delegation.

The gateway signing scheme is documented by the MIT-licensed webtoon-api client:
https://github.com/s0ko1ex/webtoon-api . The shared client value below identifies
that public protocol; it is not an account credential or evidence of ownership.
The authenticated reader endpoint decides access to each requested episode.

Signing-protocol attribution: Copyright (c) 2023 [Alexandr Sokolov]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import base64
import hmac
import json
import logging
from urllib.parse import urlencode, urlsplit

from .errors import AccessDenied, AuthenticationError, MetadataError
from .models import ArchiveError


HOST = 'global.apis.naver.com'
CLOCK = f'https://{HOST}/currentTime'
BASE = f'https://{HOST}/lineWebtoon/webtoon/'
ROUTES = frozenset({'myUnlockedTitleProductsV1.json', 'myUnlockedEpisodeProductsV1.json',
                    'titleHomeMainV3', 'episodeInfoWithLogin.json'})
COOKIE_NAMES = frozenset({'NEO_SES', 'NEO_CHK'})
MAX_DOCUMENT = 4 * 1024 * 1024
PAGE_SIZE = 20
MAX_PAGES = 1000
AGENT = 'nApps (Android 13; linewebtoon; 2.12.2)'
PUBLIC_GATEWAY_KEY = b'gUtPzJFZch4ZyAGviiyH94P99lQ3pFdRTwpJWDlSGFfwgpr6ses5ALOxWHOIT7R1'


def gateway_route(url, site):
    """Recognize only the observed clock and read-only routes, never a host wildcard."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None  # The ordinary asset boundary supplies its sanitized refusal.
    if site.key != 'webtoon' or parts.hostname != HOST:
        return None
    try:
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.fragment or parts.netloc != HOST):
            raise ValueError
        if parts.path == '/currentTime' and not parts.query:
            return 'clock'
        if parts.path.startswith('/lineWebtoon/webtoon/'):
            route = parts.path.removeprefix('/lineWebtoon/webtoon/')
            if route in ROUTES:
                return route
    except ValueError:
        pass
    raise ArchiveError('Webtoon mobile requests must use a confirmed read-only HTTPS route.')


def session_cookie(header):
    """Keep only the observed session pair; preserve their values without logging them."""
    if not isinstance(header, str) or not header.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in header):
        raise AuthenticationError('The Webtoon session is unavailable; sign in again.')
    kept = {}
    for item in header.split(';'):
        name, equals, value = item.strip().partition('=')
        if name not in COOKIE_NAMES:
            continue
        if not equals or not value or name in kept:
            raise AuthenticationError('The Webtoon session is ambiguous; sign in again.')
        kept[name] = value
    if not kept.get('NEO_SES'):
        return ''
    return '; '.join(f'{name}={value}' for name, value in kept.items())


def signed_url(route, parameters, timestamp):
    if route not in ROUTES or not isinstance(timestamp, bytes) or not timestamp.isdigit() or not 10 <= len(timestamp) <= 16:
        raise MetadataError('Webtoon supplied an unusable gateway clock or route.')
    query = {'v': 4} if route == 'episodeInfoWithLogin.json' else {}
    query.update(parameters)
    query.update(platform='APP_ANDROID', language='en', serviceZone='GLOBAL')
    url = BASE + route + '?' + urlencode(query)
    # The service specifies this HMAC-SHA1 protocol, including the 255-byte URL prefix.
    signature = base64.b64encode(hmac.digest(PUBLIC_GATEWAY_KEY, url.encode('utf-8')[:255] + timestamp, 'sha1')).decode('ascii')
    return url + '&' + urlencode({'msgpad': timestamp.decode('ascii'), 'md': signature})


class MobileAPI:
    def __init__(self, transport, account_check):
        self.transport = transport
        self.account_check = account_check

    def request(self, route, **parameters):
        self.account_check()
        clock = self.transport.get(CLOCK, max_bytes=32).strip()
        try:
            data = self.transport.get(signed_url(route, parameters, clock), max_bytes=MAX_DOCUMENT)
        except ArchiveError as error:
            if getattr(error, 'source_http_status', None) == 401:
                raise AuthenticationError('Sign in to Webtoon again.') from None
            # A generic HTTP refusal may be a challenge or provider outage.
            # Stop the run rather than mislabeling each episode as unowned.
            raise
        self.account_check()
        try:
            message = json.loads(data)['message']
            if not isinstance(message, dict) or message.get('type') != 'response' or message.get('service') != 'com.naver.webtoon':
                raise ValueError
            code = message.get('code')
            if code == 1001:
                raise AuthenticationError('Sign in to Webtoon again.')
            if code == 1002 and route == 'episodeInfoWithLogin.json':
                logging.getLogger('comic_archive.download').info(
                    'reader access: provider=webtoon result=denied stage=reader_manifest gateway_code=1002')
                raise AccessDenied('Webtoon did not authorize this account request.')
            if code is not None:
                raise MetadataError('Webtoon returned an unrecognized account response; this run stopped.')
            result = message['result']
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError):
            raise MetadataError('Webtoon returned an unexpected mobile response.') from None

    def listed(self, route, field, **parameters):
        seen = set()
        for page in range(MAX_PAGES):
            result = self.request(route, **parameters, offset=page * PAGE_SIZE, pageSize=PAGE_SIZE)
            rows = result.get(field)
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise MetadataError('Webtoon returned an unexpected mobile list.')
            for row in rows:
                if not isinstance(row, dict):
                    raise MetadataError('Webtoon returned an unexpected mobile list entry.')
                identity = (row.get('titleNo'), row.get('episodeNo'))
                if any(type(number) is not int or number <= 0 for number in identity if number is not None):
                    raise MetadataError('Webtoon returned an invalid mobile list identity.')
                if identity in seen or identity[0] is None:
                    raise MetadataError('Webtoon repeated an entry in its mobile list.')
                seen.add(identity)
                yield row
            if len(rows) < PAGE_SIZE:
                return
        raise MetadataError('Webtoon did not finish its mobile list within the supported bound.')
