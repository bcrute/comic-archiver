"""Replay the desktop's closed HTTP fixtures through native reader construction.

No device, live account or provider request is used. Real WebView profile/isolation
and workspace switching remain separate native/device gates for the whole block.
"""
import unittest
from urllib.parse import urlsplit

import httpx

from comic_archive import sites, worker_host
from comic_archive.adapters.webtoon import WebtoonAdapter
from comic_archive.android_readers import reader
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport
from test_webtoon import HOME, USER_INFO, SUBSCRIPTIONS, series_pages, subscriptions_page, subscription, who
from test_tappytoon import page_data


class NativeStore:
    def __init__(self, site):
        self.site = site
        self.asked, self.applied = [], []

    def headerFor(self, url):
        self.asked.append(url)
        return 'sid=FIXTURE_SESSION' if urlsplit(url).hostname in self.site.cookie_hosts else None

    def apply(self, url, values):
        self.applied.append((url, list(values)))

    def browserUserAgent(self):
        return 'Fixture WebView/1.0'


def browser_cookie(domain):
    return {'domain': domain, 'path': '/', 'name': 'sid', 'value': 'FIXTURE_SESSION',
            'expires': -1, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}


class NativeReaderTests(unittest.TestCase):
    def webtoon_wire(self):
        listing = HOME + '/en/fantasy/sample-series/list?title_no=7'
        pages = series_pages() | {USER_INFO: who('fixture-account'),
                 SUBSCRIPTIONS: subscriptions_page(subscription(listing, 'Sample Series'))}
        calls = []

        def request(value):
            calls.append((str(value.url), value.headers.get('Cookie')))
            if str(value.url) not in pages:
                raise AssertionError('Reader requested an unrecorded fixture endpoint.')
            if urlsplit(str(value.url)).hostname in sites.named('webtoon').cookie_hosts:
                self.assertEqual(value.headers.get('Cookie'), 'sid=FIXTURE_SESSION')
            return httpx.Response(200, content=pages[str(value.url)])
        return httpx.Client(transport=httpx.MockTransport(request)), calls

    def test_webtoon_discovery_replays_the_same_closed_desktop_http_answers(self):
        reference_client, reference_calls = self.webtoon_wire()
        native_client, native_calls = self.webtoon_wire()
        site = sites.named('webtoon')
        store = NativeStore(site)
        reference = WebtoonAdapter(transport=Transport(cookies=[browser_cookie('.webtoons.com')],
            client=reference_client, site=site, interval=0))
        native = reader('webtoon', store, client=native_client, interval=0)
        self.addCleanup(reference.transport.close); self.addCleanup(native.transport.close)
        expected = reference.inspect_library(category='subscribed')
        actual = native.inspect_library(category='subscribed')
        self.assertEqual(actual, expected)
        self.assertEqual(native.inspect_series('7'), reference.inspect_series('7'))
        native_series, reference_series = native.series_metadata('7'), reference.series_metadata('7')
        self.assertEqual(native_series, reference_series)
        self.assertEqual(native.retrieve_cover(native_series), reference.retrieve_cover(reference_series))
        self.assertEqual([url for url, _ in native_calls], [url for url, _ in reference_calls])
        self.assertTrue(native_calls)
        self.assertTrue(all(cookie == ('sid=FIXTURE_SESSION' if urlsplit(url).hostname in site.cookie_hosts else None)
                            for url, cookie in native_calls))
        self.assertEqual(native.transport.cookies, ())

    def test_cookie_source_is_never_asked_for_a_webtoon_image_cdn_or_other_site(self):
        store = NativeStore(sites.named('webtoon'))
        requests = []

        def request(value):
            requests.append(value)
            return httpx.Response(200, content=b'fixture-image')
        client = httpx.Client(transport=httpx.MockTransport(request))
        native = reader('webtoon', store, client=client, interval=0)
        self.addCleanup(native.transport.close)
        self.assertEqual(native.transport.get('https://webtoon-phinf.pstatic.net/fixture.jpg'), b'fixture-image')
        self.assertEqual(store.asked, [])
        self.assertNotIn('Cookie', requests[0].headers)
        with self.assertRaises(ArchiveError):
            native.transport.get('https://www.tappytoon.com/en/fixture')
        self.assertEqual(len(requests), 1)

    def test_native_tappytoon_identity_uses_the_same_public_page_key_and_live_cookie_authority(self):
        from comic_archive.adapters.tappytoon import KEY_PAGE, TappytoonAdapter
        site = sites.named('tappytoon')
        page = page_data(account=321)
        calls = []

        def request(value):
            self.assertEqual(str(value.url), KEY_PAGE)
            calls.append(value.headers.get('Cookie'))
            self.assertEqual(value.headers.get('Cookie'), 'sid=FIXTURE_SESSION')
            return httpx.Response(200, content=page)
        reference_client = httpx.Client(transport=httpx.MockTransport(request))
        native_client = httpx.Client(transport=httpx.MockTransport(request))
        reference = TappytoonAdapter(transport=Transport(cookies=[browser_cookie('.tappytoon.com')],
            client=reference_client, site=site, interval=0))
        native = reader('tappytoon', NativeStore(site), client=native_client, interval=0)
        self.addCleanup(reference.transport.close); self.addCleanup(native.transport.close)
        self.assertEqual(native.account_identity(), reference.account_identity())
        self.assertEqual(native.account_identity(), '321')
        self.assertEqual(calls, ['sid=FIXTURE_SESSION', 'sid=FIXTURE_SESSION'])
        self.assertEqual(native.transport.cookies, ())

    def test_unknown_or_unwired_site_never_builds_a_fallback_reader(self):
        for key in ('unknown', 'tapas;other', None, 7):
            with self.subTest(key=key), self.assertRaises(ArchiveError):
                reader(key, NativeStore(sites.DEFAULT))
        with self.assertRaises(ArchiveError):
            reader('webtoon', object())
        with self.assertRaises(ArchiveError):
            worker_host.android_adapter_factory(lambda url: '', site_key='unknown')
