"""Tappytoon's reader, against made-up answers built like Tappytoon's own (no real text, keys or pictures)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import httpx

from test_archive import png
from comic_archive import sites, worker
from comic_archive.adapters.tappytoon import KEY_PAGE, LISTS, TappytoonAdapter
from comic_archive.archive import verify
from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError
from comic_archive.jobs import sync
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport

API = 'https://api-global.tappytoon.com'
CDN = 'https://content-repository-cdn.tappytoon.com/67'
KEY = 'Bearer ' + 'k' * 40
CHAPTERS = API + '/comics/67/chapters?sort=asc&limit=100&excludes=wait_until_free&skipAgeRatingRestriction=true&locale=en'
COMIC = {'id': 67, 'category': 'comic', 'title': 'Sample Series ', 'slug': 'sample-series', 'description': 'Short.',
         'longDescription': 'A story made up for tests.', 'authors': [{'id': 1, 'name': 'First Author'}, {'id': 2, 'name': 'Second Author'}],
         'coverImageUrl': 'https://image-repository-cdn.tappytoon.com/series/67/cover.jpg'}


# Where each of the account's lists is asked for.
LIST = {name: f'{API}{path}?{urlencode(query)}' for name, (path, query, _) in LISTS.items()}
RENTAL_END = '2099-11-01T10:00:00.000Z'


def page_data(key=KEY, account=None):
    """Tappytoon's page data: its reader's key, and the signed-in account if there is one."""
    data = {'props': {'initialState': {'axios': {'headers': {'Authorization': key}}, 'oauth2': {'user': {}}}}}
    if account is not None:
        data['props']['initialState']['oauth2']['user'] = {'id': account}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'.encode()


def chapter(number, free=True, unlocked=False, comic=67):
    return {'id': 900 + number, 'comicId': comic, 'order': number, 'title': f'Episode {number + 1}', 'isFree': free,
            'isAccessible': free or unlocked, 'isUserUnlocked': unlocked, 'isUserRented': False, 'rentalExpiredAt': None}


def manifest(number):
    return {'mediaType': 'image', 'files': [{'sortKey': key, 'width': 720, 'height': 1000, 'url': f'{CDN}/{number}/{key}.jpg'}
                                            for key in (1, 0)]}


class Answer:
    def __init__(self, data, code=200, headers=()):
        self.status_code, self.data, self.headers = code, data, httpx.Headers(list(headers))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_bytes(self):
        yield self.data


class Site:
    """A stand-in for Tappytoon's servers. The API answers only with the key; pictures only with the chapter's cookies."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def stream(self, method, url, headers):
        self.calls.append((url, headers))
        if url.startswith(API) and headers.get('Authorization') != KEY:
            return Answer(b'{}', 401)
        if url.startswith(CDN) and 'CloudFront-Signature=chapter' not in headers.get('Cookie', ''):
            return Answer(b'', 403)
        if url not in self.answers:
            raise AssertionError(f'unexpected request: {url}')
        answer = self.answers[url]
        return answer if isinstance(answer, Answer) else Answer(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    def close(self):
        pass


def cookies_for_chapter(number):
    return [('Set-Cookie', f'CloudFront-Signature=chapter{number}; Domain=tappytoon.com; Path=/; Secure; HttpOnly'),
            ('Set-Cookie', 'tracker=elsewhere; Domain=example.com; Path=/')]


def series_answers():
    """Two pages of chapter list: two free chapters, one locked, one the account unlocked; and each chapter's pictures."""
    following = CHAPTERS + '&cursor=abc'
    answers = {KEY_PAGE: page_data(), API + '/comics/67?locale=en': COMIC,
               COMIC['coverImageUrl']: png(200),
               CHAPTERS: Answer(json.dumps([chapter(0), chapter(1)]).encode(), headers=[('Link', f'<{following}>; rel="next"')]),
               following: [chapter(2, free=False), chapter(3, free=False, unlocked=True)]}
    for number in (0, 1, 2, 3):
        answers[f'{API}/chapters/{900 + number}?locale=en'] = chapter(number, free=number < 2, unlocked=number == 3)
        answers[f'{API}/content-delivery/contents/manifest?chapterId={900 + number}&variant=high&locale=en&canAcceptSignedCookie=true'] = \
            Answer(json.dumps(manifest(number)).encode(), headers=cookies_for_chapter(number))
        for key in (0, 1):
            answers[f'{CDN}/{number}/{key}.jpg'] = png(10 * number + key)
    return answers


# Tappytoon's page list for a chapter the account cannot read, as it answers.
NO_LICENSE = Answer(json.dumps({'type': 'about:blank', 'status': 400, 'code': 400, 'timestamp': '2026-10-14T00:00:00Z',
                                'message': 'User does not have the license of requested content'}).encode(), 400)


def reader(answers, adapter=TappytoonAdapter):
    client = Site(answers)
    return adapter(transport=Transport(client=client, site=sites.named('tappytoon'))), client


@patch('comic_archive.transport.time.sleep')
class TappytoonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    @patch('builtins.print')
    def test_the_free_and_unlocked_chapters_are_saved_with_their_pictures_in_order_and_the_locked_one_is_not(self, output, sleep):
        adapter, client = reader(series_answers() | {KEY_PAGE: page_data(account=5), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}]})
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901', '903'])
        self.assertEqual(verify(self.root), [])
        saved = []
        for path in self.root.glob('**/*.cbz'):
            with zipfile.ZipFile(path) as archive:
                saved.append([archive.read(name) for name in sorted(archive.namelist()) if not name.endswith(('.xml', '.json'))])
        self.assertEqual(sorted(saved), sorted([png(10 * number), png(10 * number + 1)] for number in (0, 1, 3)))
        self.assertNotIn(f'{API}/content-delivery/contents/manifest?chapterId=902&variant=high&locale=en&canAcceptSignedCookie=true',
                         [url for url, _ in client.calls])
        # The key goes to the API only; the chapter's cookies to Tappytoon's hosts only; nothing at all to the cover host.
        for url, headers in client.calls:
            self.assertEqual('Authorization' in headers, url.startswith(API), url)
            self.assertNotIn('elsewhere', headers.get('Cookie', ''))
            if url == COMIC['coverImageUrl']:
                self.assertNotIn('Cookie', headers)

    def test_series_details_and_chapter_list_come_from_tappytoons_api(self, sleep):
        adapter, _ = reader(series_answers())
        details = adapter.series_metadata('67')
        self.assertEqual((details.id, details.title, details.creators, details.description, details.source_url, details.platform_id),
                         ('sample-series', 'Sample Series', ('First Author', 'Second Author'), 'A story made up for tests.',
                          'https://www.tappytoon.com/en/book/sample-series', '67'))
        self.assertEqual(adapter.retrieve_cover(details), png(200))
        listing = adapter.inspect_series('67')
        self.assertEqual([(e['id'], e['order'], e['title'], e['access_state']) for e in listing['episodes']],
                         [('900', 1, 'Episode 1', 'tappytoon_free'), ('901', 2, 'Episode 2', 'tappytoon_free'),
                          ('902', 3, 'Episode 3', 'unavailable_or_unverified'), ('903', 4, 'Episode 4', 'tappytoon_unlocked')])
        self.assertEqual(listing['accessible_episode_count'], 3)
        adapter, _ = reader(series_answers() | {API + '/comics/67?locale=en': COMIC | {'id': 68}})
        with self.assertRaises(MetadataError):
            adapter.inspect_series('67')

    def test_a_novel_is_set_aside_like_tapas_novels_without_reading_its_chapters(self, sleep):
        adapter, client = reader({KEY_PAGE: page_data(), API + '/comics/67?locale=en': COMIC | {'category': 'novel'}})
        listing = adapter.inspect_series('67')
        self.assertEqual((listing['episode_enumeration_skipped'], listing['unsupported_series_reason'], listing['episodes']),
                         (True, 'novel', []))
        self.assertEqual(len(client.calls), 2)

    def test_a_key_that_has_run_out_is_renewed_once_and_then_the_job_stops(self, sleep):
        answers = series_answers()
        adapter, client = reader(answers)
        adapter._key = 'Bearer ' + 'x' * 40
        self.assertEqual(adapter.comic('67')['slug'], 'sample-series')
        self.assertEqual([url for url, _ in client.calls], [API + '/comics/67?locale=en', KEY_PAGE, API + '/comics/67?locale=en'])
        answers[KEY_PAGE] = page_data('Bearer ' + 'y' * 40)
        adapter, client = reader(answers)
        with self.assertRaises(ArchiveError):
            adapter.comic('67')
        self.assertEqual([url for url, _ in client.calls], [KEY_PAGE, API + '/comics/67?locale=en'] * 2)
        for broken in (b'<html></html>', page_data('Basic ' + 'k' * 40), page_data(None)):
            adapter, _ = reader({KEY_PAGE: broken})
            with self.subTest(broken=broken[-80:]), self.assertRaisesRegex(MetadataError, 'without the key'):
                adapter.comic('67')

    def test_a_chapter_list_that_is_wrong_stops_before_any_chapter_is_read(self, sleep):
        base = series_answers()
        broken = {
            'leads elsewhere': {CHAPTERS: Answer(json.dumps([chapter(0)]).encode(), headers=[('Link', f'<{API}/comics/68/chapters?cursor=x>; rel="next"')])},
            'other series': {CHAPTERS: [chapter(0, comic=68)]},
            'twice': {CHAPTERS: [chapter(0), chapter(0)]},
            'missing marks': {CHAPTERS: [{k: v for k, v in chapter(0).items() if k != 'isFree'}]},
            'other shape': {CHAPTERS: {'chapters': []}},
        }
        for name, change in broken.items():
            adapter, client = reader(base | change)
            with self.subTest(name), self.assertRaises(MetadataError):
                adapter.inspect_series('67')
            self.assertFalse(any('/chapters/9' in url or url.startswith(CDN) for url, _ in client.calls))

    def test_a_chapter_is_read_only_when_it_is_free_or_unlocked_and_its_pictures_are_tappytoons(self, sleep):
        base = series_answers()
        url = 'https://www.tappytoon.com/en/chapters/900'
        listing = f'{API}/content-delivery/contents/manifest?chapterId=900&variant=high&locale=en&canAcceptSignedCookie=true'
        wrong = {
            'locked': ({f'{API}/chapters/900?locale=en': chapter(0, free=False)}, AccessDenied),
            'another chapter': ({f'{API}/chapters/900?locale=en': chapter(1)}, MetadataError),
            'pictures elsewhere': ({listing: {'mediaType': 'image', 'files': [{'sortKey': 0, 'url': 'https://example.com/a.jpg'}]}}, ArchiveError),
            'no pictures': ({listing: {'mediaType': 'image', 'files': []}}, MetadataError),
            'same place twice': ({listing: {'mediaType': 'image', 'files': [{'sortKey': 0, 'url': f'{CDN}/a.jpg'}] * 2}}, MetadataError),
            'not pictures': ({listing: manifest(0) | {'mediaType': 'text'}}, MetadataError),
            'no license after all': ({listing: NO_LICENSE}, AccessDenied),
        }
        for name, (change, error) in wrong.items():
            adapter, _ = reader(base | change)
            with self.subTest(name), self.assertRaises(error):
                adapter.capture_automatic(url)
        # Any other refusal is not taken for a chapter the account cannot read: it stops, naming the answer.
        for answer in (Answer(b'{"code":400,"message":"Bad request"}', 400), Answer(b'', 302), Answer(NO_LICENSE.data, 409)):
            adapter, _ = reader(base | {listing: answer})
            with self.subTest(code=answer.status_code), self.assertRaisesRegex(ArchiveError, f'HTTP {answer.status_code}') as raised:
                adapter.capture_automatic(url)
            self.assertNotIsInstance(raised.exception, AccessDenied)
        # Pictures that run out while a chapter is saved are asked for again, but only if the chapter is unchanged.
        adapter, _ = reader(base)
        episode = adapter.capture_automatic(url).episodes[0]
        self.assertEqual(adapter.refresh(episode).pages, episode.pages)
        adapter.transport.client.answers[listing] = Answer(json.dumps(manifest(1)).encode(), headers=cookies_for_chapter(0))
        with self.assertRaisesRegex(ArchiveError, 'changed on Tappytoon'):
            adapter.refresh(episode)
        adapter, client = reader(base)
        for address in (url.replace('www.tappytoon.com', 'example.com'), url + '?x=1', url.replace('chapters', 'book'), 'http' + url[5:]):
            with self.subTest(address), self.assertRaises(ArchiveError):
                adapter.capture_automatic(address)
        self.assertEqual(client.calls, [])

    def test_the_account_is_the_one_tappytoons_own_page_names_and_must_stay_signed_in(self, sleep):
        adapter, client = reader({KEY_PAGE: page_data(account=123456789)})
        self.assertEqual(adapter.account_identity(), '123456789')
        self.assertEqual(adapter.key(), KEY)
        self.assertEqual(len(client.calls), 1)
        for visitor in (page_data(), page_data(account=0), page_data(account='5'), page_data(account=True)):
            adapter, _ = reader({KEY_PAGE: visitor})
            with self.subTest(visitor=visitor[-110:]), self.assertRaises(AuthenticationError):
                adapter.account_identity()
        # A renewed key is taken only while the page still names the same account.
        renewed = 'Bearer ' + 'y' * 40
        adapter, client = reader({KEY_PAGE: page_data(account=5)})
        adapter.account_identity()
        client.answers[KEY_PAGE] = page_data(renewed, account=5)
        self.assertEqual(adapter.key(renew=True), renewed)
        for later in (page_data(renewed), page_data(renewed, account=7)):
            client.answers[KEY_PAGE] = later
            with self.subTest(later=later[-110:]), self.assertRaises(AuthenticationError):
                adapter.key(renew=True)

    def test_the_accounts_bought_subscribed_and_recent_lists_name_their_series(self, sleep):
        following = LIST['subscribed'] + '&cursor=c'
        answers = {KEY_PAGE: page_data(account=5),
                   LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series '}, {'bookId': 68, 'title': 'Other Series'}],
                   LIST['subscribed']: Answer(json.dumps([{'id': 67, 'title': 'Sample Series'}]).encode(),
                                              headers=[('Link', f'<{following}>; rel="next"')]),
                   following: [{'id': 69, 'title': 'Third Series'}, {'id': 67, 'title': 'Sample Series'}],
                   LIST['recent']: []}
        adapter, _ = reader(answers)
        self.assertEqual(adapter.inspect_library(category='unlocked'),
                         {'authenticated': True, 'membership_verified': True, 'scope': 'unlocked', 'complete': True,
                          'boundary_observed': True, 'series': [{'source_series_id': '67', 'title': 'Sample Series'},
                                                                {'source_series_id': '68', 'title': 'Other Series'}]})
        self.assertEqual([s['source_series_id'] for s in adapter.inspect_library(category='subscribed')['series']], ['67', '69'])
        self.assertEqual(adapter.inspect_library(category='recent')['series'], [])
        # Go reads the bought list first.
        self.assertEqual(sites.named('tappytoon').lists, ('unlocked', 'subscribed', 'recent'))
        # The lists are the account's: a visitor gets none, and a list Tappytoon does not have is not asked for.
        adapter, client = reader(answers | {KEY_PAGE: page_data()})
        with self.assertRaises(AuthenticationError):
            adapter.inspect_library(category='unlocked')
        self.assertEqual([url for url, _ in client.calls], [KEY_PAGE])
        adapter, client = reader(answers)
        with self.assertRaises(ArchiveError):
            adapter.inspect_library(category='free_episodes')
        self.assertEqual(client.calls, [])
        for broken in ([{'id': 67, 'title': 'Sample Series'}], [{'bookId': '67', 'title': 'Sample Series'}],
                       [{'bookId': 67, 'title': ' '}], [{'bookId': 67}], {'books': []}):
            adapter, _ = reader(answers | {LIST['unlocked']: broken})
            with self.subTest(broken=broken), self.assertRaises(MetadataError):
                adapter.inspect_library(category='unlocked')

    @patch('builtins.print')
    def test_go_saves_what_the_account_can_read_from_its_bought_list(self, output, sleep):
        answers = series_answers() | {KEY_PAGE: page_data(account=5), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}]}
        adapter, _ = reader(answers)
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901', '903'])
        self.assertEqual(verify(self.root), [])

    @patch('builtins.print')
    def test_go_goes_on_past_a_chapter_tappytoon_then_does_not_give_to_the_account(self, output, sleep):
        refused = f'{API}/content-delivery/contents/manifest?chapterId=903&variant=high&locale=en&canAcceptSignedCookie=true'
        answers = series_answers() | {KEY_PAGE: page_data(account=5), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}],
                                      refused: NO_LICENSE}
        adapter, _ = reader(answers)
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual((sorted(result['archived_episode_ids']), result['unavailable_episode_ids']), (['900', '901'], ['903']))
        self.assertEqual(verify(self.root), [])

    def test_a_rented_chapter_is_read_with_the_end_of_its_rental_and_one_without_an_end_is_not(self, sleep):
        rented = chapter(2, free=False) | {'isAccessible': True, 'isUserRented': True, 'rentalExpiredAt': RENTAL_END}
        following = CHAPTERS + '&cursor=abc'
        answers = series_answers() | {f'{API}/chapters/902?locale=en': rented,
                                      following: [rented, chapter(3, free=False, unlocked=True)]}
        adapter, _ = reader(answers)
        listed = {e['id']: (e['access_state'], e['access_expires_at']) for e in adapter.inspect_series('67')['episodes']}
        self.assertEqual(listed, {'900': ('tappytoon_free', None), '901': ('tappytoon_free', None),
                                  '902': ('tappytoon_unlocked', '2099-11-01T10:00:00+00:00'), '903': ('tappytoon_unlocked', None)})
        episode = adapter.capture_automatic('https://www.tappytoon.com/en/chapters/902').episodes[0]
        self.assertEqual(episode.access_expires_at, '2099-11-01T10:00:00+00:00')
        for end in (None, 'soon'):
            adapter, client = reader(answers | {f'{API}/chapters/902?locale=en': rented | {'rentalExpiredAt': end}})
            with self.subTest(end=end), self.assertRaises(ArchiveError):
                adapter.capture_automatic('https://www.tappytoon.com/en/chapters/902')
            self.assertFalse(any('manifest' in url for url, _ in client.calls))

    def test_a_list_that_leads_back_to_a_page_already_read_stops_there(self, sleep):
        following = CHAPTERS + '&cursor=abc'
        for back in (CHAPTERS, following):
            answers = series_answers() | {following: Answer(json.dumps([chapter(2)]).encode(), headers=[('Link', f'<{back}>; rel="next"')])}
            adapter, client = reader(answers)
            with self.subTest(back=back), self.assertRaisesRegex(MetadataError, 'already read'):
                adapter.inspect_series('67')
            self.assertEqual([url for url, _ in client.calls if '/chapters' in url], [CHAPTERS, following])

    @patch('builtins.print')
    def test_a_rented_chapter_without_a_readable_end_is_listed_as_unverified_and_the_rest_of_the_series_is_saved(self, output, sleep):
        following = CHAPTERS + '&cursor=abc'
        for end in (None, 'soon', 5):
            rented = chapter(2, free=False) | {'isAccessible': True, 'isUserRented': True, 'rentalExpiredAt': end}
            adapter, _ = reader(series_answers() | {following: [rented, chapter(3, free=False, unlocked=True)]})
            listing = adapter.inspect_series('67')
            with self.subTest(end=end):
                self.assertEqual({e['id']: (e['access_state'], e['authorization_reason'], e['access_expires_at']) for e in listing['episodes']},
                                 {'900': ('tappytoon_free', None, None), '901': ('tappytoon_free', None, None),
                                  '902': ('unavailable_or_unverified', 'temporary_access', None),
                                  '903': ('tappytoon_unlocked', None, None)})
                self.assertEqual(listing['accessible_episode_count'], 3)
        answers = series_answers() | {KEY_PAGE: page_data(account=5), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}],
                                      following: [rented, chapter(3, free=False, unlocked=True)]}
        adapter, client = reader(answers)
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901', '903'])
        self.assertFalse(any('chapterId=902' in url for url, _ in client.calls))

    def test_a_rate_limit_met_in_one_job_stops_only_that_job_on_the_same_reader(self, sleep):
        adapter, client = reader({KEY_PAGE: Answer(b'', 429, headers=[('Retry-After', '30')])})
        adapter.deadline = None  # As the downloader begins each job.
        with self.assertRaisesRegex(ArchiveError, 'rate limit'):
            adapter.account_identity()
        # The rest of that job asks for nothing more.
        with self.assertRaisesRegex(ArchiveError, 'rate limit') as stopped:
            adapter.account_identity()
        self.assertEqual((len(client.calls), stopped.exception.retry_after_seconds), (1, 30))
        client.answers[KEY_PAGE] = page_data(account=5)
        adapter.deadline = None
        self.assertEqual(adapter.account_identity(), '5')
        self.assertEqual(len(client.calls), 2)

    @patch('builtins.print')
    def test_a_series_tappytoon_no_longer_has_is_set_aside_and_go_goes_on_with_the_rest(self, output, sleep):
        gone = API + '/comics/68?locale=en'
        for code in (404, 410):
            adapter, client = reader({KEY_PAGE: page_data(), gone: Answer(b'{}', code)})
            with self.subTest(code=code), self.assertRaises(MetadataError) as raised:
                adapter.inspect_series('68')
            self.assertEqual((raised.exception.source_http_status, raised.exception.source_series_id, raised.exception.source_route),
                             (code, '68', '/series/68'))
            # The key is renewed once first, in case it was only the key that had run out.
            self.assertEqual([url for url, _ in client.calls], [KEY_PAGE, gone] * 2)
        # A refusal is not taken for a series that is gone.
        adapter, _ = reader({KEY_PAGE: page_data(), gone: Answer(b'{}', 403)})
        with self.assertRaises(ArchiveError) as raised:
            adapter.inspect_series('68')
        self.assertNotIsInstance(raised.exception, MetadataError)
        answers = series_answers() | {KEY_PAGE: page_data(account=5), gone: Answer(b'{}', 404),
                                      LIST['unlocked']: [{'bookId': 68, 'title': 'Gone Series'}, {'bookId': 67, 'title': 'Sample Series'}]}
        adapter, _ = reader(answers)
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901', '903'])

    def test_a_chapter_tappytoon_no_longer_gives_or_has_is_set_aside_without_asking_for_its_pictures(self, sleep):
        url = 'https://www.tappytoon.com/en/chapters/900'
        for code in (403, 404, 410):
            adapter, client = reader(series_answers() | {f'{API}/chapters/900?locale=en': Answer(b'{}', code)})
            for check in (adapter.capture_automatic, adapter.episode_access):
                with self.subTest(code=code, check=check.__name__), self.assertRaises(AccessDenied):
                    check(url)
            self.assertFalse(any('manifest' in address or address.startswith(CDN) for address, _ in client.calls))
        adapter, _ = reader(series_answers() | {f'{API}/chapters/900?locale=en': Answer(b'{}', 401)})
        with self.assertRaises(ArchiveError) as raised:
            adapter.capture_automatic(url)
        self.assertNotIsInstance(raised.exception, AccessDenied)

    def test_whether_a_chapter_may_be_read_is_told_from_its_details_alone(self, sleep):
        rented = chapter(2, free=False) | {'isAccessible': True, 'isUserRented': True, 'rentalExpiredAt': RENTAL_END}
        adapter, client = reader(series_answers() | {f'{API}/chapters/902?locale=en': rented})
        self.assertEqual(adapter.episode_access('https://www.tappytoon.com/en/chapters/900'),
                         {'source_episode_id': '900', 'access_state': 'tappytoon_free', 'access_expires_at': None})
        self.assertEqual(adapter.episode_access('https://www.tappytoon.com/en/chapters/902'),
                         {'source_episode_id': '902', 'access_state': 'tappytoon_unlocked',
                          'access_expires_at': '2099-11-01T10:00:00+00:00'})
        for end in (None, 'soon'):
            adapter.transport.client.answers[f'{API}/chapters/902?locale=en'] = rented | {'rentalExpiredAt': end}
            with self.subTest(end=end), self.assertRaises(ArchiveError):
                adapter.episode_access('https://www.tappytoon.com/en/chapters/902')
        adapter.transport.client.answers[f'{API}/chapters/902?locale=en'] = chapter(2, free=False)
        with self.assertRaises(AccessDenied):
            adapter.episode_access('https://www.tappytoon.com/en/chapters/902')
        with self.assertRaises(ArchiveError):
            adapter.episode_access('https://www.tappytoon.com/en/book/sample-series')
        self.assertFalse(any('manifest' in url or url.startswith(CDN) for url, _ in client.calls))

    @patch('builtins.print')
    def test_a_free_only_download_checks_each_chapter_first_and_saves_only_the_free_ones(self, output, sleep):
        adapter, client = reader(series_answers() | {KEY_PAGE: page_data(account=5), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}]})
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'], free_only=True)
        self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901'])
        self.assertFalse(any('chapterId=903' in url for url, _ in client.calls))
        self.assertEqual(verify(self.root), [])

    @patch('builtins.print')
    def test_a_cover_whose_address_ran_out_is_asked_for_again_with_the_series_details_read_afresh(self, output, sleep):
        fresh = 'https://image-repository-cdn.tappytoon.com/series/67/fresh.jpg'

        class CoverRunsOut(Site):
            def stream(self, method, url, headers):
                if url == COMIC['coverImageUrl']:
                    # Tappytoon now gives the series with a new cover address.
                    self.answers[API + '/comics/67?locale=en'] = COMIC | {'coverImageUrl': fresh}
                return super().stream(method, url, headers)

        client = CoverRunsOut(series_answers() | {KEY_PAGE: page_data(account=5), COMIC['coverImageUrl']: Answer(b'', 403),
                                                  fresh: png(201), LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}]})
        adapter = TappytoonAdapter(transport=Transport(client=client, site=sites.named('tappytoon')))
        with adapter:
            result = sync(adapter, self.root, categories=['unlocked'])
        self.assertEqual((result['enriched_series_ids'], result['metadata_unavailable_series']), (['67'], []))
        self.assertEqual([url for url, _ in client.calls if 'image-repository' in url], [COMIC['coverImageUrl'], fresh])

    def test_the_downloader_reads_tappytoon_with_its_reader(self, sleep):
        self.assertIs(worker.reader(sites.named('tappytoon')), TappytoonAdapter)


class KeyAndCookieTests(unittest.TestCase):
    def test_a_sites_key_goes_only_to_its_own_hosts_and_only_cookies_for_the_site_itself_are_kept(self):
        client = Site({f'{API}/x': Answer(b'{}', headers=[
            ('Set-Cookie', 'CloudFront-Signature=chapter1; Domain=.tappytoon.com; Path=/'),
            ('Set-Cookie', 'own=1; Path=/'),
            ('Set-Cookie', 'other=1; Domain=webtoons.com'),
            ('Set-Cookie', 'page=1; Domain=www.tappytoon.com'),
            ('Set-Cookie', 'wider=1; Domain=com')])})
        transport = Transport([{'name': 'CloudFront-Signature', 'value': 'old', 'domain': '.tappytoon.com'}], client=client,
                              site=sites.named('tappytoon'))
        with patch('comic_archive.transport.time.sleep'):
            transport.get(f'{API}/x', authorization=KEY, take_cookies=True)
        self.assertEqual(transport.cookies, (('tappytoon.com', 'CloudFront-Signature=chapter1'),))
        self.assertEqual(transport.cookie_header('content-repository-cdn.tappytoon.com'), 'CloudFront-Signature=chapter1')
        for url in ('https://image-repository-cdn.tappytoon.com/a.jpg', 'https://tappytoon.com.example.com/a'):
            with self.subTest(url), self.assertRaises(ArchiveError):
                transport.get(url, authorization=KEY)
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
