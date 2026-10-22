"""The Android worker's Tapas reader: the core's own HTTP flow over the live WebView session.

This is the smallest adapter the native runtime injects into jobs.archive_discovered/jobs.sync
in place of the desktop browser adapter. Every metadata, access, pagination and archive
semantic is the existing core's — TapasHTTPAdapter over the canonical Transport with the
frozen cookie_source/cookie_sink seams (see android_transport) — so the WebView store stays
the single cookie authority and no jar, browser, subprocess or new route exists on this side.
Authenticated identity is the core's own who-am-i check, run fail-closed before any work; a
cookie's presence alone never claims sign-in.

Documented divergences from the desktop browser flow (verified at the integrated point
against the stand-in site before beta; each fails closed into honest blockers, never a guess):

- Library discovery requests the site's own verified reading-list pages
  (library_navigation.reading_list_location) over HTTP instead of clicking rendered pager
  controls; the card extraction below reproduces the rendered-DOM rules (adapters/tapas.py
  _series_cards) from the served HTML, and a supplied adjacent Next link stands in for the
  visible Next control the browser flow requires.
- The Free Episodes filter and transaction history are visible browser flows; discovery
  reports an honest blocker for the first and refuses the second.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from . import sites
from .android_transport import AndroidTransport
from .errors import AuthenticationError
from .adapters.library_navigation import reading_list_location
from .models import ArchiveError
from .transport import Transport


def site_addresses():
    """The sites table for the native engine's constructor, as plain public-safe values.

    The Kotlin WebViewSignInEngine takes its site map as explicit caller input; this hands
    the bridge the real table (comic_archive.sites) so no second site list exists anywhere.
    Only each site's addresses travel — never anything about a saved sign-in.
    """
    return [{'key': site.key, 'home': site.home, 'signInPath': site.signin_path,
             'cookieHosts': list(site.cookie_hosts), 'signinHosts': list(site.signin_hosts)}
            for site in sites.SITES if site.available]


# The account lists the HTTP reading-list route itself verifies (reading_list_location).
HTTP_CATEGORIES = ('subscribed', 'recent', 'updated')


def _series_path_url(url):
    """The site's own series URL shape, exactly as the rendered-DOM extraction admits it."""
    parsed = urlsplit(url)
    if (parsed.scheme == 'https' and parsed.hostname == 'tapas.io' and not parsed.username
            and not parsed.password and not parsed.port
            and re.fullmatch(r'/series/[A-Za-z0-9_-]{1,100}(?:/info)?', parsed.path)):
        return f'https://tapas.io{parsed.path}'
    return None


class ReadingListHTML(HTMLParser):
    """Series cards and supplied pager links from one served reading-list page.

    The same rules as the desktop _series_cards: a card is a link to the site's own
    /series/<slug> page, carrying one distinct tiara series ID from the link or its nearest
    ancestors (or none), with the link text trimmed as its title. Rendered-visibility and
    disabled-state checks have no equivalent over served HTML; the absence of a supplied
    adjacent Next link is the boundary, and ambiguity stays an unresolved identity.
    """

    VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                      'meta', 'param', 'source', 'track', 'wbr'))

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.cards = []
        self.page_links = []
        self._stack = []
        self._card = None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        attributes = {key: value for key, value in attrs if isinstance(key, str)}
        self._stack.append((tag, attributes))
        if tag != 'a':
            return
        href = attributes.get('href')
        if not isinstance(href, str):
            return
        url = urljoin(self.page_url, href)
        location = reading_list_location(url)
        if location is not None:
            self.page_links.append(location)
        if self._card is not None:
            return  # A nested anchor inside a card is not the card's own link.
        series_url = _series_path_url(url)
        if series_url is None:
            return
        # One distinct tiara series ID across the link and its four nearest ancestors,
        # as the rendered-DOM walk collects; anything else leaves the identity unresolved.
        ids = []
        for _tag, frame in self._stack[-5:]:
            value = frame.get('data-tiara-event-meta-series-id')
            if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value):
                ids.append(value)
        self._card = {'source_series_id': ids[0] if len(set(ids)) == 1 else None,
                      'title': [], 'source_url': series_url}

    def handle_data(self, data):
        if self._card is not None:
            self._card['title'].append(data)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break
        if tag == 'a' and self._card is not None:
            card, self._card = self._card, None
            self.cards.append({'source_series_id': card['source_series_id'],
                               'title': ''.join(card['title']).strip()[:200],
                               'source_url': card['source_url']})


class AndroidTapasAdapter:
    """The Tapas worker adapter for Android: TapasHTTPAdapter's whole flow over the live store.

    Construct with the native seam in any of its forms — the WebViewCookieStore bridge
    (headerFor/apply), the live cookie_source/cookie_sink callables the worker host hands
    over, or an already-built Transport (AndroidTransport included). Everything else is the
    core's: jobs.archive_discovered and jobs.sync see the adapter surface they already call.
    """

    supports_novels = True
    supports_expiring_access = True
    supports_series_metadata = True
    supports_comic_series_classification = True
    supports_library_checkpoint = True

    def __init__(self, cookie_source=None, cookie_sink=None, transport=None, interval=1.0,
                 expected_account_id=None, network_guard=None):
        if transport is None:
            if callable(getattr(cookie_source, 'headerFor', None)):
                transport = AndroidTransport(cookie_source, interval=interval,
                                             network_guard=network_guard)
            elif callable(cookie_source):
                transport = Transport(cookies=(), interval=interval, network_guard=network_guard,
                                      cookie_source=cookie_source, cookie_sink=cookie_sink)
            else:
                raise ArchiveError('The Android reader needs the app sign-in store or its live transport.')
        if not isinstance(transport, Transport):
            raise ArchiveError('The Android reader needs the canonical live transport.')
        self.transport = transport
        self.deadline = self.cancel_check = self.asset_metrics = None
        self.expected_account_id = expected_account_id
        from .adapters.tapas_http import TapasHTTPAdapter
        self._http = TapasHTTPAdapter(transport=transport, expected_account_id=expected_account_id)

    def _sync_state(self):
        """Carry the job's runtime limits to the one transport before any delegated call."""
        for key in ('deadline', 'cancel_check'):
            setattr(self.transport, key, getattr(self, key, None))
        metrics = getattr(self, 'asset_metrics', None)
        if metrics is not None:
            self.transport.metrics = metrics
        self._http.expected_account_id = getattr(self, 'expected_account_id', None)

    def __enter__(self):
        # Fail closed: nothing runs until the core's own who-am-i verifies the account.
        self._sync_state()
        self._http.check_session()
        return self

    def __exit__(self, *args):
        self.transport.close()

    # -- The jobs.archive_discovered surface, all delegated to the core's own flow. --

    def account_identity(self):
        self._sync_state()
        return self._http.account_identity()

    def episode_url(self, key):
        return self._http.episode_url(key)

    def episode_access(self, url):
        self._sync_state()
        return self._http.episode_access(url)

    def capture_automatic(self, url, series_id=None, series_title=None, title=None, order=None):
        self._sync_state()
        manifest = self._http.capture(url, series_id, series_title, title, order)
        episode = manifest.episodes[0]
        content = 'novel chapter text' if episode.content_type == 'novel' else f'{len(episode.pages)} page URLs'
        print(f"Episode {episode.id}: verified access; captured {content} without scrolling.", flush=True)
        return manifest

    def manifest(self):
        return self._http.manifest()

    def retrieve(self, page, max_bytes=None):
        self._sync_state()
        return self._http.retrieve(page, max_bytes=max_bytes)

    def asset_request(self, page, max_bytes=None):
        return self._http.asset_request(page, max_bytes=max_bytes)

    def refresh(self, episode):
        self._sync_state()
        return self._http.refresh(episode)

    def prefetch_episode(self, url):
        """Accepted so lookahead-enabled jobs validate; the single HTTP reader has nothing to prepare."""
        return None

    def series_metadata(self, key):
        self._sync_state()
        return self._http.series_metadata(key)

    def series_identity(self, key):
        self._sync_state()
        return self._http.series_identity(key)

    def retrieve_cover(self, series, max_bytes=None):
        self._sync_state()
        return self._http.retrieve_cover(series, max_bytes=max_bytes)

    def inspect_series(self, key, strict_order=True, comic_only=False):
        self._sync_state()
        return self._http.inspect_series(key, strict_order=strict_order, comic_only=comic_only)

    def inspect_transaction_history(self, **options):
        raise ArchiveError('Account transaction history is a desktop-browser flow; '
                           'the Android worker cannot read it.')

    # -- Library discovery over the site's own verified reading-list pages. --

    def inspect_library(self, details=False, category='subscribed', on_page=None):
        if details:
            raise ArchiveError('Detailed library research is a desktop-browser flow; '
                               'the Android worker lists the reading list only.')
        if category == 'free_episodes':
            self._sync_state()
            self._http.check_session()
            return {'authenticated': True, 'membership_verified': True, 'scope': 'free_episodes',
                    'complete': False, 'account_inventory_complete': False, 'pages': [], 'series': [],
                    'boundary_observed': False, 'restore_succeeded': None,
                    'reader_or_comic_pages_requested': False, 'duplicate_series_on_later_pages': 0,
                    'blocker': 'The Free Episodes filter is a visible browser control; '
                               'the Android worker did not request it.'}
        return self.inspect_library_pages(category, 200, on_page=on_page)

    def inspect_library_pages(self, category='subscribed', max_pages=3, on_page=None):
        """Follow the site's own supplied reading-list pages; list membership grants no access."""
        if type(max_pages) is not int or not 1 <= max_pages <= 200:
            raise ArchiveError('Reading-list page limit must be from 1 to 200.')
        if category not in HTTP_CATEGORIES:
            raise ArchiveError('The Android worker reads the subscribed, recent or updated list.')
        self._sync_state()
        self._http.check_session()
        result = {'authenticated': True, 'membership_verified': True, 'scope': category,
                  'complete': False, 'account_inventory_complete': False, 'pages': [], 'series': [],
                  'boundary_observed': False, 'restore_succeeded': None,
                  'reader_or_comic_pages_requested': False, 'duplicate_series_on_later_pages': 0,
                  'blocker': 'The reading-list page budget was reached.'}
        found = {}
        identities = set()
        fingerprints = set()
        for index in range(max_pages):
            page = index + 1
            url = f'https://tapas.io/reading-list?category={category.upper()}&pageNumber={page}'
            location = reading_list_location(url)
            if location is None or location['page'] != page or location['category'] != category:
                result['blocker'] = 'The constructed reading-list page failed its own scope validation; no request was made.'
                break
            raw = self.transport.get(url)
            if len(raw) > 8 * 1024 * 1024:
                result['blocker'] = 'Reading-list HTML exceeds the supported size; retained pages are partial.'
                break
            try:
                html = raw.decode('utf-8')
            except UnicodeError:
                result['blocker'] = 'Reading-list HTML has an unsupported encoding; retained pages are partial.'
                break
            parser = ReadingListHTML(url)
            parser.feed(html)
            parser.close()
            candidates = {card['source_url']: card for card in parser.cards}
            signature = tuple(sorted(candidates))
            if not signature or signature in fingerprints:
                result['blocker'] = 'The reading list is empty or repeats a prior page; completeness remains unverified.'
                break
            # A series seen on an earlier page means the list shifted while it was read and may have skipped one.
            result['duplicate_series_on_later_pages'] += sum(
                series_url in found or card['source_series_id'] in identities
                for series_url, card in candidates.items())
            fingerprints.add(signature)
            found.update(candidates)
            identities.update(card['source_series_id'] for card in candidates.values()
                              if card['source_series_id'])
            result['pages'].append({'page': page, 'series_count': len(candidates)})
            if on_page is not None:
                on_page({'authenticated': True, 'membership_verified': True, 'scope': category,
                         'complete': False, 'series': list(candidates.values()),
                         'pages': list(result['pages'])})
            adjacent = next((link for link in parser.page_links
                             if link['page'] == page + 1 and link['category'] == category), None)
            if adjacent is None:
                result['boundary_observed'] = True
                if result['duplicate_series_on_later_pages']:
                    result['blocker'] = ('The boundary was reached with series repeated across pages; '
                                         'the list may have shifted and skipped one.')
                elif any(card['source_series_id'] is None for card in found.values()):
                    result['blocker'] = 'Some listed series have no single verified series ID; the list is not complete.'
                else:
                    result.update(complete=True,
                                  blocker="Reading-list boundary observed over the site's paged HTML; "
                                          'purchase/unlock reconciliation remains unverified.')
                break
            if index + 1 >= max_pages:
                break
        result['series'] = list(found.values())
        result['observed_series_count'] = len(found)
        result['unresolved_series_id_count'] = sum(card['source_series_id'] is None for card in found.values())
        return result


def signed_in_probe(cookie_source=None, cookie_sink=None, transport=None, expected_account_id=None,
                    network_guard=None):
    """A public-safe signed-in answer for the app's dispatch: the verified who-am-i decides.

    Cookie presence is never sufficient; only the core's authenticated who-am-i through the
    live transport reports True. The result carries fixed words only — no account ID, name,
    URL or cookie ever leaves this call or is written anywhere.
    """
    try:
        with AndroidTapasAdapter(cookie_source=cookie_source, cookie_sink=cookie_sink,
                                 transport=transport, expected_account_id=expected_account_id,
                                 network_guard=network_guard):
            return {'signed_in': True, 'verified': True, 'reason': None}
    except AuthenticationError:
        return {'signed_in': False, 'verified': False, 'reason': 'not_signed_in'}
    except ArchiveError:
        return {'signed_in': False, 'verified': False, 'reason': 'unavailable'}
