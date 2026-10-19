import json
import tempfile
import time
import io
import logging
import unittest
from pathlib import Path
from unittest.mock import patch
from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive.models import ArchiveError
from comic_archive.session import cookie_expiry_seconds, load_session, save_session
from comic_archive.transport import Transport, ExpiredAsset, asset_url


class Response:
    def __init__(self, code=200, data=b"image", headers=None):
        self.status_code, self.data, self.headers = code, data, headers or {}

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


class TransportTests(unittest.TestCase):
    @patch('comic_archive.transport.time.sleep')
    def test_rate_limit_stops_other_threads_from_starting_or_retrying_requests(self, sleep):
        client = Client([Response(429), Response()])
        transport = Transport(client=client)
        with self.assertRaises(ArchiveError):
            transport.get('https://cdn.tapas.io/pc/first.png')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(transport.get, 'https://cdn.tapas.io/pc/second.png')
            with self.assertRaisesRegex(ArchiveError, 'rate limit'):
                future.result()
        self.assertEqual(len(client.calls), 1)

    def test_two_transfers_overlap_but_share_request_spacing_and_private_responses(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)
        starts = []
        class ParallelResponse(Response):
            def iter_bytes(inner):
                barrier.wait(4)
                yield inner.data
        class ParallelClient:
            def stream(inner, method, url, headers):
                from urllib.parse import urlsplit
                starts.append(time.monotonic())
                self.assertNotIn('Cookie', headers)
                return ParallelResponse(data=b'image', headers={'Content-Type': 'image/' + urlsplit(url).path.rsplit('/', 1)[1]})
        transport = Transport([{'domain': 'tapas.io', 'name': 'session', 'value': 'PRIVATE'}],
                              interval=0.5, client=ParallelClient())
        def retrieve(kind):
            data = transport.get('https://cdn.tapas.io/pc/' + kind + '?signature=PRIVATE')
            return data, transport.last_response['content_type']
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(retrieve, 'png')
            b = pool.submit(retrieve, 'jpeg')
            self.assertEqual(a.result(), (b'image', 'image/png'))
            self.assertEqual(b.result(), (b'image', 'image/jpeg'))
        self.assertGreaterEqual(starts[1] - starts[0], 0.45)
        self.assertIsNone(transport.last_response)

    @patch('comic_archive.transport.time.sleep')
    def test_verbose_host_logging_cannot_emit_signed_urls_or_response_cookies(self, sleep):
        import httpx
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        http_logger = logging.getLogger('httpx')
        core_logger = logging.getLogger('httpcore.http11')
        loggers = (root, http_logger, core_logger)
        original = [(logger.level, list(logger.filters)) for logger in loggers]
        root.addHandler(handler)
        http_logger.addHandler(handler)
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            headers={'Set-Cookie':'SESSION_PRIVATE'},content=b'original image')))
        try:
            transport = Transport(client=client)
            self.assertEqual(transport.get('https://us-a.tapas.io/pc/page.jpg?signature=SIGNED_PRIVATE'),b'original image')
            # A configured child handler/level must not bypass its privacy filter.
            core_logger.addHandler(handler)
            core_logger.setLevel(logging.DEBUG)
            core_logger.debug('receive_response_headers.complete Set-Cookie=SESSION_PRIVATE')
            logging.getLogger('comic_archive.jobs').info('Useful archive progress')
            self.assertIn('Useful archive progress',output.getvalue())
            self.assertNotIn('PRIVATE',output.getvalue())
            self.assertNotIn('HTTP Request',output.getvalue())
        finally:
            client.close()
            for logger,(level,filters) in zip(loggers,original,strict=True):
                logger.removeHandler(handler)
                logger.setLevel(level)
                logger.filters[:] = filters

    @patch("comic_archive.transport.time.sleep")
    def test_cookie_boundary_and_redirects(self, sleep):
        client = Client([Response(), Response(), Response(302)])
        t = Transport([{"domain": ".tapas.io", "name": "session", "value": "SECRET"}], client=client)
        t.get("https://tapas.io/image")
        t.get("https://cdn.tapas.io/image?signature=PRIVATE")
        self.assertIn("Cookie", client.calls[0][1])
        self.assertNotIn("Cookie", client.calls[1][1])
        with self.assertRaises(ArchiveError):
            t.get("https://cdn.tapas.io/redirect")

    @patch("comic_archive.transport.time.sleep")
    def test_expiry_and_backoff(self, sleep):
        client = Client([Response(503, headers={"Retry-After": "4"}), Response()])
        self.assertEqual(Transport(client=client).get("https://cdn.tapas.io/a"), b"image")
        sleep.assert_any_call(4)
        with self.assertRaises(ExpiredAsset):
            Transport(client=Client([Response(403)])).get("https://cdn.tapas.io/a")

    @patch('comic_archive.transport.time.sleep')
    def test_network_failure_is_typed_and_private_while_unknown_errors_and_rate_limits_stop(self,sleep):
        import httpx
        from comic_archive.errors import NetworkUnavailable
        client=Client([])
        def fail(*args,**kwargs):
            client.calls.append(None)
            raise httpx.ConnectError('PRIVATE_COOKIE https://cdn.tapas.io/a?signature=PRIVATE')
        client.stream=fail
        with self.assertRaises(NetworkUnavailable) as error:
            Transport(client=client).get('https://cdn.tapas.io/a?signature=PRIVATE')
        self.assertEqual(len(client.calls),3)
        self.assertNotIn('PRIVATE',str(error.exception))
        client.calls.clear()
        def invalid(*args,**kwargs):
            client.calls.append(None)
            raise ValueError('PRIVATE_BAD_RESPONSE')
        client.stream=invalid
        with self.assertRaises(ArchiveError) as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertNotIsInstance(error.exception,NetworkUnavailable)
        self.assertNotIn('PRIVATE',str(error.exception))
        self.assertEqual(len(client.calls),1)
        client=Client([Response(429,headers={'Retry-After':'1'}),Response()])
        with self.assertRaisesRegex(ArchiveError,'rate limit') as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertNotIsInstance(error.exception,NetworkUnavailable)
        self.assertEqual(len(client.calls),1)

    @patch('comic_archive.transport.time.sleep')
    def test_exhausted_gateway_failure_is_typed_without_retrying_a_long_server_pause(self,sleep):
        from comic_archive.errors import NetworkUnavailable
        client=Client([Response(503),Response(503),Response(503)])
        with self.assertRaises(NetworkUnavailable) as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertEqual(error.exception.source_http_status,503)
        self.assertEqual(len(client.calls),3)
        client=Client([Response(503,headers={'Retry-After':'120'})])
        with self.assertRaises(NetworkUnavailable) as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertEqual(error.exception.retry_after_seconds,120)
        self.assertEqual(len(client.calls),1)

    @patch("comic_archive.transport.time.sleep")
    def test_streaming_byte_budget_stops_without_retrying_or_leaking_response(self, sleep):
        from comic_archive.errors import RunLimit
        client = Client([Response(data=b"PRIVATE_BYTES")])
        with self.assertRaises(RunLimit) as error:
            Transport(client=client).get("https://cdn.tapas.io/a?signature=PRIVATE", max_bytes=3)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(len(client.calls), 1)

    def test_untrusted_hosts(self):
        for url in ["http://cdn.tapas.io/x", "https://tapas.io.evil.test/x", "https://127.0.0.1/x", "https://user:pass@tapas.io/x", "file:///etc/passwd"]:
            with self.assertRaises(ArchiveError):
                asset_url(url)

    def test_request_interval_bounds_reject_nan_infinity_and_boolean(self):
        for interval in (0.24, float('nan'), float('inf'), True, 61):
            with self.assertRaises(ArchiveError):
                Transport(interval=interval, client=Client([]))
        self.assertEqual(Transport(interval=0.5, client=Client([])).interval, 0.5)
        self.assertEqual(Transport(interval=0.25, client=Client([])).interval, 0.25)

    def test_observed_tapas_asset_host_and_sanitized_fixture(self):
        file = Path(__file__).parent / "fixtures/tapas-public-observation.json"
        observation = json.loads(file.read_text())
        for sample in observation["sampled_asset_paths"]:
            url = f"https://{observation['asset_host']}{sample['path']}"
            self.assertEqual(asset_url(url), url)
            self.assertNotIn("?", sample["path"])
        self.assertFalse(observation["authenticated"])
        self.assertFalse(observation["image_bytes_retrieved"])

    def test_private_session_only_tapas(self):
        state = {"cookies": [
            {"domain": ".tapas.io", "name": "session", "value": "PRIVATE"},
            {"domain": ".google.com", "name": "oauth", "value": "OTHER"}],
            "origins": [{"origin": "https://tapas.io", "localStorage": []}, {"origin": "https://accounts.google.com", "localStorage": []}]}
        class Context:
            def storage_state(self):
                return state
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "session"
            save_session(Context(), folder)
            file = folder / "state.json"
            self.assertEqual(file.stat().st_mode & 0o777, PRIVATE_FILE)
            self.assertEqual(folder.stat().st_mode & 0o777, PRIVATE_FOLDER)
            stored = json.loads(file.read_text())
            self.assertEqual(len(stored["cookies"]), 1)
            self.assertEqual(len(stored["origins"]), 1)

    def test_cookie_expiry_normalization(self):
        self.assertEqual(cookie_expiry_seconds(1790893649525), 1790893649.525)
        self.assertEqual(cookie_expiry_seconds(1790893649), 1790893649)
        self.assertEqual(cookie_expiry_seconds(0), -1)
        self.assertEqual(cookie_expiry_seconds(-1), -1)
        for expiry in [float("nan"), float("inf"), "PRIVATE", -20, 10 ** 18]:
            with self.assertRaises(ArchiveError):
                cookie_expiry_seconds(expiry)

    def test_old_saved_state_is_normalized_without_echoing_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "state.json"
            file.write_text(json.dumps({"cookies": [{"name": "session", "value": "PRIVATE", "expires": 1790893649525}], "origins": []}))
            state = load_session(Path(root))
            self.assertEqual(state["cookies"][0]["expires"], 1790893649.525)
            self.assertEqual(state["cookies"][0]["value"], "PRIVATE")


if __name__ == "__main__":
    unittest.main()
