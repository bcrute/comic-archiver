import unittest
from unittest.mock import patch

from test_transport_session import Client, Response
from comic_archive.errors import NetworkUnavailable, RunLimit
from comic_archive.models import ArchiveError
from comic_archive.transport import ExpiredAsset, Transport, asset_url


class Clock:
    """Stands in for the time module: sleeping only moves this clock forward."""
    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if self.on_sleep:
            self.on_sleep()
        self.now += seconds


class TransportLimitTests(unittest.TestCase):
    def test_httpx_cookie_jar_cannot_bypass_the_desktop_host_and_explicit_take_rules(self):
        import httpx
        seen = []

        def response(request):
            seen.append(request.headers.get('cookie'))
            return httpx.Response(200, content=b'fixture',
                headers={'set-cookie': 'sid=FIXTURE_ROTATED; Domain=tapas.io; Path=/; Secure'})

        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            transport = Transport(client=client, interval=0.25)
            transport.get('https://tapas.io/first')  # No take_cookies: ignored.
            transport.get('https://tapas.io/second', take_cookies=True)
            transport.get('https://tapas.io/third')
            transport.get('https://cdn.tapas.io/fixture.png')
            self.assertEqual(len(client.cookies), 0)
        self.assertEqual(seen, [None, None, 'sid=FIXTURE_ROTATED', None])

    @patch('comic_archive.transport.time.sleep')
    def test_a_byte_budget_that_is_spent_or_not_a_number_stops_before_any_request(self, sleep):
        client = Client([Response()])
        transport = Transport(client=client)
        for budget in (0, -5, True, 1.5, '10'):
            with self.subTest(budget=budget), self.assertRaises(RunLimit):
                transport.get('https://cdn.tapas.io/a', max_bytes=budget)
        self.assertEqual(client.calls, [])

    @patch('comic_archive.transport.time.sleep')
    def test_a_page_over_100_mib_is_refused_without_being_retried(self, sleep):
        chunk = b'\0' * (1024 * 1024)

        class Endless(Response):
            def iter_bytes(self):
                for _ in range(101):
                    yield chunk
        client = Client([Endless(), Response()])
        with self.assertRaisesRegex(ArchiveError, '100 MiB') as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertNotIsInstance(error.exception, RunLimit)
        self.assertEqual(len(client.calls), 1)

    @patch('comic_archive.transport.time.sleep')
    def test_a_server_error_that_never_clears_is_a_plain_failure_after_three_attempts(self, sleep):
        client = Client([Response(500), Response(500), Response(500), Response()])
        with self.assertRaises(ArchiveError) as error:
            Transport(client=client).get('https://cdn.tapas.io/a')
        self.assertNotIsInstance(error.exception, NetworkUnavailable)
        self.assertEqual(len(client.calls), 3)
        sleep.assert_any_call(2)
        sleep.assert_any_call(4)

    def test_the_job_deadline_cuts_a_long_server_pause_short_instead_of_sleeping_through_it(self):
        clock = Clock()
        client = Client([Response(503, headers={'Retry-After': '30'}), Response()])
        with patch('comic_archive.transport.time', clock):
            transport = Transport(client=client)
            transport.deadline = clock.now + 10
            with self.assertRaisesRegex(RunLimit, 'time budget'):
                transport.get('https://cdn.tapas.io/a')
        self.assertEqual(len(client.calls), 1)
        self.assertLess(clock.now, 1000 + 10 + 1)

    def test_a_retry_pause_the_deadline_allows_is_waited_out_and_the_retry_succeeds(self):
        clock = Clock()
        client = Client([Response(503, headers={'Retry-After': '4'}), Response()])
        with patch('comic_archive.transport.time', clock):
            transport = Transport(client=client)
            transport.deadline = clock.now + 100
            self.assertEqual(transport.get('https://cdn.tapas.io/a'), b'image')
        self.assertEqual(len(client.calls), 2)
        self.assertGreaterEqual(clock.now, 1004)

    def test_a_request_waiting_for_its_turn_does_not_start_once_another_hit_the_rate_limit(self):
        holder = []

        def another_request_is_told_to_slow_down():
            limit = ArchiveError('Tapas rate limit reached; this job has stopped.')
            limit.retry_after_seconds = 30
            holder[0]._rate_limit_error = limit
            holder[0]._rate_limited.set()

        clock = Clock(on_sleep=another_request_is_told_to_slow_down)
        client = Client([Response()])
        with patch('comic_archive.transport.time', clock):
            holder.append(Transport(client=client))
            with self.assertRaisesRegex(ArchiveError, 'rate limit') as error:
                holder[0].get('https://cdn.tapas.io/a')
        self.assertEqual(error.exception.source_http_status, 429)
        self.assertEqual(error.exception.retry_after_seconds, 30)
        self.assertEqual(client.calls, [])

    @patch('comic_archive.transport.time.sleep')
    def test_a_rate_limit_stops_the_job_that_met_it_and_the_next_job_may_ask_again(self, sleep):
        client = Client([Response(429, headers={'Retry-After': '30'}), Response()])
        transport = Transport(client=client)
        for _ in range(2):
            with self.assertRaisesRegex(ArchiveError, 'rate limit') as error:
                transport.get('https://cdn.tapas.io/a')
            self.assertEqual(error.exception.retry_after_seconds, 30)
        self.assertEqual(len(client.calls), 1)
        transport.clear_rate_limit()
        self.assertEqual(transport.get('https://cdn.tapas.io/a'), b'image')
        self.assertEqual(len(client.calls), 2)

    @patch('comic_archive.transport.time.sleep')
    def test_an_address_that_ran_out_or_was_refused_names_the_status_it_was_given(self, sleep):
        for code in (401, 403, 404, 410):
            client = Client([Response(code), Response()])
            with self.subTest(code=code), self.assertRaises(ExpiredAsset) as error:
                Transport(client=client).get('https://cdn.tapas.io/a')
            self.assertEqual(error.exception.source_http_status, code)
            self.assertEqual(len(client.calls), 1)


class TransportAddressTests(unittest.TestCase):
    def test_an_address_whose_port_or_host_cannot_be_read_is_refused_as_outside_the_boundary(self):
        client = Client([Response()])
        for url in ('https://cdn.tapas.io:99999/a', 'https://cdn.tapas.io:abc/a', 'https://cdn.tapas.io:-1/a',
                    'https://[cdn.tapas.io/a', 'https://cdn.tapas.io:443/a'):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ArchiveError, 'host boundary') as error:
                    asset_url(url)
                self.assertIs(type(error.exception), ArchiveError)
                with self.assertRaisesRegex(ArchiveError, 'host boundary'):
                    Transport(client=client).get(url)
        self.assertEqual(client.calls, [])


class TransportAgentTests(unittest.TestCase):
    CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'

    def test_a_download_names_the_browser_it_was_given_and_without_one_adds_no_name_of_its_own(self):
        client = Client([Response(), Response()])
        self.assertEqual(Transport(client=client, agent=self.CHROME).get('https://cdn.tapas.io/a'), b'image')
        self.assertEqual(Transport(client=client).get('https://cdn.tapas.io/a'), b'image')
        (_, named), (_, unnamed) = client.calls
        self.assertEqual(named['User-Agent'], self.CHROME)
        self.assertEqual(named['Referer'], 'https://tapas.io/')
        # The HTTP client then gives its own name.
        self.assertNotIn('User-Agent', unnamed)

    def test_a_name_that_is_not_plain_printable_text_is_dropped_instead_of_sent(self):
        for agent in (b'Mozilla/5.0', 5, ['Mozilla/5.0'], '', '   ', 'Mozilla/5.0\r\nCookie: PRIVATE', 'Mozilla/5.0\n', 'Mozilla/5.0\t',
                      'Mozilla/5.0\x00', 'Mozilla/5.0\x7f', 'Mozilla/5.0 caf\u00e9'):
            with self.subTest(agent=agent):
                client = Client([Response()])
                transport = Transport(client=client, agent=agent)
                self.assertIsNone(transport.agent)
                transport.get('https://cdn.tapas.io/a')
                self.assertNotIn('User-Agent', client.calls[0][1])


if __name__ == '__main__':
    unittest.main()
