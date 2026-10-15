"""Run the shipped WebView script against synthetic, entirely intercepted HTTP.

No Tapas requests leave this test; fixtures model the observed desktop contract.
"""
import copy
import json
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class AndroidCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Optional Playwright unavailable')
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.firefox.launch(headless=True)
        except Exception:
            cls.runtime.stop()
            raise unittest.SkipTest('Optional Firefox runtime unavailable')
        cls.script = (SOURCE/'assets/capture.js').read_text().replace('__EPISODE_ID__', '1')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.runtime.stop()

    def fixture(self):
        return {
            '/who-am-i': {'data': {'user': {'id': 1}}, 'error_details': None},
            '/episode/1/info': {'data': {'id': 1, 'scene': 1, 'title': 'Synthetic episode',
                'free': True, 'unlocked': False, 'must_pay': False, 'scheduled': False,
                'book': False, 'closing_date': None}, 'error_details': None},
            '/episode/1': '<script>var seriesId = 2;</script><img class="content__img" src="https://us-a.tapas.io/pc/adjacent.jpg">'
                '<div id="episode-1"><img class="content__img" src="https://us-a.tapas.io/thumbnail/placeholder.jpg"'
                ' data-src="https://us-a.tapas.io/pc/first.jpg?signature=synthetic">'
                '<img class="content__img" src="https://us-a.tapas.io/pc/second.jpg"></div>'
                '<div id="episode-2"><img class="content__img" src="https://us-a.tapas.io/pc/next.jpg"></div>',
            '/series/2': {'data': {'id': 2, 'title': 'Synthetic series', 'url': 'synthetic', 'comic': True, 'book': False}},
        }

    def capture(self, fixture):
        context = self.browser.new_context(service_workers='block'); calls = []
        page = context.new_page()
        def intercept(route):
            from urllib.parse import urlsplit
            path = urlsplit(route.request.url).path
            if path == '/' and route.request.method == 'GET':
                route.fulfill(status=200, content_type='text/html', body='<html></html>');return
            calls.append((route.request.method, path))
            response = fixture.get(path)
            if response is None:
                route.abort();return
            if isinstance(response, int):
                route.fulfill(status=response, body='synthetic refusal');return
            route.fulfill(status=200, content_type='text/html' if isinstance(response, str) else 'application/json',
                          body=response if isinstance(response, str) else json.dumps(response))
        context.route('**/*', intercept)
        try:
            page.goto('https://tapas.io/')
            page.evaluate(self.script)
            page.wait_for_function("window.__comicArchiveResult && window.__comicArchiveResult.state !== 'pending'", timeout=10000)
            return page.evaluate('window.__comicArchiveResult'), calls
        finally:
            context.close()

    def test_authorized_scope_order_original_sources_and_get_only(self):
        result, calls = self.capture(self.fixture())
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['pages'], ['https://us-a.tapas.io/pc/first.jpg?signature=synthetic',
                                         'https://us-a.tapas.io/pc/second.jpg'])
        self.assertEqual(calls, [('GET', path) for path in ('/who-am-i', '/episode/1/info', '/episode/1', '/series/2')])

    def test_locked_unknown_temporary_and_mismatched_access_stop_before_reader(self):
        changes = [{'must_pay': True}, {'free': False}, {'free': 'true'}, {'closing_date': 'temporary'},
                   {'scheduled': True}, {'book': True}, {'id': 99}]
        for change in changes:
            with self.subTest(change=change):
                fixture = self.fixture();fixture['/episode/1/info']['data'].update(change)
                result, calls = self.capture(fixture)
                self.assertEqual(result['state'], 'held')
                self.assertEqual(calls, [('GET', '/who-am-i'), ('GET', '/episode/1/info')])
        fixture=self.fixture();del fixture['/episode/1/info']['data']['closing_date']
        self.assertEqual(self.capture(fixture)[0]['code'], 'authorization_unknown')

    def test_signed_url_duplicates_external_assets_and_missing_scope_are_held(self):
        for reader in ('<script>var seriesId=2;</script><div id="episode-2"></div>',
            '<script>var seriesId=2;</script><div id="episode-1"><img class="content__img" src="https://evil.example/pc/a.jpg"></div>',
            '<script>var seriesId=2;</script><div id="episode-1"><img class="content__img" src="https://us-a.tapas.io/pc/a.jpg?x=1">'
            '<img class="content__img" src="https://us-a.tapas.io/pc/a.jpg?x=2"></div>'):
            fixture=self.fixture();fixture['/episode/1']=reader
            result,calls=self.capture(fixture)
            self.assertEqual(result['state'], 'held');self.assertNotIn(('GET','/series/2'),calls)

    def test_missing_identity_and_source_refusal_stop_without_reader(self):
        for response in (403, {'data': {'user': None}}, {'data': {'user': {'id': '1'}}}):
            fixture=self.fixture();fixture['/who-am-i']=response
            result,calls=self.capture(fixture)
            self.assertEqual(result['state'], 'held');self.assertEqual(calls,[('GET','/who-am-i')])
            self.assertNotIn('synthetic refusal', json.dumps(result))

    def test_series_mismatch_is_held_without_image_requests(self):
        fixture=self.fixture();fixture['/series/2']['data']['id']=3
        result,calls=self.capture(fixture)
        self.assertEqual(result['code'],'series_unverified')
        self.assertFalse(any(path.startswith('/pc/') for method,path in calls))
