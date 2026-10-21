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

from comic_archive.models import ArchiveError
from comic_archive.android_transport import AndroidTransport, WebViewCookieSource


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
        super().__init__(store, interval=1.0, client=object(), **kwargs)
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
            members = {header_name: lambda url: None,
                       'apply': lambda url, values: None}
            self.assertEqual(WebViewCookieSource(type('Bridge', (), members)())
                             .header_for('https://tapas.io/x'), '')
        with self.assertRaises(ArchiveError):
            WebViewCookieSource(type('NoHeader', (), {'apply': lambda url, values: None})())

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
    def test_the_adapter_wires_the_store_in_and_keeps_no_jar(self):
        store = Store({'https://tapas.io/x': 'session=GOLDEN'})
        transport = AndroidTransport(store, interval=0.25, client=object())
        source = transport.store
        self.assertIs(transport.cookie_source, source.header_for)
        self.assertIs(transport.cookie_sink, source.apply)
        self.assertEqual(transport.cookies, ())
        # A raw bridge is wrapped; a wrapped source passes through unchanged.
        self.assertIsInstance(AndroidTransport(Store(), client=object()).store, WebViewCookieSource)
        self.assertIs(AndroidTransport(source, client=object()).store, source)

    def test_inherited_gates_stay_binding(self):
        for interval in (0.24, float('nan'), True):
            with self.assertRaises(ArchiveError):
                AndroidTransport(Store(), interval=interval, client=object())
        for bridge in (object(), None):
            with self.assertRaises(ArchiveError):
                AndroidTransport(bridge, client=object())

    def test_the_live_header_travels_to_the_site_host_only_through_the_canonical_path(self):
        store = Store({'https://tapas.io/who-am-i': 'session=GOLDEN'})
        transport = Capturing(store)
        self.assertEqual(transport.get('https://tapas.io/who-am-i'), b'golden')
        self.assertEqual(transport.get('https://us-a.tapas.io/pc/golden/0001.png'), b'golden')
        self.assertEqual(store.asked, ['https://tapas.io/who-am-i'])
        self.assertEqual(transport.sent, {'https://tapas.io/who-am-i': 'session=GOLDEN',
                                          'https://us-a.tapas.io/pc/golden/0001.png': None})

    def test_the_tapas_adapter_accepts_the_injected_transport_without_a_session_file(self):
        from comic_archive.adapters.tapas_http import TapasHTTPAdapter
        body = json.dumps({'data': {'user': {'id': 7}}}).encode()

        class WhoAmI(Capturing):
            def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
                self.sent[url] = headers.get("Cookie")
                return body

        transport = WhoAmI(Store({'https://tapas.io/who-am-i': 'session=GOLDEN'}))
        with TapasHTTPAdapter(transport=transport) as adapter:
            self.assertTrue(adapter.check_session())
            self.assertEqual(adapter.account_identity(), '7')
        self.assertEqual(transport.sent['https://tapas.io/who-am-i'], 'session=GOLDEN')


if __name__ == '__main__':
    unittest.main()
