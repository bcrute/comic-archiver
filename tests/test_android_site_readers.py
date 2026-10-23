"""Replay the desktop's closed HTTP fixtures through native reader construction.

No device, live account or provider request is used. Real WebView profile/isolation
and workspace switching remain separate native/device gates for the whole block.
"""
import unittest
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

import httpx

from comic_archive import network, network_android, sites, worker_host
from comic_archive.adapters.webtoon import WebtoonAdapter
from comic_archive.android_readers import reader, signed_in_probe
from comic_archive.models import ArchiveError
from comic_archive.archive import verify
from comic_archive.inventory import Inventory
from comic_archive.jobs import sync
from comic_archive.transport import Transport
from test_webtoon import HOME, USER_INFO, SUBSCRIPTIONS, series_pages, subscriptions_page, subscription, who
from test_tappytoon import page_data
import test_tappytoon as tappytoon_fixture
import test_webtoon as webtoon_fixture
from test_archive import png


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
    @patch('builtins.print')
    def test_native_webtoon_saves_the_desktop_paginated_archive_fixture_with_the_same_original_images(self, output):
        pages = series_pages() | {USER_INFO: who('fixture-account')}
        observed = []

        def request(value):
            url = str(value.url)
            observed.append(value)
            self.assertIn(url, pages)
            return httpx.Response(200, content=pages[url])

        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(request)) as client:
            root = Path(folder) / 'Library'
            with Inventory(root) as inventory:
                inventory.observe('7', 'Sample Series', [], 'fixture')
            native = reader('webtoon', NativeStore(sites.named('webtoon')), client=client, interval=0)
            with native:
                result = sync(native, root, refresh_lists=False)
            self.assertEqual(sorted(result['archived_episode_ids']), ['7000001', '7000002', '7000004'])
            self.assertEqual(verify(root), [])
            for path, number in zip(sorted(root.glob('**/*.cbz')), (1, 2, 4), strict=True):
                with zipfile.ZipFile(path) as archive:
                    pictures = [archive.read(name) for name in sorted(archive.namelist()) if name.endswith('.png')]
                self.assertEqual(pictures, [png(10 * number + 1), png(10 * number + 2)])
        images = [value for value in observed if str(value.url).startswith(webtoon_fixture.IMAGES)]
        self.assertEqual(len(images), 6)
        for value in images:
            self.assertNotIn('cookie', value.headers)
            self.assertNotIn('authorization', value.headers)
            self.assertEqual(value.headers['referer'], HOME + '/')

    @patch('builtins.print')
    def test_native_tappytoon_saves_only_the_desktop_fixtures_free_and_unlocked_chapters(self, output):
        fixture = tappytoon_fixture

        class ChapterStore(NativeStore):
            # A deterministic stand-in for the native store's own cookie parser.
            # Production still forwards raw attributes to WebView, without a jar.
            chapter = ''

            def apply(self, url, values):
                super().apply(url, values)
                for value in values:
                    if value.startswith('CloudFront-Signature=chapter'):
                        self.chapter = value.split(';', 1)[0]

            def headerFor(self, url):
                base = super().headerFor(url)
                return base + ('; ' + self.chapter if self.chapter else '') if base else None

        answers = fixture.series_answers() | {fixture.KEY_PAGE: page_data(account=5),
            fixture.LIST['unlocked']: [{'bookId': 67, 'title': 'Sample Series'}]}
        observed = []

        def request(value):
            url = str(value.url)
            observed.append(value)
            self.assertIn(url, answers)
            if url.startswith(fixture.API):
                self.assertEqual(value.headers.get('authorization'), fixture.KEY)
            if url.startswith(fixture.CDN):
                self.assertIn('CloudFront-Signature=chapter', value.headers.get('cookie', ''))
            answer = answers[url]
            if isinstance(answer, fixture.Answer):
                return httpx.Response(answer.status_code, content=answer.data, headers=answer.headers)
            return httpx.Response(200, content=answer if isinstance(answer, bytes) else json.dumps(answer).encode())

        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(request)) as client:
            root = Path(folder) / 'Library'
            store = ChapterStore(sites.named('tappytoon'))
            native = reader('tappytoon', store, client=client, interval=0)
            with native:
                result = sync(native, root, categories=['unlocked'])
            self.assertEqual(sorted(result['archived_episode_ids']), ['900', '901', '903'])
            self.assertEqual(verify(root), [])
            pictures = []
            for path in root.glob('**/*.cbz'):
                with zipfile.ZipFile(path) as archive:
                    pictures.append([archive.read(name) for name in sorted(archive.namelist())
                                     if not name.endswith(('.xml', '.json'))])
            self.assertEqual(sorted(pictures), sorted([png(10 * n), png(10 * n + 1)] for n in (0, 1, 3)))
            self.assertEqual(native.transport.cookies, ())
        for value in observed:
            url = str(value.url)
            self.assertEqual('authorization' in value.headers, url.startswith(fixture.API))
            self.assertNotIn('tracker=elsewhere', value.headers.get('cookie', ''))
            self.assertNotIn('chapterId=902', url)
            if url == fixture.COMIC['coverImageUrl']:
                self.assertNotIn('cookie', value.headers)

    def test_declared_archive_uri_roots_keep_the_old_identity_and_match_core_workspace_folders(self):
        path = Path(__file__).resolve().parents[1] / 'android/app/src/main/res/xml/archive_paths.xml'
        roots = {node.attrib['name']: node.attrib['path'] for node in ET.parse(path).getroot()}
        self.assertEqual(roots['library'], 'workspace/Library/')
        base = Path('workspace')
        for site in sites.SITES:
            name = 'library' if site == sites.DEFAULT else site.key + '-library'
            self.assertEqual(roots[name], (sites.folder(base, site) / 'Library').as_posix() + '/')
        self.assertEqual(len(roots), len(sites.SITES))

    def test_worker_factory_enforces_the_selected_librarys_live_network_policy(self):
        self.addCleanup(network_android.configure, None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / 'Library'
            library.mkdir()
            facts = root / 'net.json'
            facts.write_text(json.dumps({'connection': 'wifi', 'metered': False}))
            network_android.configure(facts)
            network.configure(library, True)
            native = worker_host.android_adapter_factory(NativeStore(sites.named('webtoon')),
                site_key='webtoon', library_root=library)()
            native.transport.close()
            calls = []

            def request(value):
                self.assertEqual(str(value.url), USER_INFO)
                calls.append(str(value.url))
                return httpx.Response(200, content=who('fixture-account'))

            # Use the real factory's guard through the same closed HTTP source.
            with httpx.Client(transport=httpx.MockTransport(request)) as client:
                native = reader('webtoon', NativeStore(sites.named('webtoon')), client=client,
                                network_guard=native.transport.network_guard, interval=0)
                self.assertEqual(native.account_identity(), 'fixture-account')
                facts.write_text(json.dumps({'connection': 'cellular', 'metered': True}))
                with patch('comic_archive.network.time.monotonic', return_value=time.monotonic() + 2), \
                        self.assertRaises(ArchiveError):
                    native.account_identity()
            self.assertEqual(calls, [USER_INFO], 'The held request must not reach the wire')

    def test_probe_requires_provider_identity_even_with_a_live_cookie(self):
        site = sites.named('webtoon')
        for payload, expected in ((who('fixture-account'), None), (who(), 'not_signed_in'),
                                  (b'not the account response', 'unavailable')):
            calls = []

            def request(value, answer=payload):
                self.assertEqual(str(value.url), USER_INFO)
                self.assertEqual(value.headers.get('Cookie'), 'sid=FIXTURE_SESSION')
                calls.append(str(value.url))
                return httpx.Response(200, content=answer)

            with self.subTest(expected=expected), httpx.Client(transport=httpx.MockTransport(request)) as client:
                answer = signed_in_probe('webtoon', NativeStore(site), client=client)
            self.assertEqual(answer, {'signed_in': expected is None, 'verified': expected is None,
                                      'reason': expected})
            self.assertEqual(calls, [USER_INFO])
            self.assertNotIn('fixture-account', str(answer))

    def test_tappytoon_probe_verifies_the_existing_desktop_account_fixture(self):
        from comic_archive.adapters.tappytoon import KEY_PAGE
        calls = []

        def request(value):
            self.assertEqual(str(value.url), KEY_PAGE)
            calls.append(str(value.url))
            return httpx.Response(200, content=page_data(account=321))

        with httpx.Client(transport=httpx.MockTransport(request)) as client:
            answer = signed_in_probe('tappytoon', NativeStore(sites.named('tappytoon')), client=client)
        self.assertEqual(answer, {'signed_in': True, 'verified': True, 'reason': None})
        self.assertEqual(calls, [KEY_PAGE])
        self.assertNotIn('321', str(answer))

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
