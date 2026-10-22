"""The Android bridge/adapter against synthetic native seam objects: no network, no device.

These check only what this line owns — WebViewCookieSource's bridge validation and
sanitization, and AndroidTransport's thin wiring. The canonical request behaviour of the
cookie_source/cookie_sink seams themselves (per-request and per-attempt asking, CDN and
cross-origin isolation, raw sink forwarding) is covered by tests/test_android_seams.py on
the merged core; these tests need that merged core (Transport's cookie_source/cookie_sink
constructor seams) to run.
"""
import json
import unittest

import httpx

from comic_archive.models import ArchiveError
from comic_archive.android_transport import AndroidTransport, WebViewCookieSource


class CloseableClient:
    """The least client seam: Transport.close() calls client.close(), nothing else."""

    def close(self):
        pass


class Store:
    """A stand-in for the native WebViewCookieStore: records what it was asked, never logs."""

    def __init__(self, headers=None):
        self.headers = dict(headers or {})
        self.asked, self.given = [], []

    def headerFor(self, url):
        self.asked.append(url)
        return self.headers.get(url)

    def apply(self, url, values):
        self.given.append((url, tuple(values)))


class Capturing(AndroidTransport):
    """The real adapter with the network call replaced; the Cookie header recorded per URL."""

    def __init__(self, store, **kwargs):
        super().__init__(store, interval=1.0, client=CloseableClient(), **kwargs)
        self.interval = 0.0
        self.sent = {}

    def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
        self.sent[url] = headers.get("Cookie")
        return b"golden"


class WebViewCookieSourceTests(unittest.TestCase):
    def test_the_bridge_is_validated_without_echoing_its_failure(self):
        for bridge in (object(), type('Half', (), {'headerFor': lambda self, url: None})()):
            with self.assertRaises(ArchiveError) as error:
                WebViewCookieSource(bridge)
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_the_chaquopy_snake_case_name_is_accepted_too(self):
        # Chaquopy exposes the same Kotlin method under camelCase and snake_case; the seam
        # takes either spelling of headerFor so the bridge wiring cannot fail on naming alone.
        for header_name in ('headerFor', 'header_for'):
            # Plain functions in a type() dict become bound methods (self arrives as the
            # first argument); staticmethod keeps the store-shaped signatures.
            members = {header_name: staticmethod(lambda url: None),
                       'apply': staticmethod(lambda url, values: None)}
            self.assertEqual(WebViewCookieSource(type('Bridge', (), members)())
                             .header_for('https://tapas.io/x'), '')
        with self.assertRaises(ArchiveError):
            WebViewCookieSource(type('NoHeader', (), {'apply': staticmethod(lambda url, values: None)})())

    def test_a_failing_or_unusable_header_answer_is_replaced_by_a_safe_fixed_message(self):
        for answer in (RuntimeError('value=PRIVATE'), 7, 'fine\r\nHost: evil', 'v\0'):
            store = Store()
            if isinstance(answer, BaseException):
                def fail(url, error=answer):
                    raise error
                store.headerFor = fail
            else:
                store.headerFor = lambda url, value=answer: value
            with self.assertRaises(ArchiveError) as error:
                WebViewCookieSource(store).header_for('https://tapas.io/x')
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_no_answer_is_an_empty_header_and_raw_values_pass_through_unchanged(self):
        store = Store()
        source = WebViewCookieSource(store)
        self.assertEqual(source.header_for('https://tapas.io/x'), '')
        raw = ('sid=NEW; Path=/reader; Secure; HttpOnly', 'other=3; Max-Age=0')
        source.apply('https://tapas.io/reader', list(raw))
        self.assertEqual(store.given, [('https://tapas.io/reader', raw)])
        source.apply('https://tapas.io/reader', [])  # Nothing to take is not a store question.
        self.assertEqual(len(store.given), 1)

    def test_a_store_that_cannot_take_values_is_left_as_it_is(self):
        store = Store()

        def refuse(url, values):
            raise RuntimeError('PRIVATE_COOKIE')
        store.apply = refuse
        WebViewCookieSource(store).apply('https://tapas.io/reader', ['sid=NEW'])  # No raise.


class AndroidTransportTests(unittest.TestCase):
    def test_real_request_path_uses_mobile_session_and_actual_webview_agent(self):
        agent = 'Actual WebView/154 Mobile'
        store = Store({'https://m.tapas.io/who-am-i': 'session=GOLDEN'})
        store.browserUserAgent = lambda: agent
        seen = []

        def answer(request):
            seen.append(request)
            # The observed phone contract: the bare client is refused; the
            # browser agent on canonical redirects. Only the mobile request
            # using its own live session verifies the account.
            if request.headers.get('User-Agent') != agent:
                return httpx.Response(403)
            if request.url.host == 'tapas.io':
                return httpx.Response(302, headers={'Location': 'https://m.tapas.io/who-am-i'})
            self.assertEqual(request.url.host, 'm.tapas.io')
            self.assertEqual(request.headers.get('Cookie'), 'session=GOLDEN')
            self.assertEqual(request.headers.get('Referer'), 'https://m.tapas.io/')
            return httpx.Response(200, json={'data': {'user': {'id': 7}}})

        from comic_archive.android_adapter import AndroidTapasAdapter
        client = httpx.Client(transport=httpx.MockTransport(answer), trust_env=False)
        with AndroidTapasAdapter(transport=AndroidTransport(store, client=client)) as adapter:
            self.assertTrue(adapter._http.check_session())
        self.assertEqual(len(seen), 2)  # adapter entry and explicit verification
        self.assertEqual(store.asked, ['https://m.tapas.io/who-am-i'] * 2)

    def test_routing_validates_before_reading_any_session_header(self):
        store = Store()
        transport = Capturing(store)
        for url in ('http://tapas.io/who-am-i', 'https://tapas.io:444/who-am-i',
                    'https://tapas.io.evil.test/who-am-i', 'https://m.tapas.io.evil.test/'):
            with self.assertRaises(ArchiveError):
                transport.get(url)
        self.assertEqual(store.asked, [])
        self.assertEqual(transport.sent, {})

    def test_unusable_browser_agent_never_crosses_into_request_headers(self):
        for agent in ('bad\r\nCookie: PRIVATE', 42, ''):
            store = Store()
            store.browserUserAgent = lambda value=agent: value
            with self.assertRaises(ArchiveError) as error:
                AndroidTransport(store, client=CloseableClient())
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_the_adapter_wires_the_store_in_and_keeps_no_jar(self):
        store = Store({'https://tapas.io/x': 'session=GOLDEN'})
        transport = AndroidTransport(store, interval=0.25, client=CloseableClient())
        source = transport.store
        # Bound methods do not have stable identity across accesses; same underlying
        # function on the same instance is what "wired to the store's own seams" means.
        self.assertEqual(transport.cookie_source, source.header_for)
        self.assertEqual(transport.cookie_sink, source.apply)
        self.assertEqual(transport.cookies, ())
        # A raw bridge is wrapped; a wrapped source passes through unchanged.
        self.assertIsInstance(AndroidTransport(Store(), client=CloseableClient()).store, WebViewCookieSource)
        self.assertIs(AndroidTransport(source, client=CloseableClient()).store, source)

    def test_inherited_gates_stay_binding(self):
        for interval in (0.24, float('nan'), True):
            with self.assertRaises(ArchiveError):
                AndroidTransport(Store(), interval=interval, client=CloseableClient())
        for bridge in (object(), None):
            with self.assertRaises(ArchiveError):
                AndroidTransport(bridge, client=CloseableClient())

    def test_the_live_header_travels_to_the_site_host_only_through_the_canonical_path(self):
        store = Store({'https://m.tapas.io/who-am-i': 'session=GOLDEN'})
        transport = Capturing(store)
        self.assertEqual(transport.get('https://tapas.io/who-am-i'), b'golden')
        self.assertEqual(transport.get('https://us-a.tapas.io/pc/golden/0001.png'), b'golden')
        self.assertEqual(store.asked, ['https://m.tapas.io/who-am-i'])
        self.assertEqual(transport.sent, {'https://m.tapas.io/who-am-i': 'session=GOLDEN',
                                          'https://us-a.tapas.io/pc/golden/0001.png': None})

    def test_the_tapas_adapter_accepts_the_injected_transport_without_a_session_file(self):
        from comic_archive.adapters.tapas_http import TapasHTTPAdapter
        body = json.dumps({'data': {'user': {'id': 7}}}).encode()

        class WhoAmI(Capturing):
            def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
                self.sent[url] = headers.get("Cookie")
                return body

        transport = WhoAmI(Store({'https://m.tapas.io/who-am-i': 'session=GOLDEN'}))
        with TapasHTTPAdapter(transport=transport) as adapter:
            self.assertTrue(adapter.check_session())
            self.assertEqual(adapter.account_identity(), '7')
        self.assertEqual(transport.sent['https://m.tapas.io/who-am-i'], 'session=GOLDEN')


if __name__ == '__main__':
    unittest.main()


class MobileJsonModeTests(unittest.TestCase):
    def test_numeric_api_requests_use_observed_xhr_mode_without_changing_browser_identity(self):
        import httpx
        requests = []

        class Store:
            def browserUserAgent(self):
                return 'Actual WebView Agent'

            def headerFor(self, url):
                return 'session=fixture'

            def apply(self, url, values):
                pass

        def respond(request):
            requests.append(request)
            if request.url.path == '/series/1001' and request.headers.get('X-Requested-With') != 'XMLHttpRequest':
                return httpx.Response(302, headers={'Location': '/series/fixture/info'})
            return httpx.Response(200, json={'data': {'id': 1001}})

        from comic_archive.android_transport import AndroidTransport
        client = httpx.Client(transport=httpx.MockTransport(respond))
        with client:
            transport = AndroidTransport(Store(), client=client)
            transport.interval = 0
            for path in ['/series/1001', '/series/1001/episodes?page=1&max_limit=20',
                         '/episode/9001/info', '/reading-list?category=SUBSCRIBED',
                         '/series/fixture/info', '/episode/9001']:
                transport.get('https://tapas.io' + path)
        self.assertTrue(all(r.headers['User-Agent'] == 'Actual WebView Agent' for r in requests))
        self.assertTrue(all(r.headers.get('X-Requested-With') == 'XMLHttpRequest' for r in requests[:3]))
        self.assertTrue(all('X-Requested-With' not in r.headers for r in requests[3:]))
        self.assertTrue(all(r.url.host == 'm.tapas.io' for r in requests))
