"""Tappytoon: what its website shows, read over HTTPS from the same API its pages read.

Only GET requests are made, to Tappytoon's own pages, its API and its picture hosts. Nothing is bought,
unlocked, rented or changed. Signed in, the same pages and API answer as the account: its lists, and the
chapters it bought or rents. The pages are plain picture files: the website lays an invisible blank picture
over each one, and the app reads the real ones the way the website's reader does, undoing nothing.

Each page Tappytoon sends carries the key its own reader uses for the API; the app reads it from there and
never writes it anywhere. The pictures of a chapter are given only with the cookies that the chapter's page
list hands out, and the app keeps those for that chapter's pictures, as a browser does.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .. import sites
from ..errors import AccessDenied, AuthenticationError, MetadataError, UnsupportedContent
from ..expiry import expiration
from ..models import ArchiveError, Episode, Manifest, Page, Series
from ..session import load_session
from ..transport import ExpiredAsset, Transport, asset_url
from .site_reader import SiteReader

SITE = sites.named('tappytoon')
HOME = SITE.home
API = 'https://api-global.tappytoon.com'
CHAPTER = re.compile(r'/en/chapters/([1-9][0-9]{0,19})')
PAGE_DATA = re.compile(rb'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)
# A page that is the same for everyone and is not sent elsewhere: the start pages redirect by language.
KEY_PAGE = HOME + '/en/comics/discover'
KEY = re.compile(r'Bearer [A-Za-z0-9._~+/=-]{20,4000}')
SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,99}')
# The picture quality Tappytoon's own reader asks for.
VARIANT = 'high'
# What Tappytoon's page list answers for a chapter the account cannot read, even when the chapter said it could.
NO_LICENSE = b'does not have the license'
# A series of 100 pages of 100 chapters is far beyond any on the site; a list that long has gone wrong.
MAX_LIST_PAGES = 100
MAX_DOCUMENT = 8 * 1024 * 1024
# The account's own lists, as its library page reads them: where each is, and which field names the series.
LISTS = {'unlocked': ('/me/unlocked-books', {'category': 'comic', 'sort': 'unlocked_date', 'locale': 'en'}, 'bookId'),
         'subscribed': ('/comics', {'filter': 'favorites', 'category': 'comic', 'sort': 'subscribed_date:desc',
                                    'locale': 'en'}, 'id'),
         'recent': ('/comics', {'filter': 'visits', 'category': 'comic', 'locale': 'en'}, 'id')}


def number(value, low=1):
    if type(value) is not int or not low <= value < 10 ** 20:
        raise TypeError
    return value


def account(state):
    """The signed-in account's number in Tappytoon's page data, or None for a visitor."""
    try:
        key = state['oauth2']['user']['id']
    except (TypeError, KeyError):
        return None
    return str(key) if type(key) is int and 0 < key < 10 ** 20 else None


def access(chapter):
    """The access mark of a chapter as Tappytoon describes it to this reader."""
    if chapter['isAccessible'] and chapter['isFree']:
        return SITE.free
    if chapter['isAccessible'] and (chapter['isUserUnlocked'] or chapter['isUserRented']):
        return SITE.unlocked
    return 'unavailable_or_unverified'


def rental_end(chapter):
    """When a rented chapter can no longer be read; None for one that is free or kept."""
    if access(chapter) != SITE.unlocked or chapter['isUserUnlocked']:
        return None
    if not isinstance(chapter.get('rentalExpiredAt'), str):
        raise MetadataError('Tappytoon gave a rented chapter without the end of its rental.')
    return expiration(chapter['rentalExpiredAt'])


def listed_access(chapter):
    """(access mark, why it is not readable, end of rental) of a listed chapter. A rental without a
    readable end is listed as unverified, so one odd chapter does not stop the series' whole list."""
    try:
        return access(chapter), None, rental_end(chapter)
    except (MetadataError, UnsupportedContent):
        return 'unavailable_or_unverified', 'temporary_access', None


def checked_chapter(item, comic=None):
    """A chapter as Tappytoon's API gives it, with the fields the app reads checked."""
    try:
        number(item['id']), number(item['comicId']), number(item['order'], low=0)
        if comic is not None and item['comicId'] != comic:
            raise TypeError
        if not isinstance(item['title'], str) or not item['title'].strip():
            raise TypeError
        if any(type(item[name]) is not bool for name in ('isFree', 'isAccessible', 'isUserUnlocked', 'isUserRented')):
            raise TypeError
    except (KeyError, TypeError):
        raise MetadataError('Tappytoon gave a chapter of an unexpected shape.') from None
    return item


class TappytoonAdapter(SiteReader):
    site = SITE

    def __init__(self, session: Path | None = None, preferred=None, headless=True, interval=1.0, transport=None):
        self.session = session
        if transport is None:
            state = load_session(session)
            from ..network import NetworkGuard
            transport = Transport(state['cookies'], interval=interval, site=SITE,
                                  network_guard=NetworkGuard(Path(session).absolute().parent / 'Library'))
        self.transport = transport
        self._key = None
        self._account = None
        self._comics = {}

    def account_identity(self):
        """The number of the signed-in account, as the website's own page names it."""
        if self._key is None:
            self.key()
        if self._account is None:
            raise AuthenticationError('Sign in to Tappytoon again.')
        return self._account

    def inspect_library(self, details=False, category='subscribed', on_page=None):
        """The series in one of the account's own lists."""
        if category not in LISTS:
            raise ArchiveError('Tappytoon has no such list.')
        self.account_identity()
        path, query, field = LISTS[category]
        found = {}
        for item in self.listed(path, query):
            try:
                key, title = str(number(item[field])), item['title'].strip()
                if not title:
                    raise TypeError
            except (KeyError, TypeError, AttributeError):
                raise MetadataError('Tappytoon gave a list of an unexpected shape.') from None
            found.setdefault(key, {'source_series_id': key, 'title': title})
        return {'authenticated': True, 'membership_verified': True, 'scope': category, 'complete': True,
                'boundary_observed': True, 'series': list(found.values())}

    def key(self, renew=False):
        """The key Tappytoon's own reader uses for its API, read from the website's page data.

        The same data names the signed-in account. Once one is seen, the page must keep naming it: a sign-in
        that has run out would otherwise read on as a visitor, and find bought chapters locked.
        """
        if self._key is None or renew:
            data = self.transport.get(KEY_PAGE)
            found = PAGE_DATA.search(data) if len(data) <= MAX_DOCUMENT else None
            try:
                state = json.loads(found[1])['props']['initialState']
                key = state['axios']['headers']['Authorization']
            except (TypeError, ValueError, KeyError):
                state = key = None
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise MetadataError('Tappytoon sent its page without the key its reader uses.')
            named = account(state)
            if self._account is not None and named != self._account:
                raise AuthenticationError('Sign in to Tappytoon again.')
            self._key, self._account = key, named
        return self._key

    def api(self, path, query, take_cookies=False, address=None):
        """One answer from Tappytoon's API, as JSON. A key that has run out is renewed once."""
        url = address or f'{API}{path}?{urlencode(query)}'
        for renew in (False, True):
            try:
                data = self.transport.get(url, authorization=self.key(renew), take_cookies=take_cookies)
                break
            except ExpiredAsset:
                if renew:
                    raise
        try:
            return json.loads(data)
        except ValueError:
            raise MetadataError('Tappytoon gave an answer of an unexpected shape.') from None

    def comic(self, key):
        """A series' own details from Tappytoon, checked to be that series."""
        if key not in self._comics:
            try:
                value = self.api(f'/comics/{int(key)}', {'locale': 'en'})
            except ExpiredAsset as gone:
                if getattr(gone, 'source_http_status', None) not in {404, 410}:
                    raise
                # Tappytoon no longer has the series: the job sets it aside, as it does for other sites.
                error = MetadataError('Tappytoon no longer shows this series.')
                error.source_http_status, error.source_series_id, error.source_route = gone.source_http_status, key, f'/series/{key}'
                raise error from None
            if (not isinstance(value, dict) or value.get('id') != int(key) or not isinstance(value.get('title'), str)
                    or not value['title'].strip() or not isinstance(value.get('slug'), str) or not SLUG.fullmatch(value['slug'])
                    or value.get('category') not in {'comic', 'novel'}):
                raise MetadataError('Tappytoon gave a series of an unexpected shape.')
            self._comics[key] = value
        return self._comics[key]

    def listed(self, path, query):
        """Everything in one of Tappytoon's lists, following its pages."""
        found, address, asked = [], None, set()
        for _ in range(MAX_LIST_PAGES):
            value = self.api(path, query, address=address)
            asked.add(address or f'{API}{path}?{urlencode(query)}')
            if not isinstance(value, list):
                raise MetadataError('Tappytoon gave a list of an unexpected shape.')
            found += value
            following = re.search(r'<([^>]+)>\s*;\s*rel="next"', (self.transport.last_response or {}).get('link', ''))
            if following is None:
                break
            parts = urlsplit(following[1])
            if (parts.scheme, parts.netloc, parts.path) != ('https', urlsplit(API).netloc, path) or not value:
                raise MetadataError('Tappytoon gave a list that leads somewhere unexpected.')
            if following[1] in asked:
                raise MetadataError('Tappytoon gave a list that leads back to a page already read.')
            address = following[1]
        else:
            raise MetadataError('Tappytoon gave a list longer than any the app expects.')
        return found

    def chapters(self, key):
        """Every chapter Tappytoon lists in a series, in its order."""
        found = [checked_chapter(item, int(key)) for item in self.listed(
            f'/comics/{int(key)}/chapters', {'sort': 'asc', 'limit': 100, 'excludes': 'wait_until_free',
                                             'skipAgeRatingRestriction': 'true', 'locale': 'en'})]
        ids = [chapter['id'] for chapter in found]
        orders = [chapter['order'] for chapter in found]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise MetadataError('Tappytoon listed a chapter twice.')
        return found

    def inspect_series(self, key, strict_order=False, comic_only=True):
        comic = self.comic(key)
        if comic['category'] != 'comic':
            return {'complete': False, 'authenticated': True, 'source_series_id': key, 'title': comic['title'],
                    'episodes': [], 'episode_enumeration_skipped': True, 'unsupported_series_reason': 'novel',
                    'series_content_flags': {'comic': False, 'book': True}}
        # Tappytoon counts chapters from 0; the library counts from 1.
        listed = []
        for chapter in self.chapters(key):
            mark, reason, expires = listed_access(chapter)
            listed.append({'id': str(chapter['id']), 'title': chapter['title'].strip(), 'order': chapter['order'] + 1,
                           'source_scene': None, 'access_state': mark, 'authorization_reason': reason,
                           'access_expires_at': expires, 'source_url': self.episode_url(str(chapter['id']))})
        return {'complete': True, 'authenticated': True, 'source_series_id': key, 'title': comic['title'].strip(),
                'episode_count': len(listed), 'ordering_verified': True,
                'accessible_episode_count': sum(episode['access_state'] in sites.ACCESS for episode in listed),
                'series_content_flags': {'comic': True, 'book': False}, 'episodes': listed}

    def series_metadata(self, key):
        comic = self.comic(key)
        authors = comic.get('authors') if isinstance(comic.get('authors'), list) else []
        names = tuple(dict.fromkeys(author['name'].strip() for author in authors
                                    if isinstance(author, dict) and isinstance(author.get('name'), str) and author['name'].strip()))[:20]
        description = next((text.strip() for text in (comic.get('longDescription'), comic.get('description'))
                            if isinstance(text, str) and text.strip()), None)
        cover = comic.get('coverImageUrl') if isinstance(comic.get('coverImageUrl'), str) and comic['coverImageUrl'] else None
        if cover is not None:
            asset_url(cover, SITE)
        return Series(comic['slug'], comic['title'].strip(), names, description, f"{HOME}/en/book/{comic['slug']}",
                      SITE.key, key, comic['slug'], cover_url=cover)

    def retrieve_cover(self, series, max_bytes=None):
        try:
            return super().retrieve_cover(series, max_bytes)
        except ExpiredAsset:
            # The cover's address has run out: the series' details are asked for again for a fresh one.
            self._comics.pop(series.platform_id, None)
            raise

    def episode_url(self, key):
        return f'{HOME}/en/chapters/{int(key)}'

    def chapter(self, url):
        """(chapter, access mark, end of rental) of a chapter page the account may read; any other is refused."""
        parts = urlsplit(url)
        match = CHAPTER.fullmatch(parts.path)
        if (parts.scheme, parts.netloc, parts.query) != ('https', SITE.host, '') or match is None:
            raise ArchiveError('This is not a Tappytoon chapter page.')
        try:
            chapter = checked_chapter(self.api(f'/chapters/{match[1]}', {'locale': 'en'}))
        except ExpiredAsset as error:
            if getattr(error, 'source_http_status', None) not in {403, 404, 410}:
                raise
            raise AccessDenied('Tappytoon no longer gives this chapter to this account; it will not be read.') from None
        if chapter['id'] != int(match[1]):
            raise MetadataError('Tappytoon showed a different chapter than the one asked for.')
        mark = access(chapter)
        if mark not in sites.ACCESS:
            raise AccessDenied('This chapter is neither free, bought nor rented; it will not be read.')
        expires = rental_end(chapter)
        return chapter, mark, expires

    def episode_access(self, url):
        """Whether the account may read a chapter, from its details alone: none of its pictures are asked for."""
        chapter, mark, expires = self.chapter(url)
        return {'source_episode_id': str(chapter['id']), 'access_state': mark, 'access_expires_at': expires}

    def capture_automatic(self, url):
        chapter, mark, expires = self.chapter(url)
        key = str(chapter['comicId'])
        comic = self.comic(key)
        try:
            listing = self.api('/content-delivery/contents/manifest',
                               {'chapterId': chapter['id'], 'variant': VARIANT, 'locale': 'en', 'canAcceptSignedCookie': 'true'},
                               take_cookies=True)
        except ArchiveError as error:
            # Set aside like a locked chapter, so the rest of the download goes on; any other refusal stops it.
            if getattr(error, 'source_http_status', None) == 400 and NO_LICENSE in getattr(error, 'source_answer', b''):
                raise AccessDenied('Tappytoon did not give this chapter to this account; it will not be read.') from None
            raise
        try:
            if listing['mediaType'] != 'image':
                raise TypeError
            files = sorted(listing['files'], key=lambda item: number(item['sortKey'], low=0))
            locations = [asset_url(item['url'], SITE) for item in files]
            if not locations or len({item['sortKey'] for item in files}) != len(files):
                raise TypeError
        except (KeyError, TypeError):
            raise MetadataError('Tappytoon gave a chapter page list of an unexpected shape.') from None
        pages = tuple(Page(order, location, False) for order, location in enumerate(locations, 1))
        series = Series(comic['slug'], comic['title'].strip(), source_url=f"{HOME}/en/book/{comic['slug']}",
                        source=SITE.key, platform_id=key, slug=comic['slug'])
        episode = Episode(str(chapter['id']), series.id, chapter['title'].strip(), chapter['order'] + 1, pages, mark,
                          self.episode_url(str(chapter['id'])), access_expires_at=expires)
        self._manifest = Manifest(series, (episode,))
        return self._manifest
