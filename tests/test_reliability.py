from datetime import datetime, timezone
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from comic_archive.adapters.tapas import BrowserReadTransport
from comic_archive.errors import NetworkUnavailable, RunLimit
from comic_archive.models import ArchiveError
from comic_archive.retry_after import MAX_RETRY_AFTER, retry_after_seconds
from comic_archive.transport import Transport
from test_transport_session import Client, Response


class ReliabilityTests(unittest.TestCase):
    def test_real_queue_accepts_only_boolean_scan_only_and_retains_budget(self):
        from comic_archive.queue_client import submit_job
        with tempfile.TemporaryDirectory() as work:
            session=Path(work)/'session'
            (session/'worker/requests').mkdir(parents=True)
            (session/'worker/status.json').write_text(json.dumps({'state':'ready'}))
            result=submit_job(session,'sync',{'scan_only':True,'refresh_lists':False,'scan_limit':50,'max_seconds':900})
            request=json.loads((session/'worker/requests'/(result['task_id']+'.json')).read_text())
            self.assertTrue(request['args']['scan_only'])
            self.assertEqual(request['args']['max_seconds'],900)
            with self.assertRaises(ArchiveError):submit_job(session,'sync',{'scan_only':'yes'})
            self.assertEqual(len(list((session/'worker/requests').glob('*.json'))),1)

    def test_retry_after_accepts_delta_and_http_date_without_retaining_raw_values(self):
        now=datetime(2026,10,8,3,0,tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('120'),120)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 03:02:00 GMT',now),120)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 02:59:00 GMT',now),0)
        self.assertEqual(retry_after_seconds('9'*200),MAX_RETRY_AFTER)
        for value in (True,120,'PRIVATE_COOKIE','-1','NaN'):
            self.assertIsNone(retry_after_seconds(value,now))

    @patch('comic_archive.transport.time.sleep')
    def test_cdn_rate_limit_preserves_wait_and_stops_other_starts(self,sleep):
        client=Client([Response(429,headers={'Retry-After':'120'}),Response()])
        transport=Transport(client=client)
        for url in ('https://cdn.tapas.io/pc/a','https://cdn.tapas.io/pc/b'):
            with self.assertRaises(ArchiveError) as caught:transport.get(url)
            self.assertEqual(caught.exception.source_http_status,429)
            self.assertEqual(caught.exception.retry_after_seconds,120)
        self.assertEqual(len(client.calls),1)

    @patch('comic_archive.transport.time.sleep')
    def test_long_server_wait_pauses_without_retrying_or_shortening_delay(self,sleep):
        client=Client([Response(503,headers={'Retry-After':'900'}),Response()])
        with self.assertRaises(NetworkUnavailable) as caught:
            Transport(client=client).get('https://cdn.tapas.io/pc/a')
        self.assertEqual(caught.exception.retry_after_seconds,900)
        self.assertEqual(len(client.calls),1)
        self.assertFalse(any(call.args[0]>=2 for call in sleep.call_args_list))

    @patch('comic_archive.adapters.tapas.Transport')
    def test_reader_rate_limit_retains_wait_without_alternate_navigation(self,assets):
        page=Mock();page.url='https://tapas.io/'
        page.evaluate.return_value={'status':429,'text':'PRIVATE_BODY','retry_after':'120'}
        transport=BrowserReadTransport(page,Mock())
        with self.assertRaises(ArchiveError) as caught:transport.get('https://tapas.io/episode/81')
        self.assertEqual(caught.exception.source_http_status,429)
        self.assertEqual(caught.exception.retry_after_seconds,120)
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertEqual(page.evaluate.call_count,1);page.goto.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_reader_refusal_classification_is_fixed_and_never_retries_or_leaks_body(self,assets):
        for kind in ('challenge', 'html', 'json', 'text', 'other', 'PRIVATE_COOKIE', {}, True):
            with self.subTest(kind=kind):
                page=Mock();page.url='https://tapas.io/'
                page.evaluate.return_value={'status':403,'text':'PRIVATE_BODY_AND_TOKEN',
                    'source_response_kind':kind,'headers':{'Set-Cookie':'PRIVATE_COOKIE'}}
                transport=BrowserReadTransport(page,Mock())
                with self.assertRaises(ArchiveError) as caught:
                    transport.get('https://tapas.io/episode/81')
                expected=kind if isinstance(kind,str) and kind in {'challenge','html','json','text','other'} else None
                self.assertEqual(getattr(caught.exception,'source_response_kind',None),expected)
                self.assertEqual('browser challenge' in str(caught.exception),kind=='challenge')
                self.assertNotIn('PRIVATE',str(caught.exception))
                self.assertNotIn('PRIVATE',json.dumps(transport.last_response))
                self.assertEqual(page.evaluate.call_count,1)
                page.goto.assert_not_called()

    def test_job_deadline_limits_socket_timeouts_and_rejects_late_stream_without_retry(self):
        clock=[100.0];calls=[]
        class LateStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'first'
                clock[0]=102.0
                yield b'late'
        def reply(request):
            calls.append(request.extensions['timeout'])
            return httpx.Response(200,stream=LateStream())
        with httpx.Client(timeout=10,transport=httpx.MockTransport(reply)) as client:
            transport=Transport(client=client);transport.deadline=101.0
            with patch('comic_archive.transport.time.monotonic',side_effect=lambda:clock[0]):
                with self.assertRaises(RunLimit):transport.get('https://cdn.tapas.io/pc/a')
            self.assertEqual(len(calls),1)
            self.assertTrue(all(value<=1 for value in calls[0].values()))
            self.assertEqual(transport.metrics.snapshot()['successful_requests'],0)

    def test_cancellation_interrupts_read_retry_backoff(self):
        from comic_archive.cancellation import RunCancelled
        failed=[False];calls=[]
        def reply(request):
            calls.append(request);failed[0]=True
            raise httpx.ReadTimeout('PRIVATE_URL')
        def cancel():
            if failed[0]:raise RunCancelled('Stopped')
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            transport=Transport(client=client);transport.cancel_check=cancel
            with self.assertRaises(RunCancelled):transport.get('https://cdn.tapas.io/pc/a')
            self.assertEqual(len(calls),1)
