"""Synthetic checks for the Android seams: live cookie sourcing and the metering probe."""
import json
import tempfile
import unittest
from pathlib import Path

from comic_archive import network_android
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport


class _HeaderCapture(Transport):
    """The real request path with the network call replaced; every request recorded."""

    def __init__(self, **kwargs):
        super().__init__(client=object(), **kwargs)
        self.interval = 0.0
        self.sent = {}

    def _request(self, url, headers, max_bytes, attempt, take_cookies=False):
        # One entry per request, not per URL: repeats and retries are what these tests observe.
        self.sent.setdefault(url, []).append(headers.get("Cookie"))
        return b"golden"


class LiveCookieSourceTests(unittest.TestCase):
    def test_the_source_is_asked_per_request_with_the_exact_url_and_never_for_a_cdn(self):
        asked = []
        transport = _HeaderCapture(cookie_source=lambda url: asked.append(url) or "golden-session=1")
        self.assertEqual(transport.get("https://tapas.io/series/7"), b"golden")
        self.assertEqual(transport.get("https://us-a.tapas.io/pc/golden/0001.png"), b"golden")
        # The site's own host got the live header; the CDN host got none and was never asked.
        self.assertEqual(asked, ["https://tapas.io/series/7"])
        self.assertEqual(transport.sent, {"https://tapas.io/series/7": ["golden-session=1"],
                                          "https://us-a.tapas.io/pc/golden/0001.png": [None]})

    def test_each_request_sees_the_current_answer_and_none_sends_no_header(self):
        answers = iter(["golden-session=1", None])
        transport = _HeaderCapture(cookie_source=lambda url: next(answers))
        transport.get("https://tapas.io/who-am-i")
        transport.get("https://tapas.io/who-am-i")
        # Both requests hit the same URL: one key, two entries, in order.
        self.assertEqual(transport.sent["https://tapas.io/who-am-i"],
                         ["golden-session=1", None])

    def test_a_source_that_returns_something_else_than_text_sends_no_header(self):
        transport = _HeaderCapture(cookie_source=lambda url: 7)
        transport.get("https://tapas.io/episode/81")
        self.assertEqual(transport.sent, {"https://tapas.io/episode/81": [None]})

    def test_a_source_must_be_callable_and_the_jar_path_is_unchanged_without_one(self):
        with self.assertRaises(ArchiveError):
            Transport(client=object(), cookie_source="golden-session=1")
        transport = _HeaderCapture()
        transport.get("https://tapas.io/series/7")
        self.assertEqual(transport.sent, {"https://tapas.io/series/7": [None]})

    def test_a_retry_asks_the_source_again_after_admission_instead_of_reusing_the_header(self):
        from comic_archive.transport import _RetryRequest
        answers = iter(["golden-session=1", "golden-session=2"])
        transport = _HeaderCapture(cookie_source=lambda url: next(answers))
        recorded = transport._request

        def flaky(url, headers, max_bytes, attempt, take_cookies=False):
            if attempt == 0:
                recorded(url, headers, max_bytes, attempt, take_cookies)
                raise _RetryRequest(0.0)
            return recorded(url, headers, max_bytes, attempt, take_cookies)

        transport._request = flaky
        self.assertEqual(transport.get("https://tapas.io/who-am-i"), b"golden")
        self.assertEqual(transport.sent["https://tapas.io/who-am-i"],
                         ["golden-session=1", "golden-session=2"])


class PlatformCookieSinkTests(unittest.TestCase):
    SET_COOKIE = ["golden-session=2; Path=/; Secure", "other=3; Max-Age=0"]

    def _transport(self, sink):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status_code = 200
        response.headers.get.side_effect = lambda key, default="": default
        response.headers.get_list.side_effect = lambda key: self.SET_COOKIE if key == "set-cookie" else []
        response.iter_bytes.return_value = iter([b"golden"])
        client = MagicMock()
        client.stream.return_value.__enter__.return_value = response
        client.stream.return_value.__exit__.return_value = False
        return Transport(client=client, cookie_source=lambda request_url: "golden-session=1", cookie_sink=sink)

    def test_own_origin_set_cookie_values_reach_the_sink_raw_and_the_jar_stays_empty(self):
        seen = []
        transport = self._transport(lambda url, values: seen.append((url, tuple(values))))
        self.assertEqual(transport.get("https://tapas.io/who-am-i", take_cookies=True), b"golden")
        self.assertEqual(seen, [("https://tapas.io/who-am-i", tuple(self.SET_COOKIE))])
        self.assertEqual(transport.cookies, ())

    def test_cdn_and_other_origins_never_reach_the_sink(self):
        seen = []
        transport = self._transport(lambda url, values: seen.append((url, tuple(values))))
        transport.get("https://us-a.tapas.io/pc/golden/0001.png", take_cookies=True)
        self.assertEqual(seen, [])

    def test_a_sink_must_be_callable(self):
        with self.assertRaises(ArchiveError):
            Transport(client=object(), cookie_sink="set-cookie")


class AndroidMeteringProbeTests(unittest.TestCase):
    def setUp(self):
        # The probe is process-global state: reset before and after, or a configured file
        # here changes network.probe() for every later test in the same run.
        network_android.configure(None)
        self.addCleanup(network_android.configure, None)

    def test_an_unconfigured_or_unusable_status_file_is_unknown_which_holds(self):
        network_android.configure(None)
        self.assertEqual(network_android.probe(), {"connection": "unknown", "metered": None})
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "netstatus.json"
            network_android.configure(status)
            for broken in (None, json.dumps({"connection": "subway", "metered": False}),
                           json.dumps({"connection": "wifi", "metered": "no"})):
                if broken is None:
                    status.unlink(missing_ok=True)  # The first pass removes a file never written.
                else:
                    status.write_text(broken)
                self.assertEqual(network_android.probe(), {"connection": "unknown", "metered": None})

    def test_the_shapes_the_service_may_write_map_onto_the_probe_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "netstatus.json"
            network_android.configure(status)
            for content in ({"connection": "wifi", "metered": False},
                            {"connection": "cellular", "metered": True}):
                status.write_text(json.dumps(content))
                observed = network_android.probe()
                self.assertEqual(observed["connection"], content["connection"])
                self.assertIs(observed["metered"], content["metered"])
