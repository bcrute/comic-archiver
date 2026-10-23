"""Core-versus-stand-in contracts, run on the fleet; no Android execution claimed."""
import contextlib
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from comic_archive.android_adapter import AndroidTapasAdapter
from comic_archive.android_transport import AndroidTransport
from comic_archive.errors import AccessDenied, AuthenticationError


class Store:
    def __init__(self, cookie='fixture_session=READY'):
        self.cookie = cookie

    def headerFor(self, url):
        return self.cookie if url.startswith('https://m.tapas.io/') else None

    def apply(self, url, values):
        pass

    def browserUserAgent(self):
        return 'Mozilla/5.0 Android integration fixture'


class SiteStandInTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'android/app/src/androidTest/assets/beta_fixture.py'
        spec = importlib.util.spec_from_file_location('_beta_fixture_test', path)
        self.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fixture)
        self.fixture.release_transfer()

    def adapter(self, cookie='fixture_session=READY'):
        transport = AndroidTransport(Store(cookie), client=self.fixture.client(), interval=0.25)
        # Fixture semantics, not wall-clock pacing; production construction still respects its minimum.
        transport.interval = 0
        self.addCleanup(transport.close)
        return AndroidTapasAdapter(transport=transport)

    def test_native_transport_and_core_parser_read_both_real_list_pages(self):
        with self.adapter() as adapter:
            result = adapter.inspect_library(category='subscribed')
        self.assertTrue(result['authenticated'])
        self.assertTrue(result['complete'])
        self.assertEqual([row['source_series_id'] for row in result['series']], ['1001', '2002'])
        self.assertEqual(len(json.loads(self.fixture.observation())['subscribed_pages']), 2)

    def test_cookie_presence_without_the_expected_identity_stays_signed_out(self):
        for cookie in ('', 'fixture_session=WRONG', 'unrelated=READY'):
            with self.subTest(cookie=cookie), self.assertRaises(AuthenticationError):
                with self.adapter(cookie):
                    pass

    def test_comic_order_and_png_bytes_match_the_production_capture_flow(self):
        adapter = self.adapter()
        manifest = adapter._http.capture('https://tapas.io/episode/9001')
        self.assertEqual(tuple(page.location for page in manifest.episodes[0].pages), self.fixture.ASSETS)
        for url, expected in zip(self.fixture.ASSETS, self.fixture.PAGES, strict=True):
            self.assertEqual(adapter.transport.get(url), expected)
            self.assertTrue(expected.startswith(b'\x89PNG\r\n\x1a\n'))
        facts = json.loads(self.fixture.observation())
        self.assertEqual(facts['asset_cookie_leaks'], 0)
        self.assertEqual(facts['unexpected_requests'], 0)

    def test_core_novel_capture_accepts_only_the_scoped_fixture_text(self):
        manifest = self.adapter()._http.capture('https://tapas.io/episode/9100')
        chapter = manifest.episodes[0]
        self.assertEqual(chapter.content_type, 'novel')
        self.assertIn(self.fixture.NOVEL_TEXT, chapter.text_html)
        self.assertEqual(chapter.pages, ())

    def test_locked_episode_stops_before_reader_or_asset_retrieval(self):
        with self.assertRaises(AccessDenied):
            self.adapter()._http.capture('https://tapas.io/episode/9003')
        facts = json.loads(self.fixture.observation())
        self.assertFalse(facts['locked_reader_requested'])
        self.assertEqual(facts['asset_requests'], 0)

    def test_unknown_request_is_answered_locally_and_counted_as_a_failure(self):
        with self.fixture.client() as client:
            self.assertEqual(client.get('https://unmapped.invalid/private').status_code, 404)
            self.assertEqual(client.post('https://m.tapas.io/purchase').status_code, 405)
        self.assertEqual(json.loads(self.fixture.observation())['unexpected_requests'], 2)

    def test_install_changes_socket_factory_but_retains_native_cookie_authority(self):
        from comic_archive import android_adapter, android_readers, android_transport
        store = Store()
        with contextlib.ExitStack() as stack:
            for module in (android_adapter, android_readers, android_transport):
                stack.enter_context(patch.object(module, 'AndroidTransport', module.AndroidTransport))
            self.fixture.install()
            reader = android_readers.reader('tapas', store)
            self.addCleanup(reader.transport.close)
            self.assertIs(reader.transport.store._bridge, store)
            self.assertIsInstance(reader.transport.client._transport, self.fixture.httpx.MockTransport)
            self.assertEqual(reader.account_identity(), '4242')

    def test_server_can_hold_a_real_httpx_stream_until_the_native_pause_action_arrives(self):
        self.fixture._release.clear()
        received, errors = [], []

        def read():
            try:
                with self.fixture.client() as client, client.stream('GET', self.fixture.ASSETS[0]) as response:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        received.append(chunk)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=read)
        thread.start()
        try:
            self.assertTrue(self.fixture._blocked.wait(2))
            self.assertTrue(thread.is_alive())
            self.assertEqual(sum(map(len, received)), 65536)
        finally:
            self.fixture.release_transfer()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(b''.join(received), self.fixture.PAGES[0])
