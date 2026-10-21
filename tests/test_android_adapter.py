"""The Android Tapas adapter against synthetic responses: no network, no device, no browser.

Golden-source checks of the exact semantics jobs.archive_discovered/jobs.sync rely on, over
an authenticated library holding one comic and one novel plus a locked episode refusal, with
every request answered by canned bodies through the real adapter stack. The recorded request
order proves the reused core methods (check_session's who-am-i, reading-list pagination,
resolve_series, enumerate_episodes, access_state) are the ones doing the work. Like
test_android_transport.py these need the merged core (Transport's cookie_source/cookie_sink
constructor seams) and have not run against the pre-merge tree; no recorder framework exists
here — one transport fake serves bodies and notes what was asked.
"""
import json
import unittest

from comic_archive.android_adapter import AndroidTapasAdapter, ReadingListHTML, signed_in_probe
from comic_archive.android_transport import AndroidTransport
from comic_archive.errors import AccessDenied, AuthenticationError
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport


class Store:
    """A stand-in for the native WebViewCookieStore: one live session header, site-scoped."""

    def __init__(self, broken=False):
        self.broken = broken
        self.asked = []

    def headerFor(self, url):
        self.asked.append(url)
        if self.broken:
            raise RuntimeError('value=PRIVATE')
        return 'session=GOLDEN' if url.startswith('https://tapas.io/') else None

    def apply(self, url, values):
        pass


class Client:
    """The socket-less client stand-in: it only records that it was closed."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SyntheticTransport(AndroidTransport):
    """The real transport with the socket replaced: canned bodies, exact request order."""

    def __init__(self, responses, store=None, **kwargs):
        self.client_fake = Client()
        super().__init__(store or Store(), interval=1.0, client=self.client_fake, **kwargs)
        self.interval = 0.0
        self.responses = responses
        self.fetched = []

    def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
        self.fetched.append((url, headers.get("Cookie")))
        body = self.responses[url]
        if isinstance(body, BaseException):
            raise body
        return body


def body(value):
    return json.dumps(value).encode()


WHO_AM_I = "https://tapas.io/who-am-i"
SIGNED_IN = body({'data': {'user': {'id': 4242}}})
LIST_1 = "https://tapas.io/reading-list?category=SUBSCRIBED&pageNumber=1"
LIST_2 = "https://tapas.io/reading-list?category=SUBSCRIBED&pageNumber=2"


def page(cards, next_page=None):
    """One served reading-list page in the site's own list/pager markup."""
    nxt = (f'<section class="paging-wrap"><a class="paging__button--next" '
           f'href="/reading-list?category=SUBSCRIBED&pageNumber={next_page}">Next</a></section>') \
        if next_page else ''
    return ('<!doctype html><html><body><ul class="content-list-wrap">'
            + ''.join(cards) + f'</ul>{nxt}</body></html>').encode()


def card(series_id, slug, title, on_anchor=False):
    """A series card: the tiara series ID sits on the list item, or on the link itself."""
    if on_anchor:
        return (f'<li class="content-item"><a href="/series/{slug}" '
                f'data-tiara-event-meta-series-id="{series_id}">{title}</a></li>')
    return (f'<li class="content-item" data-tiara-event-meta-series-id="{series_id}">'
            f'<a href="/series/{slug}">{title}</a></li>')


LIBRARY_1 = page([card(1001, 'golden-comic', 'Golden Comic'),
                  card(2002, 'silver-novel', 'Silver Novel', on_anchor=True)], next_page=2)
LIBRARY_2 = page([card(3003, 'bronze-comic', 'Bronze Comic')])
AMBIGUOUS = page(['<li class="content-item" data-tiara-event-meta-series-id="1001">'
                  '<div data-tiara-event-meta-series-id="2002">'
                  '<a href="/series/two-ids">Ambiguous</a></div></li>'])
LOCKED_INFO = "https://tapas.io/episode/9003/info"
LOCKED = body({'data': {'id': 9003, 'title': 'Locked Gate', 'scene': 2, 'must_pay': True,
                        'scheduled': False, 'book': False, 'free': False, 'unlocked': False,
                        'closing_date': None}})
COMIC = body({'data': {'id': 1001, 'title': 'Golden Comic', 'url': 'golden-comic',
                       'comic': True, 'book': False}})
NOVEL = body({'data': {'id': 2002, 'title': 'Silver Novel', 'url': 'silver-novel',
                       'comic': False, 'book': True}})
COMIC_EPISODES = body({'data': {'pagination': {'total': 2, 'has_next': False}, 'episodes': [
    {'id': 9001, 'title': 'Golden Open', 'scene': 1, 'must_pay': False, 'scheduled': False,
     'book': False, 'free': True, 'unlocked': False, 'closing_date': None},
    {'id': 9003, 'title': 'Locked Gate', 'scene': 2, 'must_pay': True, 'scheduled': False,
     'book': False, 'free': False, 'unlocked': False, 'closing_date': None}]}})
NOVEL_EPISODES = body({'data': {'pagination': {'total': 1, 'has_next': False}, 'episodes': [
    {'id': 9100, 'title': 'Silver Chapter One', 'scene': 1, 'must_pay': False, 'scheduled': False,
     'book': True, 'free': False, 'unlocked': True, 'closing_date': None}]}})


def library_transport():
    return SyntheticTransport({WHO_AM_I: SIGNED_IN, LIST_1: LIBRARY_1, LIST_2: LIBRARY_2,
                               "https://tapas.io/series/1001": COMIC,
                               "https://tapas.io/series/1001/episodes?page=1&max_limit=20": COMIC_EPISODES,
                               "https://tapas.io/series/2002": NOVEL,
                               "https://tapas.io/series/2002/episodes?page=1&max_limit=20": NOVEL_EPISODES,
                               LOCKED_INFO: LOCKED})


class ReadingListHTMLTests(unittest.TestCase):
    def test_series_links_and_supplied_pager_links_are_extracted_with_their_one_identity(self):
        parser = ReadingListHTML(LIST_1)
        parser.feed(LIBRARY_1.decode())
        self.assertEqual(parser.cards, [
            {'source_series_id': '1001', 'title': 'Golden Comic', 'source_url': 'https://tapas.io/series/golden-comic'},
            {'source_series_id': '2002', 'title': 'Silver Novel', 'source_url': 'https://tapas.io/series/silver-novel'}])
        self.assertEqual(parser.page_links, [{'page': 2, 'category': 'subscribed', 'type': 'all'}])

    def test_off_site_and_unshapely_links_are_not_cards(self):
        html = page(['<li><a href="https://evil.example/series/x">No</a></li>',
                     '<li><a href="https://tapas.io/episode/9001">Episode</a></li>',
                     '<li><a href="/series/Bad_Slug!">Bad</a></li>'])
        parser = ReadingListHTML(LIST_1)
        parser.feed(html.decode())
        self.assertEqual(parser.cards, [])
        self.assertEqual(parser.page_links, [])


class AndroidTapasAdapterDiscoveryTests(unittest.TestCase):
    def test_the_authenticated_library_is_listed_page_by_page_to_its_boundary(self):
        transport = library_transport()
        adapter = AndroidTapasAdapter(transport=transport)
        checkpoints = []
        result = adapter.inspect_library(category='subscribed', on_page=checkpoints.append)
        self.assertEqual(result['pages'], [{'page': 1, 'series_count': 2}, {'page': 2, 'series_count': 1}])
        self.assertEqual(result['series'], [
            {'source_series_id': '1001', 'title': 'Golden Comic', 'source_url': 'https://tapas.io/series/golden-comic'},
            {'source_series_id': '2002', 'title': 'Silver Novel', 'source_url': 'https://tapas.io/series/silver-novel'},
            {'source_series_id': '3003', 'title': 'Bronze Comic', 'source_url': 'https://tapas.io/series/bronze-comic'}])
        self.assertTrue(result['authenticated'])
        self.assertTrue(result['membership_verified'])
        self.assertEqual(result['scope'], 'subscribed')
        self.assertTrue(result['boundary_observed'])
        self.assertTrue(result['complete'])
        self.assertEqual((result['observed_series_count'], result['unresolved_series_id_count'],
                          result['duplicate_series_on_later_pages']), (3, 0, 0))
        self.assertEqual(result['blocker'],
                         "Reading-list boundary observed over the site's paged HTML; "
                         'purchase/unlock reconciliation remains unverified.')
        # The second checkpoint carries exactly the per-page contract the sync job verifies.
        self.assertEqual(checkpoints[1], {'authenticated': True, 'membership_verified': True,
                                          'scope': 'subscribed', 'complete': False,
                                          'series': [{'source_series_id': '3003', 'title': 'Bronze Comic',
                                                       'source_url': 'https://tapas.io/series/bronze-comic'}],
                                          'pages': [{'page': 1, 'series_count': 2}, {'page': 2, 'series_count': 1}]})
        # The reused core flow: verified identity first, then each supplied page in order,
        # every one of them the site's own page with the live session header.
        self.assertEqual([url for url, _ in transport.fetched], [WHO_AM_I, LIST_1, LIST_2])
        self.assertTrue(all(cookie == 'session=GOLDEN' for _, cookie in transport.fetched))
        self.assertTrue(all(url.startswith('https://tapas.io/') for url in transport.fetched))

    def test_a_card_without_one_verified_identity_leaves_the_list_incomplete(self):
        transport = SyntheticTransport({WHO_AM_I: SIGNED_IN, LIST_1: AMBIGUOUS})
        result = AndroidTapasAdapter(transport=transport).inspect_library(category='subscribed')
        self.assertEqual(result['series'][0]['source_series_id'], None)
        self.assertEqual(result['unresolved_series_id_count'], 1)
        self.assertTrue(result['boundary_observed'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['blocker'],
                         'Some listed series have no single verified series ID; the list is not complete.')

    def test_an_unauthenticated_session_fails_closed_before_any_page_is_requested(self):
        transport = SyntheticTransport({WHO_AM_I: body({'data': {}})})
        with self.assertRaises(AuthenticationError):
            AndroidTapasAdapter(transport=transport).inspect_library(category='subscribed')
        self.assertEqual([url for url, _ in transport.fetched], [WHO_AM_I])

    def test_an_empty_page_budget_answer_is_an_honest_incomplete_result(self):
        transport = SyntheticTransport({WHO_AM_I: SIGNED_IN, LIST_1: page([])})
        result = AndroidTapasAdapter(transport=transport).inspect_library(category='subscribed')
        self.assertEqual((result['series'], result['complete'], result['boundary_observed']), ([], False, False))
        self.assertEqual(result['blocker'],
                         'The reading list is empty or repeats a prior page; completeness remains unverified.')

    def test_unknown_categories_and_the_browser_flows_fail_closed(self):
        adapter = AndroidTapasAdapter(transport=library_transport())
        with self.assertRaises(ArchiveError):
            adapter.inspect_library(category='nonsense')
        with self.assertRaises(ArchiveError):
            adapter.inspect_library(details=True)
        with self.assertRaises(ArchiveError):
            adapter.inspect_transaction_history(max_pages=5, on_page=lambda value: None)

    def test_free_episodes_reports_an_honest_blocker_without_requesting_the_filter(self):
        transport = library_transport()
        result = AndroidTapasAdapter(transport=transport).inspect_library(category='free_episodes')
        self.assertEqual((result['authenticated'], result['membership_verified'], result['scope']), (True, True, 'free_episodes'))
        self.assertEqual((result['series'], result['pages'], result['complete']), ([], [], False))
        self.assertEqual(result['blocker'],
                         'The Free Episodes filter is a visible browser control; '
                         'the Android worker did not request it.')
        self.assertEqual([url for url, _ in transport.fetched], [WHO_AM_I])


class AndroidTapasAdapterArchiveTests(unittest.TestCase):
    def test_a_locked_episode_is_refused_by_the_real_access_gate_before_any_reader(self):
        transport = library_transport()
        with self.assertRaises(AccessDenied):
            AndroidTapasAdapter(transport=transport).episode_access('https://tapas.io/episode/9003')
        # The reused core gate: verified identity, the episode's own info, and nothing else.
        self.assertEqual([url for url, _ in transport.fetched], [WHO_AM_I, LOCKED_INFO])
        self.assertEqual(transport.fetched[1][1], 'session=GOLDEN')

    def test_inspect_series_reports_the_comic_and_the_novel_with_exact_access_states(self):
        transport = library_transport()
        adapter = AndroidTapasAdapter(transport=transport)
        comic = adapter.inspect_series('1001', strict_order=False)
        self.assertEqual(comic, {'complete': True, 'authenticated': True, 'source_series_id': '1001',
                                 'title': 'Golden Comic', 'episode_count': 2, 'ordering_verified': True,
                                 'accessible_episode_count': 1,
                                 'series_content_flags': {'book': False, 'comic': True},
                                 'episodes': [
                                     {'id': '9001', 'title': 'Golden Open', 'order': 1, 'source_scene': 1,
                                      'access_state': 'tapas_free', 'authorization_reason': None,
                                      'access_expires_at': None, 'source_url': 'https://tapas.io/episode/9001'},
                                     {'id': '9003', 'title': 'Locked Gate', 'order': 2, 'source_scene': 2,
                                      'access_state': 'unavailable_or_unverified',
                                      'authorization_reason': 'access_denied',
                                      'access_expires_at': None, 'source_url': 'https://tapas.io/episode/9003'}]})
        novel = adapter.inspect_series('2002', strict_order=False)
        self.assertEqual(novel['series_content_flags'], {'book': True, 'comic': False})
        self.assertEqual(novel['episodes'][0]['access_state'], 'tapas_unlocked')
        # resolve_series and enumerate_episodes were both reached for each series.
        self.assertEqual([url for url, _ in transport.fetched], [
            WHO_AM_I, "https://tapas.io/series/1001",
            "https://tapas.io/series/1001/episodes?page=1&max_limit=20",
            WHO_AM_I, "https://tapas.io/series/2002",
            "https://tapas.io/series/2002/episodes?page=1&max_limit=20"])

    def test_an_expected_account_change_stops_the_job(self):
        transport = library_transport()
        adapter = AndroidTapasAdapter(transport=transport, expected_account_id='999')
        with self.assertRaises(AuthenticationError):
            adapter.episode_access('https://tapas.io/episode/9003')


class AndroidTapasAdapterConstructionTests(unittest.TestCase):
    def test_each_live_wiring_form_is_accepted_and_junk_is_refused(self):
        adapter = AndroidTapasAdapter(Store())
        self.assertIsInstance(adapter.transport, AndroidTransport)
        source, sink = lambda url: '', lambda url, values: None
        adapter = AndroidTapasAdapter(cookie_source=source, cookie_sink=sink)
        self.assertIsInstance(adapter.transport, Transport)
        self.assertIs(adapter.transport.cookie_source, source)
        self.assertIs(adapter.transport.cookie_sink, sink)
        shared = library_transport()
        self.assertIs(AndroidTapasAdapter(transport=shared).transport, shared)
        for invalid in (None, object(), 'store'):
            with self.assertRaises(ArchiveError):
                AndroidTapasAdapter(cookie_source=invalid)
        with self.assertRaises(ArchiveError):
            AndroidTapasAdapter(transport=object())

    def test_entry_verifies_the_account_and_exit_closes_the_live_transport(self):
        transport = library_transport()
        with AndroidTapasAdapter(transport=transport) as adapter:
            self.assertEqual(adapter.account_identity(), '4242')
        self.assertEqual([url for url, _ in transport.fetched], [WHO_AM_I, WHO_AM_I])
        self.assertTrue(transport.client_fake.closed)


class SignedInProbeTests(unittest.TestCase):
    def test_only_the_verified_who_am_i_reports_signed_in(self):
        self.assertEqual(signed_in_probe(transport=library_transport()),
                         {'signed_in': True, 'verified': True, 'reason': None})
        # Cookies present (the store answers for every site page) yet who-am-i denies:
        # never signed in. Presence is not authentication.
        denying = SyntheticTransport({WHO_AM_I: body({'data': {}})})
        self.assertEqual(signed_in_probe(transport=denying),
                         {'signed_in': False, 'verified': False, 'reason': 'not_signed_in'})
        failing = SyntheticTransport({WHO_AM_I: ArchiveError('unavailable')}, store=Store(broken=True))
        self.assertEqual(signed_in_probe(transport=failing),
                         {'signed_in': False, 'verified': False, 'reason': 'unavailable'})

    def test_the_probe_result_is_public_safe(self):
        for result in (signed_in_probe(transport=library_transport()),
                       signed_in_probe(transport=SyntheticTransport({WHO_AM_I: body({'data': {}})}))):
            self.assertNotIn('4242', json.dumps(result))
            self.assertNotIn('GOLDEN', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
