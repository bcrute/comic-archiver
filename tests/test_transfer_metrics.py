import json
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from comic_archive.errors import AssetReadUnavailable, NetworkUnavailable
from comic_archive.models import ArchiveError
from comic_archive.transfer_metrics import TransferMetrics, network_failure_code
from comic_archive.transport import Transport
from test_transport_session import Client, Response


class TransferMetricsTests(unittest.TestCase):
    def test_bounded_latencies_keep_full_counts_and_exclude_raw_strings(self):
        metrics = TransferMetrics()
        for i in range(2100):
            metrics.begin(float(i))
            metrics.finish(float(i),float(i)+.1,200,5)
        metrics.begin(2100)
        metrics.finish(2100,2101,'PRIVATE_URL',0,'PRIVATE_COOKIE')
        result=metrics.snapshot()
        self.assertEqual(result['requests'],2101)
        self.assertEqual(result['successful_requests'],2100)
        self.assertEqual(result['successful_bytes'],10500)
        self.assertEqual(result['latency_sample_count'],2048)
        self.assertEqual(result['status_counts'],{'200':2100})
        self.assertEqual(result['error_counts'],{'network_error':1})
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_concurrent_counts_and_peak_are_coordinated(self):
        metrics=TransferMetrics();barrier=threading.Barrier(2)
        def record():
            metrics.begin(10)
            barrier.wait(3)
            metrics.finish(10,11,200,10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(record);b=pool.submit(record);a.result();b.result()
        result=metrics.snapshot()
        self.assertEqual(result['inflight_peak'],2)
        self.assertEqual(result['requests'],2)
        self.assertEqual(result['attempt_seconds_sum'],2)
        self.assertEqual(result['asset_span_seconds'],1)

    def test_retry_timing_excludes_pacing_backoff_and_releases_response_first(self):
        clock=[10.0];closed=[]
        class SlowResponse(Response):
            def iter_bytes(inner):
                clock[0]+=.1
                yield inner.data
            def __exit__(inner,*args):
                closed.append(inner.status_code)
        transport=Transport(interval=.25,client=Client([SlowResponse(503),SlowResponse()]))
        def sleep(delay):
            if delay>=2:self.assertEqual(closed,[503])
            clock[0]+=delay
        with patch('comic_archive.transport.time.monotonic',side_effect=lambda:clock[0]), \
             patch('comic_archive.transport.time.sleep',side_effect=sleep):
            self.assertEqual(transport.get('https://cdn.tapas.io/pc/page?signature=PRIVATE'),b'image')
        result=transport.metrics.snapshot()
        self.assertEqual(result['requests'],2)
        self.assertEqual(result['retries'],1)
        self.assertEqual(result['status_counts'],{'503':1,'200':1})
        self.assertEqual(result['attempt_seconds_sum'],.1)
        self.assertEqual(result['asset_span_seconds'],2.1)
        self.assertNotIn('PRIVATE',json.dumps(result))

    @patch('comic_archive.transport.time.sleep')
    def test_network_failure_records_class_without_secret_details(self,sleep):
        import httpx
        client=Client([])
        def fail(*args,**kwargs):
            raise httpx.ReadTimeout('PRIVATE_COOKIE https://cdn.tapas.io/pc/page?signature=PRIVATE')
        client.stream=fail;transport=Transport(client=client)
        with self.assertRaises(NetworkUnavailable) as failure:
            transport.get('https://cdn.tapas.io/pc/page')
        self.assertEqual(failure.exception.network_failure_code,'read_timeout')
        self.assertIsInstance(failure.exception,AssetReadUnavailable)
        result=transport.metrics.snapshot()
        self.assertEqual(result['requests'],3)
        self.assertEqual(result['retries'],2)
        self.assertEqual(result['error_counts'],{'read_timeout':3})
        self.assertNotIn('PRIVATE',json.dumps(result)+str(failure.exception))

    @patch('comic_archive.transport.time.sleep')
    def test_account_read_timeout_and_cdn_connection_failure_remain_global_holds(self,sleep):
        import httpx
        for url,kind in [('https://tapas.io/who-am-i',httpx.ReadTimeout),
                         ('https://cdn.tapas.io/pc/page',httpx.ConnectTimeout)]:
            client=Client([])
            def fail(*args,**kwargs):
                raise kind('PRIVATE network details')
            client.stream=fail
            with self.assertRaises(NetworkUnavailable) as failure:
                Transport(client=client).get(url)
            self.assertNotIsInstance(failure.exception,AssetReadUnavailable)

    @patch('comic_archive.transport.time.sleep')
    def test_same_origin_account_metadata_is_excluded_and_429_remains_a_hold(self,sleep):
        transport=Transport(client=Client([Response(data=b'PRIVATE_ACCOUNT'),Response(429)]))
        transport.get('https://tapas.io/who-am-i')
        self.assertEqual(transport.metrics.snapshot()['requests'],0)
        with self.assertRaises(ArchiveError):transport.get('https://cdn.tapas.io/pc/page')
        with self.assertRaises(ArchiveError):transport.get('https://cdn.tapas.io/pc/other')
        result=transport.metrics.snapshot()
        self.assertEqual(result['requests'],1)
        self.assertEqual(result['status_counts'],{'429':1})
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_controlled_floor_is_shared_by_queue_backlog_worker_and_service(self):
        from comic_archive.downloads import validate_request_interval
        from comic_archive.batches import budgets
        from comic_archive.queue_client import submit_job
        from comic_archive.worker import execute
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temporary:
            session=Path(temporary);(session/'worker/requests').mkdir(parents=True)
            self.assertEqual(budgets({'request_interval':.25})['request_interval'],.25)
            submit_job(session,'archive_discovered',{'request_interval':.25})
            adapter=Mock();adapter.interval=1.0
            with patch('comic_archive.jobs.archive_discovered') as job:
                execute(adapter,{'operation':'archive_discovered','args':{'request_interval':.25}})
                job.assert_called_once()
            self.assertEqual(adapter.interval,1.0)
            for invalid in (.249,0,True,float('inf'),float('nan')):
                with self.subTest(interval=invalid),self.assertRaises(ArchiveError):validate_request_interval(invalid)

    @patch('builtins.print')
    @patch('comic_archive.transport.time.sleep')
    def test_job_receipt_aggregates_across_episode_transports_and_restores_adapter(self,sleep,output):
        from comic_archive.inventory import Inventory
        from comic_archive.jobs import archive_discovered
        from test_jobs import AccountAdapter
        from test_archive import png
        adapter=AccountAdapter();old=TransferMetrics();adapter.asset_metrics=old
        def retrieve(page,max_bytes=None):
            # Recreate the per-episode transport, as browser capture does.
            transport=Transport(client=Client([Response(data=png(page.order))]),metrics=adapter.asset_metrics)
            return transport.get(page.location,max_bytes=max_bytes)
        adapter.retrieve=retrieve
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with Inventory(root) as inventory:
                inventory.observe('42','Series',[{'id':'81','title':'E81','order':1,'access_state':'tapas_free'},
                    {'id':'82','title':'E82','order':2,'access_state':'tapas_unlocked'}],'fixture')
            result=archive_discovered(adapter,root,limit=2,download_workers=2)
        self.assertEqual(result['asset_metrics']['requests'],4)
        self.assertEqual(result['asset_metrics']['status_counts'],{'200':4})
        self.assertEqual(result['asset_metrics']['successful_bytes'],result['downloaded_bytes'])
        self.assertIs(adapter.asset_metrics,old)


    def test_failures_are_classified_by_kind_and_unknown_ones_stay_generic(self):
        import httpx
        for error,code in ((httpx.ConnectTimeout('x'),'connect_timeout'),(httpx.ReadTimeout('x'),'read_timeout'),
                           (httpx.WriteTimeout('x'),'write_timeout'),(httpx.PoolTimeout('x'),'pool_timeout'),
                           (httpx.ConnectError('x'),'connect_error'),(httpx.ReadError('x'),'read_error'),
                           (httpx.WriteError('x'),'write_error'),(httpx.RemoteProtocolError('x'),'protocol_error'),
                           (httpx.TooManyRedirects('x'),'network_error'),(RuntimeError('PRIVATE'),'network_error')):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(network_failure_code(error),code)
