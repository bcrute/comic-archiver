"""The Android transport seam against synthetic stores and clients: no network, no device."""
import json
import unittest
from unittest.mock import patch

from comic_archive.models import ArchiveError
from comic_archive.android_transport import AndroidTransport, WebViewCookieSource


class Headers(dict):
    """httpx-shaped headers: plain mapping plus the Set-Cookie list the transport reads."""

    def __init__(self, values=None, set_cookies=()):
        super().__init__(values or {})
        self._set_cookies = list(set_cookies)

    def get_list(self, name):
        return list(self._set_cookies) if name.lower() == 'set-cookie' else []


class Response:
    def __init__(self, code=200, data=b"image", headers=None, set_cookies=()):
        self.status_code, self.data = code, data
        self.headers = Headers(headers, set_cookies)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_bytes(self):
        yield self.data


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def stream(self, method, url, headers):
        self.calls.append((url, headers))
        return next(self.responses)

    def close(self):
        pass


class Store:
    """A stand-in for the native WebViewCookieStore: records what it was asked, never logs."""

    def __init__(self, headers=None):
        self.headers = dict(headers or {})
        self.asked, self.given = [], []

    def cookie_header(self, url):
        self.asked.append(url)
        return self.headers.get(url, '')

    def set_session_cookies(self, url, values):
        self.given.append((url, tuple(values)))
        return True


def transport(store, client, **kwargs):
    return AndroidTransport(WebViewCookieSource(store), interval=0.25, client=client, **kwargs)


class SourceTests(unittest.TestCase):
    def test_the_bridge_is_validated_without_echoing_its_failure(self):
        for bridge in (object(), type('Half', (), {'cookie_header': lambda self, url: ''})()):
            with self.assertRaises(ArchiveError) as error:
                WebViewCookieSource(bridge)
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_a_failing_or_unusable_store_answer_is_replaced_by_a_safe_fixed_message(self):
        for answer in (RuntimeError('value=PRIVATE'), 7, 'fine\r\nHost: evil', 'v\0'):
            store = Store()
            if isinstance(answer, BaseException):
                def fail(url, error=answer):
                    raise error
                store.cookie_header = fail
            else:
                store.cookie_header = lambda url, value=answer: value
            with self.assertRaises(ArchiveError) as error:
                transport(store, Client([])).get('https://tapas.io/who-am-i')
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_raw_set_cookie_strings_pass_through_unchanged_and_refusals_are_quiet(self):
        raw = 'sid=NEW; Path=/reader; Secure; HttpOnly'
        store = Store()

        def refuse(url, values):
            store.given.append((url, tuple(values)))
            return False
        store.set_session_cookies = refuse
        source = WebViewCookieSource(store)
        self.assertFalse(source.set_session_cookies('https://tapas.io/x', [raw]))
        self.assertEqual(store.given, [('https://tapas.io/x', (raw,))])
        self.assertTrue(source.set_session_cookies('https://tapas.io/x', []))


class AndroidTransportTests(unittest.TestCase):
    @patch('comic_archive.transport.time.sleep')
    def test_the_cookie_header_is_asked_live_per_request_with_the_full_url(self, sleep):
        store = Store({'https://tapas.io/a': 'session=ONE', 'https://tapas.io/b': 'session=TWO'})
        t = transport(store, Client([Response(), Response()]))
        t.get('https://tapas.io/a')
        t.get('https://tapas.io/b')
        self.assertEqual(store.asked, ['https://tapas.io/a', 'https://tapas.io/b'])
        self.assertEqual([call[1]['Cookie'] for call in t.client.calls], ['session=ONE', 'session=TWO'])
        # No jar was built: the transport holds nothing the store did not answer itself.
        self.assertEqual(t.cookies, ())

    @patch('comic_archive.transport.time.sleep')
    def test_no_cookie_and_no_question_for_a_cdn_and_nothing_reaches_another_origin(self, sleep):
        store = Store({'https://cdn.tapas.io/x': 'session=PRIVATE'})
        t = transport(store, Client([Response()]))
        t.get('https://cdn.tapas.io/x')
        self.assertNotIn('Cookie', t.client.calls[0][1])
        self.assertEqual(store.asked, [])
        for url in ('https://evil.test/a', 'http://tapas.io/a'):
            with self.assertRaises(ArchiveError):
                t.get(url)
        self.assertEqual(len(t.client.calls), 1)

    @patch('comic_archive.transport.time.sleep')
    def test_a_redirect_hop_is_a_fresh_url_scoped_question_and_carries_nothing_over(self, sleep):
        store = Store({'https://tapas.io/hop': 'session=FIRST'})
        client = Client([Response(302), Response()])
        t = transport(store, client)
        with self.assertRaises(ArchiveError):
            t.get('https://tapas.io/hop')  # The site's own redirect is followed by the caller.
        t.get('https://cdn.tapas.io/landing')
        self.assertEqual(store.asked, ['https://tapas.io/hop'])
        self.assertNotIn('Cookie', client.calls[1][1])

    @patch('comic_archive.transport.time.sleep')
    def test_each_retry_rereads_the_store_because_a_refusal_may_have_rotated_the_session(self, sleep):
        store = Store({'https://tapas.io/a': 'session=ROTATED'})
        t = transport(store, Client([Response(503), Response()]))
        self.assertEqual(t.get('https://tapas.io/a'), b'image')
        self.assertEqual(store.asked, ['https://tapas.io/a', 'https://tapas.io/a'])
        self.assertEqual([call[1]['Cookie'] for call in t.client.calls[-1:]], ['session=ROTATED'])

    @patch('comic_archive.transport.time.sleep')
    def test_the_site_key_travels_only_to_the_site_itself(self, sleep):
        t = transport(Store(), Client([Response()]))
        t.get('https://tapas.io/api', authorization='key=PRIVATE')
        self.assertEqual(t.client.calls[0][1]['Authorization'], 'key=PRIVATE')
        for url in ('https://cdn.tapas.io/x',):
            with self.assertRaises(ArchiveError) as error:
                t.get(url, authorization='key=PRIVATE')
            self.assertNotIn('PRIVATE', str(error.exception))
        for value in ('key\nx', 'key\x7f'):
            with self.assertRaises(ArchiveError):
                t.get('https://tapas.io/api', authorization=value)

    @patch('comic_archive.transport.time.sleep')
    def test_response_set_cookie_strings_reach_the_store_verbatim_and_only_from_cookie_hosts(self, sleep):
        raw = 'sid=NEW; Domain=tapas.io; Path=/; Secure; Expires=Wed, 21 Oct 2026 12:00:00 GMT'
        store = Store()
        t = transport(store, Client([Response(set_cookies=[raw]), Response(set_cookies=['cdn=1'])]))
        t.get('https://tapas.io/reader', take_cookies=True)
        t.get('https://cdn.tapas.io/img', take_cookies=True)
        self.assertEqual(store.given, [('https://tapas.io/reader', (raw,))])
        self.assertEqual(t.cookies, ())  # The transport never becomes the jar; the store stays the authority.

    @patch('comic_archive.transport.time.sleep')
    def test_set_cookie_observed_before_a_later_budget_failure_still_reaches_the_store(self, sleep):
        from comic_archive.errors import RunLimit
        store = Store()
        t = transport(store, Client([Response(data=b'PRIVATE_BYTES', set_cookies=['sid=NEW'])]))
        with self.assertRaises(RunLimit):
            t.get('https://tapas.io/reader', max_bytes=3, take_cookies=True)
        self.assertEqual(store.given, [('https://tapas.io/reader', ('sid=NEW',))])

    def test_inherited_gates_stay_binding(self):
        from comic_archive.errors import RunLimit
        t = transport(Store(), Client([Response()]))
        self.assertEqual(t.get('https://tapas.io/a'), b'image')
        for max_bytes in (0, 'x'):
            with self.assertRaises(RunLimit):
                t.get('https://tapas.io/a', max_bytes=max_bytes)
        for interval in (0.24, float('nan'), True):
            with self.assertRaises(ArchiveError):
                AndroidTransport(WebViewCookieSource(Store()), interval=interval, client=Client([]))
        for source in (object(), None):
            with self.assertRaises(ArchiveError):
                AndroidTransport(source, client=Client([]))

    @patch('comic_archive.transport.time.sleep')
    def test_the_tapas_adapter_runs_on_the_injected_transport_without_a_session_file(self, sleep):
        from comic_archive.adapters.tapas_http import TapasHTTPAdapter
        body = json.dumps({'data': {'user': {'id': 7}}}).encode()
        client = Client([Response(data=body), Response(data=body)])
        with TapasHTTPAdapter(transport=transport(Store({'https://tapas.io/who-am-i': 'session=PRIVATE'}),
                                                 client)) as adapter:
            self.assertTrue(adapter.check_session())
            self.assertEqual(adapter.account_identity(), '7')
        self.assertEqual(client.calls[0][0], 'https://tapas.io/who-am-i')
        self.assertEqual(client.calls[0][1]['Cookie'], 'session=PRIVATE')


if __name__ == '__main__':
    unittest.main()
