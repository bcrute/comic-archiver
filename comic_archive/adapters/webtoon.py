"""Webtoon: what its website shows the signed-in account, read over HTTPS with the saved sign-in's cookies.

Only GET requests are made, to Webtoon's own pages, its episode list and its image host. Nothing is bought,
unlocked or changed. The website shows only part of many series; what it does not show is not saved.

Webtoon numbers a series' episodes from 1 within that series, and keeps its Canvas series at other addresses
than its Originals. The library needs one number per series and one per episode, so:
- a series is its title number, plus CANVAS for a Canvas series;
- an episode is its series' number times EPISODES, plus its number in the series.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import sites
from ..errors import AuthenticationError, MetadataError
from ..models import ArchiveError, Episode, Manifest, Page, Series
from ..session import load_session
from ..transport import Transport, asset_url
from .site_reader import SiteReader

SITE = sites.named('webtoon')
HOME = SITE.home
LIST_API = 'https://m.webtoons.com/api/v1/{kind}/{title}/episodes'
# What Webtoon's own pages ask to learn who is signed in.
USER_INFO = '/en/member/userInfo'
# The account's subscriptions, all on one page.
SUBSCRIPTIONS = '/en/favorite'
CANVAS = 10 ** 12
EPISODES = 10 ** 6
PAGE_SIZE = 100
# A series of 1000 pages of 100 episodes is far beyond any on the site; a list that long has gone wrong.
MAX_LIST_PAGES = 1000
MAX_DOCUMENT = 4 * 1024 * 1024
# Webtoon's own reader path: /<language>/<genre or canvas>/<series>/<episode>/viewer, and the series list beside it.
VIEWER = re.compile(r'/[a-z]{2}(?:-[A-Za-z]{2,4})?/([a-z0-9-]{1,60})/([A-Za-z0-9_-]{1,100})/[A-Za-z0-9_%-]{1,200}/viewer')
LIST = re.compile(r'/[a-z]{2}(?:-[A-Za-z]{2,4})?/([a-z0-9-]{1,60})/([A-Za-z0-9_-]{1,100})/list')


def account(info):
    """The signed-in account's sign-in name in Webtoon's answer about the visitor, or None for a visitor."""
    try:
        signed, key = info['loginUser'], info['loginId']
    except (TypeError, KeyError):
        return None
    return key if signed is True and isinstance(key, str) and key and len(key) <= 200 and key == key.strip() else None


def series_key(title_no, canvas):
    if type(title_no) is not int or not 0 < title_no < CANVAS:
        raise MetadataError('Webtoon gave a series number outside the supported range.')
    return str(title_no + CANVAS if canvas else title_no)


def split_series(key):
    """(title number, whether it is a Canvas series) of a library series number."""
    number = int(key) if isinstance(key, str) and re.fullmatch(r'[1-9][0-9]{0,12}', key) else 0
    if not 0 < number < 2 * CANVAS or number == CANVAS:
        raise ArchiveError('This is not a Webtoon series number.')
    return (number - CANVAS, True) if number > CANVAS else (number, False)


def episode_key(series, episode_no):
    if type(episode_no) is not int or not 0 < episode_no < EPISODES:
        raise MetadataError('Webtoon gave an episode number outside the supported range.')
    return str(int(series) * EPISODES + episode_no)


def split_episode(key):
    """(library series number, episode number in the series) of a library episode number."""
    if not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{6,19}', key):
        raise ArchiveError('This is not a Webtoon episode number.')
    series, episode_no = divmod(int(key), EPISODES)
    if episode_no == 0:
        raise ArchiveError('This is not a Webtoon episode number.')
    split_series(str(series))
    return str(series), episode_no


def numbers(url):
    """(title number, episode number or None) named by a Webtoon page address, which must be exactly one of each."""
    try:
        query = parse_qs(urlsplit(url).query, strict_parsing=True)
    except ValueError:
        raise MetadataError('Webtoon gave a page address without its series number.') from None
    found = []
    for name in ('title_no', 'episode_no'):
        values = query.get(name, [])
        if len(values) > 1 or (values and not re.fullmatch(r'[1-9][0-9]{0,11}', values[0])):
            raise MetadataError('Webtoon gave a page address with unexpected numbers.')
        found.append(int(values[0]) if values else None)
    if found[0] is None:
        raise MetadataError('Webtoon gave a page address without its series number.')
    return tuple(found)


def original(url):
    """An image's own file. Webtoon's '?type=opti' copies have fewer colours; the original is saved instead."""
    parts = urlsplit(url)
    return f'https://{parts.netloc}{parts.path}'


class Document(HTMLParser):
    """What the app reads from a Webtoon series page or episode page; everything else is ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.links, self.images = {}, {}, []
        self.series_link = None
        self.texts = {'episode': [], 'title': [], 'authors': [], 'summary': []}
        # The subscriptions page's list: each series' (page address, [its name's text]); None without one.
        self.subscriptions = None
        self._open = []  # (tag, which text it collects, or None)

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or '' for name, value in attrs}
        classes = set(attrs.get('class', '').split())
        if tag == 'meta' and attrs.get('property', '').startswith('og:'):
            self.meta.setdefault(attrs['property'], attrs.get('content', ''))
        elif tag == 'link' and attrs.get('rel') == 'canonical':
            self.links.setdefault('canonical', attrs.get('href', ''))
        elif tag == 'img' and '_images' in classes:
            self.images.append(attrs.get('data-url', ''))
        elif tag == 'a' and 'subj' in classes and self.series_link is None:
            self.series_link = (attrs.get('href', ''), attrs.get('title', ''))
        elif tag == 'a' and 'link' in classes and self._within('my_list'):
            self.subscriptions.append((attrs.get('href', ''), []))
        if tag in {'meta', 'link', 'img', 'br', 'input'}:
            return
        collects = None
        if tag == 'ul' and 'my_list' in classes and self.subscriptions is None:
            self.subscriptions, collects = [], 'my_list'
        elif tag == 'p' and 'subj' in classes and self.subscriptions and self._within('my_list'):
            collects = 'subscription'
        elif tag == 'h1' and 'subj_episode' in classes:
            collects = 'episode'
        elif tag in {'h1', 'h3'} and 'subj' in classes:
            collects = 'title'  # An Original's page has it in h1, a Canvas series' in h3.
        elif tag == 'div' and 'author_area' in classes:
            collects = 'authors'
        elif tag == 'p' and 'summary' in classes:
            collects = 'summary'
        elif tag == 'button':
            collects = 'ignored'  # The "author info" button inside the authors.
        self._open.append((tag, collects))

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                return

    def handle_data(self, data):
        collecting = next((kind for _, kind in reversed(self._open) if kind is not None), None)
        if collecting in self.texts:
            self.texts[collecting].append(data)
        elif collecting == 'subscription':
            self.subscriptions[-1][1].append(data)

    def _within(self, kind):
        return any(collects == kind for _, collects in self._open)

    def text(self, kind):
        return ' '.join(' '.join(self.texts[kind]).split())


def parsed(data):
    if len(data) > MAX_DOCUMENT:
        raise MetadataError('A Webtoon page is larger than any the app expects.')
    try:
        document = Document()
        document.feed(data.decode('utf-8'))
        document.close()
    except UnicodeError:
        raise MetadataError('A Webtoon page has an unexpected encoding.') from None
    return document


class WebtoonAdapter(SiteReader):
    site = SITE

    def __init__(self, session: Path | None = None, preferred=None, headless=True, interval=1.0, transport=None):
        self.session = session
        if transport is None:
            state = load_session(session)
            from ..network import NetworkGuard
            transport = Transport(state['cookies'], interval=interval, site=SITE,
                                  network_guard=NetworkGuard(Path(session).absolute().parent / 'Library'))
        self.transport = transport
        # Where each episode's page is, as Webtoon names it, learnt from the series' episode list.
        self.viewers = {}
        self._account = None

    def account_identity(self):
        """The signed-in account, as Webtoon's own pages name it. Once one is seen, it must stay the same: a
        sign-in that has run out would otherwise read on as a visitor."""
        data = self.transport.get(HOME + USER_INFO)
        try:
            named = account(json.loads(data))
        except ValueError:
            raise MetadataError('Webtoon gave an unexpected answer about who is signed in.') from None
        if named is None or self._account is not None and named != self._account:
            raise AuthenticationError('Sign in to Webtoon again.')
        self._account = named
        return named

    def signed_in(self):
        """Whether the account's own evidence is available.

        Access marks are earned from evidence, never assumed: an unsigned-in reader reports
        episodes as listed-but-unverified and refuses captures, instead of asserting they are free.
        """
        try:
            self.account_identity()
        except AuthenticationError:
            return False
        return True

    def inspect_library(self, details=False, category='subscribed', on_page=None):
        """The series the account subscribes to, from its own subscriptions page."""
        if category not in SITE.lists:
            raise ArchiveError('Webtoon has no such list.')
        self.account_identity()
        page = parsed(self.transport.get(HOME + SUBSCRIPTIONS))
        if page.subscriptions is None:
            raise MetadataError('Webtoon gave a subscriptions page without its list.')
        found = {}
        for link, text in page.subscriptions:
            parts, title = urlsplit(link), ' '.join(' '.join(text).split())
            match = LIST.fullmatch(parts.path)
            if (parts.scheme, parts.netloc) != ('https', SITE.host) or match is None or not title:
                raise MetadataError('Webtoon gave a subscriptions list of an unexpected shape.')
            title_no, episode_no = numbers(link)
            if episode_no is not None:
                raise MetadataError('Webtoon gave a subscriptions list of an unexpected shape.')
            key = series_key(title_no, match[1] == 'canvas')
            found.setdefault(key, {'source_series_id': key, 'title': title})
        return {'authenticated': True, 'membership_verified': True, 'scope': category, 'complete': True,
                'boundary_observed': True, 'series': list(found.values())}

    def episode_list(self, key):
        """Every episode the website shows in a series, in Webtoon's order: (number, title, page address)."""
        title_no, canvas = split_series(key)
        found, cursor = [], None
        for _ in range(MAX_LIST_PAGES):
            query = {'pageSize': PAGE_SIZE} | ({'cursor': cursor} if cursor is not None else {})
            data = self.transport.get(LIST_API.format(kind='canvas' if canvas else 'webtoon', title=title_no) + '?' + urlencode(query))
            try:
                result = json.loads(data)['result']
                episodes, after = result['episodeList'], result['nextCursor']
                if not isinstance(episodes, list) or type(after) is not int:
                    raise TypeError
                for item in episodes:
                    number, title, link = item['episodeNo'], item['episodeTitle'], item['viewerLink']
                    if not isinstance(title, str) or not title.strip() or not isinstance(link, str):
                        raise TypeError
                    found.append((number, title.strip(), link))
            except (ValueError, KeyError, TypeError):
                raise MetadataError('Webtoon gave an episode list of an unexpected shape.') from None
            if after == 0:
                break
            if cursor is not None and after <= cursor or not episodes:
                raise MetadataError('Webtoon gave an episode list that does not move on.')
            cursor = after
        else:
            raise MetadataError('Webtoon gave an episode list longer than any the app expects.')
        numbers_seen = [number for number, _, _ in found]
        if len(set(numbers_seen)) != len(numbers_seen):
            raise MetadataError('Webtoon listed an episode twice.')
        for number, _, link in found:
            self.viewers[episode_key(key, number)] = self.viewer(link, title_no, number)
        return found

    @staticmethod
    def viewer(link, title_no, episode_no):
        """An episode page address as Webtoon gave it, checked to be that episode on Webtoon's own site."""
        parts = urlsplit(link)
        elsewhere = (parts.scheme or parts.netloc) and (parts.scheme, parts.netloc) != ('https', SITE.host)
        if elsewhere or not VIEWER.fullmatch(parts.path):
            raise MetadataError('Webtoon gave an episode page somewhere unexpected.')
        if numbers(link) != (title_no, episode_no):
            raise MetadataError('Webtoon gave an episode page for a different episode.')
        return HOME + parts.path + '?' + parts.query

    def inspect_series(self, key, strict_order=False, comic_only=True):
        episodes = self.episode_list(key)
        if not episodes:
            error = MetadataError('Webtoon shows no episodes of this series.')
            error.source_http_status, error.source_series_id, error.source_route = 404, key, f'/series/{key}'
            raise error
        title = self.series_page(key).text('title')
        if not title:
            raise MetadataError('Webtoon gave a series page without its title.')
        # Without an account there is no evidence any listed episode may be read: the list is
        # reported as enumerated but unverified, and the importer refuses unauthenticated series.
        verified = self.signed_in()
        listed = [{'id': episode_key(key, number), 'title': name, 'order': number, 'source_scene': None,
                   'access_state': SITE.free if verified else None,
                   'authorization_reason': None if verified else 'webtoon_access_unverified',
                   'access_expires_at': None,
                   'source_url': self.viewers[episode_key(key, number)]} for number, name, _ in episodes]
        return {'complete': True, 'authenticated': verified, 'source_series_id': key, 'title': title,
                'episode_count': len(listed), 'ordering_verified': True,
                'accessible_episode_count': len(listed) if verified else 0,
                'series_content_flags': {'comic': True, 'book': False}, 'episodes': listed}

    def series_page(self, key):
        """The series' own page, found through its first listed episode: Webtoon redirects any other address."""
        title_no, _ = split_series(key)
        links = [url for episode, url in self.viewers.items() if split_episode(episode)[0] == key] or [
            self.viewer(link, title_no, number) for number, _, link in self.episode_list(key)]
        if not links:
            raise MetadataError('Webtoon shows no episodes of this series.')
        language, genre, slug = urlsplit(links[0]).path.split('/')[1:4]
        url = f'{HOME}/{language}/{genre}/{slug}/list?title_no={title_no}'
        return parsed(self.transport.get(url))

    def series_metadata(self, key):
        page = self.series_page(key)
        title = page.text('title')
        canonical = page.meta.get('og:url', '')
        match = LIST.fullmatch(urlsplit(canonical).path)
        if not title or match is None or numbers(canonical) != (split_series(key)[0], None):
            raise MetadataError('Webtoon gave a series page for a different series.')
        authors = tuple(dict.fromkeys(name.strip() for name in page.text('authors').split(',') if name.strip()))[:20]
        cover = page.meta.get('og:image') or None
        if cover is not None:
            cover = original(cover)
            asset_url(cover, SITE)
        return Series(self.library_id(key, match[2]), title, authors, page.text('summary') or None,
                      f'{HOME}{urlsplit(canonical).path}?title_no={split_series(key)[0]}', SITE.key, key, match[2],
                      cover_url=cover)

    @staticmethod
    def library_id(key, slug):
        """The series' name in the library: its Webtoon name, kept apart for Canvas series."""
        return ('canvas-' + slug if split_series(key)[1] else slug)[:100]

    def episode_url(self, key):
        series, _ = split_episode(key)
        if key not in self.viewers:
            self.episode_list(series)
        if key not in self.viewers:
            raise MetadataError('Webtoon no longer shows this episode.')
        return self.viewers[key]

    def capture_automatic(self, url):
        # Like every reader, capture needs an account whose evidence it can check first.
        if not self.signed_in():
            raise AuthenticationError('Sign in to Webtoon again.')
        title_no, episode_no = numbers(url)
        path = urlsplit(url).path
        match = VIEWER.fullmatch(path)
        if not url.startswith(HOME + '/') or match is None or episode_no is None:
            raise ArchiveError('This is not a Webtoon episode page.')
        canvas = match[1] == 'canvas'
        key = series_key(title_no, canvas)
        page = parsed(self.transport.get(url))
        canonical = page.links.get('canonical', '')
        if numbers(canonical) != (title_no, episode_no) or VIEWER.fullmatch(urlsplit(canonical).path) is None:
            raise MetadataError('Webtoon showed a different episode than the one asked for.')
        pages = tuple(Page(order, asset_url(original(image), SITE), False) for order, image in enumerate(page.images, 1))
        series_link, series_title = page.series_link or ('', '')
        title = page.text('episode')
        if (not series_title.strip() or not title or not series_link.startswith(HOME + '/')
                or LIST.fullmatch(urlsplit(series_link).path) is None or numbers(series_link) != (title_no, None)):
            raise MetadataError('Webtoon showed an episode page without its titles.')
        series = Series(self.library_id(key, match[2]), series_title.strip(), source_url=original(series_link) + f'?title_no={title_no}',
                        source=SITE.key, platform_id=key, slug=match[2])
        episode = Episode(episode_key(key, episode_no), series.id, title, episode_no, pages, SITE.free, url)
        self._manifest = Manifest(series, (episode,))
        return self._manifest
