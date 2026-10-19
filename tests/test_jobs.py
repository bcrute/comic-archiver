import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from support import PRIVATE_FILE
from comic_archive.publication import PublicationStore as Store
from comic_archive.archive import verify, sidecar_path
from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError, RunLimit
from comic_archive.inventory import Inventory
from comic_archive.jobs import DownloadBudget, archive_discovered, sync
from comic_archive.locking import LibraryLock, wait_for_library
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from test_archive import png


class AccountAdapter:
    def episode_url(self,key):
        return f'https://tapas.io/episode/{key}'
    def __init__(self):
        self.identity = "11"
        self.calls = []
        self.downloads = []
        self.denied = set()
        self.access = {"81": "tapas_free", "82": "tapas_unlocked", "83": "unavailable_or_unverified"}
        self.visible = ["42"]
        self.series = Series("s", "Series", platform_id="42")

    def account_identity(self):
        self.calls.append("identity")
        return self.identity

    def capture_automatic(self, url):
        key = url.rsplit("/", 1)[1]
        self.calls.append(("capture", key))
        if key in self.denied:
            raise AccessDenied("No current access")
        return Manifest(self.series, (Episode(key, "s", "E" + key, int(key)-80,
            (Page(1, "https://us-a.tapas.io/pc/1.jpg", False), Page(2, "https://us-a.tapas.io/pc/2.jpg", False)), self.access[key], url),))

    def episode_access(self, url):
        key = url.rsplit("/", 1)[1]
        self.calls.append(("access", key))
        if key in self.denied:
            raise AccessDenied("No current access")
        return {"source_episode_id": key, "access_state": self.access[key]}

    def retrieve(self, page, max_bytes=None):
        data = png(page.order)
        if max_bytes is not None and len(data) > max_bytes:
            raise RunLimit("Limit")
        self.downloads.append(page.order)
        return data

    def inspect_library(self, category):
        self.calls.append(("library", category))
        return {"authenticated": True, "membership_verified": True, "scope": category,
                "series": [{"source_series_id": k, "title": "S"} for k in self.visible]}

    def inspect_series(self, key, strict_order=False):
        self.calls.append(("series", key))
        return {"complete": True, "authenticated": True, "source_series_id": key, "title": "S",
                "episodes": [{"id": id, "title": "E" + id, "order": int(id)-80, "access_state": access}
                             for id, access in self.access.items()]}


class JobTests(unittest.TestCase):
    @patch('builtins.print')
    def test_scan_duration_excludes_download_time_and_is_zero_for_archive_only(self,output):
        clock=[0.0];inspect=self.adapter.inspect_series;retrieve=self.adapter.retrieve
        def timed_inspect(*args,**kwargs):clock[0]+=5;return inspect(*args,**kwargs)
        def timed_retrieve(*args,**kwargs):clock[0]+=1;return retrieve(*args,**kwargs)
        self.adapter.inspect_series=timed_inspect;self.adapter.retrieve=timed_retrieve
        with Inventory(self.root) as inventory:inventory.observe('42','S',[],'fixture')
        with patch('comic_archive.jobs.time.monotonic',side_effect=lambda:clock[0]):
            first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,limit=1,max_seconds=60)
            self.assertEqual(first['scan_elapsed_seconds'],5.0);self.assertEqual(first['elapsed_seconds'],7.0)
            second=archive_discovered(self.adapter,self.root,limit=1,max_seconds=60)
        self.assertEqual(second['scan_elapsed_seconds'],0.0)
    @patch('builtins.print')
    @patch('comic_archive.metadata.enrich_with_store')
    def test_gateway_failure_during_enrichment_pauses_without_requeuing_committed_archive(self,enrich,output):
        from comic_archive.errors import NetworkUnavailable
        failure=NetworkUnavailable('PRIVATE_SOURCE_RESPONSE');failure.source_http_status=503
        enrich.side_effect=failure;self.adapter.supports_series_metadata=True
        self.adapter.access={'81':'tapas_free','82':'tapas_free'}
        with Inventory(self.root) as inventory:inventory.observe('42','S',[],'fixture')
        result=sync(self.adapter,self.root,refresh_lists=False)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['source_http_status'],503)
        self.assertEqual(result['archived_episode_ids'],['81']);self.assertEqual(result['downloaded_pages'],2)
        self.assertEqual(self.adapter.downloads,[1,2]);self.assertEqual(verify(self.root),[])
        self.assertNotIn(('capture','82'),self.adapter.calls);self.assertNotIn('PRIVATE',json.dumps(result))
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['82'])
            self.assertEqual(inventory.status()['episodes_by_status']['archived'],1)

    @patch('builtins.print')
    def test_daily_free_jobs_prioritize_changes_and_new_episodes_without_renewing_unchanged_access(self,output):
        with patch('comic_archive.inventory.timestamp',return_value='2026-10-01T04:00:00Z'):
            with Inventory(self.root) as inventory:
                inventory.observe('42','S',[{'id':'81','title':'Old free','order':1,'access_state':'tapas_free'},
                    {'id':'82','title':'Old locked','order':2,'access_state':'unavailable_or_unverified'}],'fixture')
        self.adapter.access={'81':'tapas_free','82':'tapas_free','83':'tapas_free'}
        with patch('comic_archive.inventory.timestamp',return_value='2026-10-03T04:00:00Z'):
            first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,free_only=True,limit=1)
        self.assertEqual(first['archived_episode_ids'],['82'])
        self.assertEqual(first['priority_policy'],'recent_free');self.assertEqual(first['recent_free_candidate_count'],2)
        self.assertEqual(first['recent_free_selected_count'],1)
        self.assertEqual(verify(self.root),[])
        # A daily refresh of unchanged access does not renew the old promotion
        # or discovery time; the older backlog can continue after the window.
        with patch('comic_archive.inventory.timestamp',return_value='2026-10-04T05:00:00Z'):
            second=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,free_only=True,limit=1)
        self.assertEqual(second['archived_episode_ids'],['81'])
        self.assertEqual(second['recent_free_candidate_count'],0)
        self.assertEqual(second['recent_free_selected_count'],0)
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_recent_free_priority_cannot_override_fresh_access_denial(self,output):
        with patch('comic_archive.inventory.timestamp',return_value='2026-10-01T04:00:00Z'):
            with Inventory(self.root) as inventory:
                inventory.observe('42','S',[{'id':'81','title':'Old','order':1,'access_state':'tapas_free'},
                    {'id':'82','title':'Locked','order':2,'access_state':'unavailable_or_unverified'}],'fixture')
        self.adapter.access['82']='tapas_free';self.adapter.denied={'82'}
        with patch('comic_archive.inventory.timestamp',return_value='2026-10-03T04:00:00Z'):
            result=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,free_only=True,limit=1)
            with Inventory(self.root,read_only=True) as inventory:
                self.assertEqual([row['id'] for row in inventory.pending(True)],['81'])
        self.assertEqual(result['unavailable_episode_ids'],['82'])
        self.assertEqual(result['archived_episode_ids'],[]);self.assertEqual(self.adapter.downloads,[])

    @patch('comic_archive.inventory.timestamp',return_value='2026-10-03T04:00:00Z')
    def test_free_priority_uses_timezone_aware_dates_and_ignores_invalid_or_future_observations(self,clock):
        with Inventory(self.root) as inventory:
            inventory.observe('42','S',[{'id':str(key),'title':'E','order':key-80,'access_state':'tapas_free'}
                for key in range(81,86)],'fixture',observed_at='2026-10-01T04:00:00Z')
            inventory.db.executemany('INSERT INTO observations VALUES (?, ?, ?)',[
                ('82','2026-10-03T05:00:00+03:00','tapas_free'),
                ('83','2026-10-03T04:01:00Z','tapas_free'),
                ('84','PRIVATE_INVALID_DATE','tapas_free'),
                ('85','2026-10-03T03:00:00','tapas_free')])
            inventory.db.commit();before=inventory.db.total_changes
            rows=inventory.pending(True)
            self.assertEqual([row['id'] for row in rows],['82','81','83','84','85'])
            self.assertEqual([row['recent_free_candidate'] for row in rows],[1,0,0,0,0])
            self.assertEqual(inventory.db.total_changes,before)
            self.assertNotIn('PRIVATE',json.dumps(rows))
            self.assertEqual([row['id'] for row in inventory.pending(False)],['81','82','83','84','85'])

    @patch('comic_archive.inventory.timestamp',return_value='2026-10-03T04:00:00+00:00')
    def test_scan_freshness_counts_expired_missing_and_invalid_checks_without_granting_access(self,clock):
        with Inventory(self.root) as inventory:
            for key in range(42,49):inventory.observe(str(key),'Series',[],'fixture')
            checks={'42':'2026-10-02T03:59:59Z','43':'2026-10-02T04:00:00Z',
                    '44':'2026-10-03T06:00:00+03:00','46':'PRIVATE_BAD_TIMESTAMP',
                    '47':'2026-10-03T04:01:00Z','48':'2026-10-03T03:00:00'}
            inventory.db.executemany('INSERT INTO series_scans VALUES (?,?)',checks.items())
            inventory.db.commit();before=inventory.db.total_changes
            result=inventory.status()
            self.assertEqual(result['scan_freshness'],{
                'observed_at':'2026-10-03T04:00:00+00:00','window_hours':24,
                'checked_within_24h':2,'older_than_24h':1,'never_checked':1,
                'invalid_timestamp_count':3,'oldest_successful_check':'2026-10-02T03:59:59+00:00'})
            self.assertFalse(result['inventory_complete']);self.assertEqual(inventory.pending(),[])
            self.assertEqual(inventory.db.total_changes,before)
            self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(self.adapter.calls,[])

    def test_metadata_hold_reasons_remain_explicit_and_never_grant_access(self):
        reasons=('novel','scheduled','temporary_access','access_denied','PRIVATE_CODE')
        episodes=[{'id':str(81+i),'title':'E','order':i+1,'access_state':'unavailable_or_unverified',
                   'authorization_reason':reason} for i,reason in enumerate(reasons)]
        with Inventory(self.root) as inventory:
            inventory.observe('42','S',episodes,'fixture')
            self.assertEqual(inventory.status()['episodes_by_status'],{'unsupported':3,'unavailable':2})
            self.assertEqual(inventory.status()['unsupported_by_reason'],{'novel':1,'scheduled':1,'temporary_access':1})
            self.assertEqual(inventory.pending(),[])
            self.assertNotIn('PRIVATE',json.dumps([dict(row) for row in inventory.db.execute('SELECT * FROM episodes')]))
            self.assertEqual(self.adapter.calls,[])

    def test_fresh_supported_observation_requeues_hold_and_archived_content_survives_access_loss(self):
        with Inventory(self.root) as inventory:
            episode={'id':'81','title':'E','order':1,'access_state':'unavailable_or_unverified','authorization_reason':'temporary_access'}
            inventory.observe('42','S',[episode],'fixture')
            inventory.observe('42','S',[episode|{'access_state':'tapas_free','authorization_reason':None}],'fresh')
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])
            inventory.mark('81','archived')
            inventory.observe('42','S',[episode],'later')
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1})

    @patch('builtins.print')
    def test_optional_free_view_records_provenance_and_rechecks_before_free_only_capture(self,output):
        original=self.adapter.inspect_library
        def library(category):
            value=original(category);value['account_membership_verified']=False
            return value
        self.adapter.inspect_library=library
        result=sync(self.adapter,self.root,categories=['free_episodes'],free_only=True,scan_limit=1)
        self.assertEqual(result['archived_episode_ids'],['81']);self.assertIn(('library','free_episodes'),self.adapter.calls)
        self.assertIn(('access','81'),self.adapter.calls);self.assertNotIn(('capture','82'),self.adapter.calls)
        self.assertNotIn(('capture','83'),self.adapter.calls)
        with Inventory(self.root,read_only=True) as inventory:
            value=inventory.db.execute('SELECT provenance FROM series WHERE id=?',('42',)).fetchone()[0]
            self.assertIn('free_episodes',value)
        self.assertFalse(result['inventory_complete'])

    @patch('builtins.print')
    def test_watched_only_sync_rechecks_observed_access_without_account_list_navigation(self,output):
        with Inventory(self.root) as inventory:inventory.observe('42','Known',[],'fixture')
        result=sync(self.adapter,self.root,refresh_lists=False,free_only=True)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertFalse(result['refresh']['lists_refreshed'])
        self.assertIn(('series','42'),self.adapter.calls)
        self.assertFalse(any(isinstance(v,tuple) and v[0]=='library' for v in self.adapter.calls))
        self.assertNotIn(('capture','82'),self.adapter.calls)
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_scan_only_checks_inventory_without_readers_or_assets_and_uses_full_time_window(self,output):
        with Inventory(self.root) as inventory:inventory.observe('42','Known',[],'fixture')
        result=sync(self.adapter,self.root,refresh_lists=False,scan_only=True,max_seconds=120)
        self.assertTrue(result['scan_only'])
        self.assertEqual(result['refresh']['time_budget_seconds'],120)
        self.assertEqual(result['refresh']['remaining_series_count'],0)
        self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(result['downloaded_pages'],0)
        self.assertEqual(result['downloaded_bytes'],0)
        self.assertEqual(self.adapter.downloads,[])
        self.assertFalse(any(isinstance(v,tuple) and v[0] in {'capture','access','library'} for v in self.adapter.calls))
        self.assertFalse(list(self.root.rglob('*.cbz')))
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(len(inventory.pending()),2)

    def test_scan_only_rejects_nonboolean_before_contacting_source(self):
        with self.assertRaises(ArchiveError):sync(self.adapter,self.root,scan_only='yes')
        self.assertEqual(self.adapter.calls,[])

    @patch('builtins.print')
    def test_watched_only_sync_retains_scan_cycle_across_runs_and_denies_account_change(self,output):
        clock=[0.0]
        with Inventory(self.root) as inventory:
            inventory.observe('42','First',[],'fixture')
            inventory.observe('43','Second',[],'fixture')
        def inspect(key,strict_order=False):
            self.adapter.calls.append(('series',key));clock[0]+=61
            return {'source_series_id':key,'authenticated':True,'complete':True,'title':'S','episodes':[]}
        self.adapter.inspect_series=inspect
        with patch('comic_archive.jobs.time.monotonic',side_effect=lambda:clock[0]):
            first=sync(self.adapter,self.root,refresh_lists=False,max_seconds=120)
            self.assertEqual(first['refresh']['remaining_series_count'],1)
            second=sync(self.adapter,self.root,refresh_lists=False,max_seconds=120)
        self.assertEqual(second['refresh']['remaining_series_count'],0)
        self.assertEqual([v for v in self.adapter.calls if isinstance(v,tuple)],[('series','42'),('series','43')])
        self.adapter.calls.clear();self.adapter.identity='12'
        with self.assertRaises(AuthenticationError):sync(self.adapter,self.root,refresh_lists=False)
        self.assertEqual(self.adapter.calls,['identity']);self.assertEqual(self.adapter.downloads,[])

    def test_watched_only_sync_rejects_conflicting_or_nonboolean_scope_before_network(self):
        for options in ({'refresh_lists':'yes'},{'refresh_lists':False,'categories':['subscribed']},
                        {'refresh_lists':False,'include_history':True}):
            with self.assertRaises(ArchiveError):sync(self.adapter,self.root,**options)
        self.assertEqual(self.adapter.calls,[])

    @patch('builtins.print')
    def test_unexpected_reader_manifest_pauses_before_trying_other_readers(self,output):
        from comic_archive.errors import UnsupportedContent
        def incomplete(url):
            self.adapter.calls.append(('attempt',url.rsplit('/',1)[1]))
            error=UnsupportedContent('Missing manifest');error.code='reader_manifest_incomplete'
            raise error
        self.adapter.capture_automatic=incomplete
        with self.assertRaises(MetadataError):archive_discovered(self.adapter,self.root,self.report)
        self.assertIn(('attempt','82'),self.adapter.calls)
        self.assertNotIn(('attempt','81'),self.adapter.calls)
        self.assertEqual(self.adapter.downloads,[])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['unsupported_by_reason'],{'reader_manifest_incomplete':1})
            self.assertEqual(inventory.status()['last_run']['status'],'paused')
            self.assertEqual(inventory.status()['episodes_by_status'],{'unsupported':1,'queued':1})

    @patch('builtins.print')
    def test_unsupported_reason_is_preserved_without_raw_exception_or_unknown_codes(self,output):
        from comic_archive.errors import UnsupportedContent
        def unsupported(url):
            error=UnsupportedContent('SESSION_PRIVATE')
            error.code='novel' if url.endswith('/81') else 'PRIVATE_CODE'
            raise error
        self.adapter.capture_automatic=unsupported
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertEqual(result['unsupported_episode_ids'],['82','81'])
        self.assertEqual(self.adapter.downloads,[])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([r[0] for r in inventory.db.execute('SELECT error_code FROM episodes ORDER BY id')],
                             ['novel','content_unsupported'])
        self.assertNotIn('PRIVATE',(self.root/'.archive-state/last-run.json').read_text())

    @patch('builtins.print')
    def test_slow_account_list_retains_discovery_and_leaves_time_to_archive(self, output):
        clock = [0.0]
        class SlowList(AccountAdapter):
            supports_library_checkpoint = True
            def inspect_library(self, category, on_page=None):
                clock[0] = 61.0
                on_page({'authenticated':True,'membership_verified':True,'scope':category,
                         'series':[{'source_series_id':'99','title':'Discovered'}]})
                raise AssertionError('The checkpoint should end the metadata window')
            def capture_automatic(self, url):
                self_deadline = self.deadline
                if self_deadline != 120.0:
                    raise AssertionError('The archive must regain the overall deadline')
                return super().capture_automatic(url)
        adapter = SlowList()
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            result = sync(adapter, self.root, self.report, max_seconds=120, free_only=True)
        self.assertEqual(result['archived_episode_ids'], ['81'])
        self.assertEqual(result['refresh']['state'], 'limited')
        self.assertEqual(result['refresh']['remaining_series_count'], 2)
        self.assertEqual(adapter.downloads, [1, 2])
        self.assertEqual(verify(self.root), [])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertIn('99', [s['id'] for s in inventory.watched_series()])
            self.assertIsNone(inventory.status()['last_completed_scan'])

    @patch('builtins.print')
    def test_series_scan_window_preserves_completed_checkpoint_and_archives(self, output):
        clock = [0.0]
        with Inventory(self.root) as inventory:
            inventory.ingest_report(self.report)
            inventory.observe('43', 'Next', [], 'recent')
        inspect = self.adapter.inspect_series
        def slow(key, strict_order=False):
            value = inspect(key, strict_order)
            clock[0] = 61.0
            return value
        self.adapter.inspect_series = slow
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            result = sync(self.adapter, self.root, max_seconds=120, limit=1)
        self.assertEqual(result['archived_episode_ids'], ['82'])
        self.assertEqual(result['refresh']['state'], 'limited')
        self.assertEqual(result['refresh']['remaining_series_count'], 1)
        self.assertEqual(result['refresh']['series_check_target_count'],2)
        self.assertEqual(result['refresh']['series_checks_completed'],1)
        self.assertEqual(result['refresh']['successful_series_checks'],1)
        self.assertEqual(result['refresh']['held_series_checks'],0)
        self.assertNotIn(('series', '43'), self.adapter.calls)
        with Inventory(self.root) as inventory:
            self.assertEqual([s['id'] for s in inventory.scan_candidates()], ['43'])

    @patch('builtins.print')
    def test_session_failure_at_metadata_window_end_stops_before_reader(self, output):
        clock = [0.0]
        def expired(category):
            clock[0] = 61.0
            raise AuthenticationError('Login required')
        self.adapter.inspect_library = expired
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(AuthenticationError):
                sync(self.adapter, self.root, self.report, max_seconds=120)
        self.assertEqual(self.adapter.downloads, [])
        self.assertNotIn(('capture', '81'), self.adapter.calls)

    @patch('builtins.print')
    def test_full_scan_resumes_across_time_windows_and_repeated_archives_use_cache(self, output):
        clock = [0.0]
        with Inventory(self.root) as inventory:
            inventory.ingest_report(self.report)
            inventory.observe('43', 'Another', [], 'recent')
        inspect = self.adapter.inspect_series
        def slow(key, strict_order=False):
            self.adapter.calls.append(('slow_series', key))
            clock[0] += 61.0
            if key == '42':
                return inspect(key, strict_order)
            return {'authenticated':True,'complete':True,'source_series_id':key,'title':'Another','episodes':[]}
        self.adapter.inspect_series = slow
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            first = sync(self.adapter, self.root, max_seconds=120, free_only=True)
        self.assertIsNone(first['inventory']['last_completed_scan'])
        self.assertEqual(first['archived_episode_ids'], ['81'])
        self.adapter.calls.clear()
        clock[0] = 0.0
        downloads = len(self.adapter.downloads)
        with patch('comic_archive.jobs.time.monotonic', side_effect=lambda: clock[0]):
            second = sync(self.adapter, self.root, max_seconds=120, free_only=True)
        self.assertIn(('slow_series','43'), self.adapter.calls)
        self.assertNotIn(('slow_series','42'), self.adapter.calls)
        self.assertIsNotNone(second['inventory']['last_completed_scan'])
        self.assertEqual(second['refresh']['remaining_series_count'], 0)
        self.assertEqual(len(self.adapter.downloads), downloads)
        self.assertEqual(second['archived_episode_ids'], [])
        self.assertEqual(verify(self.root), [])

    @patch('builtins.print')
    def test_library_pages_checkpoint_series_before_a_scan_deadline(self,output):
        class CheckpointAdapter(AccountAdapter):
            supports_library_checkpoint=True
            def inspect_library(self,category,on_page=None):
                on_page({'authenticated':True,'membership_verified':True,'scope':category,
                         'series':[{'source_series_id':'99','title':'Discovered'}]})
                raise RunLimit('metadata deadline')
        result=sync(CheckpointAdapter(),self.root,max_seconds=120,free_only=True)
        self.assertEqual(result['status'],'limited');self.assertEqual(result['downloaded_pages'],0)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertIn('99',[s['id'] for s in inventory.watched_series()])
            self.assertIsNone(inventory.status()['last_completed_scan'])

    def test_scheduled_writer_waits_for_an_existing_process_without_overlap(self):
        # The other process keeps the library until this test closes its input, however slowly either runs.
        code = "import sys; from comic_archive.locking import LibraryLock; from pathlib import Path;\nwith LibraryLock(Path(sys.argv[1])):\n print('locked',flush=True);sys.stdin.read()"
        process = subprocess.Popen([sys.executable,'-c',code,str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        release = threading.Timer(.25,process.stdin.close)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            self.assertIsNone(process.poll())
            release.start()
            with wait_for_library(self.root,10):
                self.assertEqual(process.wait(timeout=2),0)
        finally:
            release.cancel()
            if release.is_alive():release.join()
            process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdout.close()
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / "Library"
        self.root.mkdir()
        self.adapter = AccountAdapter()
        self.report = self.root / "discovery.json"
        self.report.write_text(json.dumps({"schema_version": 1, "source": "tapas",
            "document_type": "comic_access_discovery", "access_snapshot_only": True,
            "series": [{"source_series_id": "42", "title": "S", "checked_at": "2026-10-02T00:00:00Z",
                "accessible_episodes": [{"id": "81", "title": "E81", "order": 1, "access_state": "tapas_free"},
                                        {"id": "82", "title": "E82", "order": 2, "access_state": "tapas_unlocked"}]}]}))

    @patch('builtins.print')
    def test_a_time_limit_that_ends_the_refresh_itself_still_reports_the_scan_count(self,output):
        clock=[0.0];identity=self.adapter.account_identity
        def slow_sign_in():
            clock[0]=11.0  # Signing in outlasted this short last batch.
            return identity()
        self.adapter.account_identity=slow_sign_in
        with Inventory(self.root) as inventory:inventory.observe('42','S',[],'fixture')
        with patch('comic_archive.jobs.time.monotonic',side_effect=lambda:clock[0]):
            result=sync(self.adapter,self.root,refresh_lists=False,max_seconds=10)
        self.assertEqual(result['status'],'limited')
        self.assertEqual(result['refresh'],{'state':'limited','remaining_series_count':1})
        self.assertEqual(self.adapter.calls,['identity'])

    @patch('builtins.print')
    def test_an_episode_that_keeps_failing_is_tried_after_the_others_and_then_held(self,output):
        capture=self.adapter.capture_automatic
        def broken(url):
            if url.endswith('/82'):raise MetadataError('The reader page is broken.')
            return capture(url)
        self.adapter.capture_automatic=broken
        with self.assertRaises(MetadataError):archive_discovered(self.adapter,self.root,self.report,limit=1)
        # The next run goes past it instead of failing on it again first.
        self.assertEqual(archive_discovered(self.adapter,self.root,self.report,limit=1)['archived_episode_ids'],['81'])
        for _ in range(2):
            with self.assertRaises(MetadataError):archive_discovered(self.adapter,self.root,self.report)
        self.adapter.calls.clear()
        held=archive_discovered(self.adapter,self.root,self.report)
        self.assertEqual((held['status'],held['pending_episode_count']),('finished',0))
        self.assertNotIn(('capture','82'),self.adapter.calls)
        with Inventory(self.root,read_only=True) as inventory:
            row=inventory.db.execute("SELECT status,error_code,attempts FROM episodes WHERE id='82'").fetchone()
            self.assertEqual(tuple(row),('failed','job_failed',3))
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1,'failed':1})

    @patch('builtins.print')
    def test_a_job_waits_for_a_short_library_writer_instead_of_failing_before_its_run(self,output):
        held=threading.Event();release=threading.Event()
        def writer():
            with LibraryLock(self.root):
                held.set();release.wait(5)
        thread=threading.Thread(target=writer);thread.start()
        self.addCleanup(thread.join,5);self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        timer=threading.Timer(.2,release.set);timer.start();self.addCleanup(timer.cancel)
        result=archive_discovered(self.adapter,self.root,self.report,limit=1)
        self.assertEqual((result['status'],result['archived_episode_ids']),('limited',['82']))

    @patch("builtins.print")
    def test_durable_queue_resumes_after_page_limit_and_skips_verified_archives(self, output):
        first = archive_discovered(self.adapter, self.root, self.report, max_pages=1)
        self.assertEqual(first["status"], "limited")
        self.assertEqual(self.adapter.downloads, [1])
        self.assertEqual(list(self.root.rglob("*.cbz")), [])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.status()["episodes_by_status"], {"queued": 2})
        second = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(second["archived_episode_ids"], ["82", "81"])
        self.assertEqual(self.adapter.downloads, [1, 2, 1, 2])
        self.assertEqual(verify(self.root), [])
        self.adapter.calls.clear()
        third = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(third["downloaded_pages"], 0)
        self.assertEqual(self.adapter.calls, ["identity"])
        self.assertEqual(third["inventory"]["episodes_by_status"], {"archived": 2})

    @patch("builtins.print")
    def test_fresh_access_denial_skips_reader_assets_and_continues(self, output):
        self.adapter.denied.add("81")
        result = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(result["unavailable_episode_ids"], ["81"])
        self.assertEqual(result["archived_episode_ids"], ["82"])
        self.assertEqual(self.adapter.downloads, [1, 2])

    @patch("builtins.print")
    def test_account_binding_blocks_another_account_before_capture(self, output):
        archive_discovered(self.adapter, self.root, self.report, max_pages=1)
        self.adapter.identity = "12"
        self.adapter.calls.clear()
        with self.assertRaises(AuthenticationError):
            archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(self.adapter.calls, ["identity"])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.status()["last_run"]["status"], "login_required")
        self.assertNotIn('"12"', (self.root / ".archive-state/last-run.json").read_text())

    @patch("builtins.print")
    def test_sync_checks_retained_series_and_preserves_archives_when_access_ends(self, output):
        archive_discovered(self.adapter, self.root, self.report)
        self.adapter.visible = []
        self.adapter.access["81"] = "unavailable_or_unverified"
        self.adapter.access["83"] = "tapas_free"
        self.adapter.calls.clear()
        result = sync(self.adapter, self.root)
        self.assertIn(("series", "42"), self.adapter.calls)
        self.assertEqual(result["archived_episode_ids"], ["83"])
        self.assertEqual(len(list(self.root.rglob("*.cbz"))), 3)
        self.assertEqual(verify(self.root), [])

    @patch('builtins.print')
    def test_history_links_add_watched_series_but_do_not_grant_episode_access(self, output):
        self.adapter.inspect_transaction_history=lambda max_pages,on_page=None: {'authenticated':True,'complete':False,
            'content_links':['https://tapas.io/series/older-owned-title']}
        self.adapter.series_identity=lambda key: {'source_series_id':'43','title':'Older title'}
        inspect=self.adapter.inspect_series
        def inspected(key,strict_order=False):
            if key=='43':
                return {'authenticated':True,'complete':True,'source_series_id':'43','title':'Older title',
                        'episodes':[{'id':'84','title':'Locked','order':1,'access_state':'unavailable_or_unverified'}]}
            return inspect(key,strict_order)
        self.adapter.inspect_series=inspected
        result=sync(self.adapter,self.root,self.report,include_history=True)
        with Inventory(self.root,read_only=True) as inventory:
            row=inventory.db.execute("SELECT * FROM episodes WHERE id='84'").fetchone()
            self.assertEqual(row['status'],'unavailable')
            series=inventory.db.execute("SELECT * FROM series WHERE id='43'").fetchone()
            self.assertIn('transaction_history',series['provenance'])
            self.assertIsNotNone(inventory.status()['last_completed_scan'])
        self.assertNotIn(('capture','84'),self.adapter.calls)
        self.assertFalse(result['inventory_complete'])
        self.adapter.series_identity=lambda key: self.fail('Known source IDs must not be resolved again')
        sync(self.adapter,self.root,self.report,include_history=True)

    @patch('builtins.print')
    def test_history_credentials_are_rejected_before_snapshot_storage(self, output):
        self.adapter.inspect_transaction_history=lambda max_pages,on_page=None:{'authenticated':True,
            'content_links':['https://user:secret@tapas.io/series/s']}
        with self.assertRaises(MetadataError):
            sync(self.adapter,self.root,self.report,include_history=True)
        self.assertFalse((self.root/'.archive-state/history.json').exists())
        self.assertNotIn('secret',(self.root/'.archive-state/last-run.json').read_text())

    @patch('builtins.print')
    def test_rotating_scan_resumes_and_archives_before_finishing_whole_inventory(self,output):
        with Inventory(self.root) as inventory:
            inventory.ingest_report(self.report)
            inventory.observe('43','Another series',[],'recent')
            inventory.observe('44','Older series',[],'recent')
        original = self.adapter.inspect_series
        def inspect(key,strict_order=False):
            if key == '42':
                return original(key,strict_order)
            self.adapter.calls.append(('series',key))
            return {'complete':True,'authenticated':True,'source_series_id':key,'title':'S','episodes':[]}
        self.adapter.inspect_series = inspect
        first = sync(self.adapter,self.root,scan_limit=1,limit=1)
        self.assertEqual(first['archived_episode_ids'],['82'])
        self.assertEqual(first['inventory']['scan_remaining_series_count'],2)
        self.assertIsNone(first['inventory']['last_completed_scan'])
        with Inventory(self.root) as inventory:
            inventory.observe('45','New discovery',[],'transaction_history')
        self.adapter.calls.clear()
        second = sync(self.adapter,self.root,scan_limit=1,limit=1)
        self.assertIn(('series','43'),self.adapter.calls)
        self.assertNotIn(('series','42'),self.adapter.calls)
        self.assertEqual(second['archived_episode_ids'],['81'])
        self.assertEqual(second['inventory']['scan_remaining_series_count'],2)
        sync(self.adapter,self.root,scan_limit=1)
        final = sync(self.adapter,self.root,scan_limit=1)
        self.assertEqual(final['inventory']['scan_remaining_series_count'],0)
        self.assertIsNotNone(final['inventory']['last_completed_scan'])
        self.assertFalse(final['inventory']['inventory_complete'])
        self.assertEqual(verify(self.root),[])

    def test_interrupted_scan_retains_only_successful_series_checkpoints(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42','First',[],'subscribed')
            inventory.observe('43','Second',[],'subscribed')
            self.assertEqual([row['id'] for row in inventory.scan_candidates(2)],['42','43'])
            inventory.record_series_scan('42')
            self.assertFalse(inventory.finish_scan_cycle())
        with Inventory(self.root) as inventory:
            self.assertEqual([row['id'] for row in inventory.scan_candidates(2)],['43'])
            with self.assertRaises(MetadataError):
                inventory.record_series_scan('not-an-id')
            inventory.record_series_scan('43')
            self.assertTrue(inventory.finish_scan_cycle())

    @patch('builtins.print')
    def test_history_resume_is_forwarded_without_accepting_cursor_values(self,output):
        from unittest.mock import Mock
        self.adapter.inspect_transaction_history = Mock(return_value={'authenticated':True,'content_links':[]})
        sync(self.adapter,self.root,self.report,include_history=True,history_resume=True,scan_limit=1)
        self.assertTrue(self.adapter.inspect_transaction_history.call_args.kwargs['resume'])
        with self.assertRaises(ArchiveError):
            sync(self.adapter,self.root,history_resume=True)

    @patch("builtins.print")
    def test_byte_budget_stops_without_archiving_partial_image(self, output):
        result = archive_discovered(self.adapter, self.root, self.report, max_bytes=1)
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["downloaded_bytes"], 0)
        self.assertEqual(list(self.root.rglob("*.cbz")), [])

    @patch("builtins.print")
    def test_malformed_sidecar_is_requeued_and_repaired_from_cached_pages(self, output):
        archive_discovered(self.adapter, self.root, self.report)
        path = sidecar_path(next(self.root.rglob('*.cbz')))
        path.write_text('{broken')
        self.adapter.downloads.clear()
        result = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(len(result['archived_episode_ids']), 1)
        self.assertEqual(self.adapter.downloads, [])
        self.assertEqual(verify(self.root), [])
        progress = json.loads((self.root / '.archive-state/progress.json').read_text())
        self.assertEqual(progress['status'], 'finished')

    @patch("builtins.print")
    def test_sync_time_budget_includes_metadata_scan(self, output):
        clock = [0.0]
        inspect = self.adapter.inspect_library
        def delayed(category):
            result = inspect(category)
            clock[0] = 2.0
            return result
        self.adapter.inspect_library = delayed
        with patch("comic_archive.jobs.time.monotonic", side_effect=lambda: clock[0]):
            result = sync(self.adapter, self.root, self.report, max_seconds=1)
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["elapsed_seconds"], 2.0)
        self.assertEqual(self.adapter.downloads, [])
        self.assertNotIn(("library", "recent"), self.adapter.calls)

    @patch("builtins.print")
    def test_free_only_does_not_capture_queued_nonfree_episodes(self, output):
        result = archive_discovered(self.adapter, self.root, self.report, free_only=True)
        self.assertEqual(result["archived_episode_ids"], ["81"])
        self.assertNotIn(("capture", "82"), self.adapter.calls)

    @patch("builtins.print")
    def test_ended_free_promotion_is_rejected_before_reader_capture(self, output):
        self.adapter.access["81"] = "tapas_unlocked"
        result = archive_discovered(self.adapter, self.root, self.report, free_only=True)
        self.assertEqual(result["not_free_episode_ids"], ["81"])
        self.assertNotIn(("capture", "81"), self.adapter.calls)
        self.assertEqual(self.adapter.downloads, [])
        result = archive_discovered(self.adapter, self.root, self.report)
        self.assertIn('81', result['archived_episode_ids'])

    @patch("builtins.print")
    def test_old_report_never_requeues_a_freshly_denied_episode(self, output):
        self.adapter.denied.add('81')
        archive_discovered(self.adapter,self.root,self.report)
        self.adapter.calls.clear()
        archive_discovered(self.adapter,self.root,self.report)
        self.assertNotIn(('capture','81'),self.adapter.calls)

    @patch("builtins.print")
    def test_bad_source_order_does_not_block_other_verified_episodes(self, output):
        report = json.loads(self.report.read_text())
        report["series"][0]["accessible_episodes"][0]["order"] = None
        self.report.write_text(json.dumps(report))
        result = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(result["archived_episode_ids"], ["82"])
        self.assertEqual(result["inventory"]["episodes_by_status"], {"archived": 1, "unsupported": 1})

    @patch("builtins.print")
    def test_process_interruption_is_recorded_and_completed_pages_resume(self, output):
        get = self.adapter.retrieve
        def interrupt(page, max_bytes=None):
            if page.order == 2:
                raise KeyboardInterrupt
            return get(page, max_bytes)
        self.adapter.retrieve = interrupt
        with self.assertRaises(KeyboardInterrupt):
            archive_discovered(self.adapter, self.root, self.report)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.status()["last_run"]["status"], "interrupted")
            self.assertEqual(inventory.status()["episodes_by_status"], {"queued": 2})
        self.adapter.retrieve = get
        archive_discovered(self.adapter, self.root, self.report, limit=1)
        self.assertEqual(self.adapter.downloads, [1, 2])

    @patch("builtins.print")
    def test_changed_source_identity_stops_and_saves_safe_failure(self, output):
        self.adapter.series = replace(self.adapter.series, platform_id="99")
        with self.assertRaises(MetadataError):
            archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(self.adapter.downloads, [])
        saved = json.loads((self.root / ".archive-state/last-run.json").read_text())
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["inventory"]["episodes_by_status"], {"failed": 1, "queued": 1})

    @patch("builtins.print")
    def test_corrupt_archive_is_repaired_using_cached_original_bytes(self, output):
        archive_discovered(self.adapter, self.root, self.report)
        next(self.root.rglob("*.cbz")).write_bytes(b"broken")
        downloads = len(self.adapter.downloads)
        result = archive_discovered(self.adapter, self.root, self.report)
        self.assertEqual(len(result["archived_episode_ids"]), 1)
        self.assertEqual(len(self.adapter.downloads), downloads)
        self.assertEqual(verify(self.root), [])

    @patch("builtins.print")
    def test_title_changes_preserve_existing_paths(self, output):
        archive_discovered(self.adapter, self.root, self.report)
        paths = sorted(self.root.rglob("*.cbz"))
        self.adapter.series = replace(self.adapter.series, title="Changed")
        manifest = self.adapter.capture_automatic("https://tapas.io/episode/81")
        manifest = replace(manifest, episodes=(replace(manifest.episodes[0], title="New name"),))
        with StoreContext(self.root) as store:
            store.archive(manifest, self.adapter, progress=lambda _: None)
        self.assertEqual(sorted(self.root.rglob("*.cbz")), paths)

    def test_cross_process_lock_blocks_second_writer_and_allows_reentrancy(self):
        code = "from pathlib import Path; from comic_archive.archive import Store; from comic_archive.errors import LibraryBusy; import sys\ntry:\n Store(Path(sys.argv[1]))\nexcept LibraryBusy:\n sys.exit(23)\n"
        with LibraryLock(self.root), LibraryLock(self.root):
            result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True)
            self.assertEqual(result.returncode, 23)
        with LibraryLock(self.root):
            pass

    def test_reusing_one_nested_lock_releases_it_after_the_outer_context(self):
        code="from pathlib import Path; from comic_archive.locking import LibraryLock; from comic_archive.errors import LibraryBusy; import sys\ntry:\n with LibraryLock(Path(sys.argv[1])):pass\nexcept LibraryBusy:sys.exit(23)\n"
        lock=LibraryLock(self.root)
        with lock:
            with lock:
                pass
            result=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True)
            self.assertEqual(result.returncode,23)
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True)
        self.assertEqual(result.returncode,0)


    def test_download_budget_rejects_unusable_limits_before_any_source_request(self):
        for options in ({'max_pages':0},{'max_pages':True},{'max_pages':2.0},{'max_bytes':-1},{'max_bytes':'9'},
                        {'max_seconds':0},{'max_seconds':-5},{'max_seconds':float('nan')},
                        {'max_seconds':float('inf')},{'max_seconds':True},{'max_seconds':'60'}):
            with self.subTest(options=options):
                with self.assertRaises(ArchiveError):DownloadBudget(self.adapter,**options)
                with self.assertRaises(ArchiveError):archive_discovered(self.adapter,self.root,self.report,**options)
        self.assertEqual(self.adapter.calls,[]);self.assertFalse((self.root/'.archive-state').exists())

    def test_download_budget_stops_before_requesting_once_its_byte_allowance_is_spent(self):
        pages=self.adapter.capture_automatic('https://tapas.io/episode/81').episodes[0].pages
        covers=[];requested=[];retrieve=self.adapter.retrieve
        def cover(series,max_bytes=None):
            covers.append(max_bytes);return png(3)
        def recorded(page,max_bytes=None):
            requested.append(page.order);return retrieve(page,max_bytes=max_bytes)
        self.adapter.retrieve_cover=cover;self.adapter.retrieve=recorded
        budget=DownloadBudget(self.adapter,max_bytes=len(png(1)))
        self.assertEqual(budget.retrieve(pages[0]),png(1))
        with self.assertRaises(RunLimit):budget.retrieve(pages[1])
        with self.assertRaises(RunLimit):budget.retrieve_cover(self.adapter.series)
        self.assertEqual(requested,[1]);self.assertEqual(self.adapter.downloads,[1]);self.assertEqual(covers,[])
        self.assertEqual((budget.pages,budget.bytes,budget.covers),(1,len(png(1)),0))
        roomy=DownloadBudget(self.adapter,max_bytes=len(png(3))+5)
        self.assertEqual(roomy.retrieve_cover(self.adapter.series),png(3))
        self.assertEqual((covers,roomy.bytes,roomy.covers),([len(png(3))+5],len(png(3)),1))

    def test_job_options_that_make_no_sense_stop_before_any_source_request(self):
        for options in ({'_scan_only':'yes'},{'_scan_only':True},{'_pipeline':'yes'},
                        {'retry_episode_id':'0'},{'retry_episode_id':81,'limit':1},{'retry_episode_id':'81'},
                        {'retry_episode_id':'81','limit':1,'_refresh':lambda *args:None},
                        {'limit':0},{'limit':True},{'limit':'1'},{'free_only':'yes'},
                        {'min_free_bytes':-1},{'min_free_bytes':True}):
            with self.subTest(options=options),self.assertRaises(ArchiveError):
                archive_discovered(self.adapter,self.root,self.report,**options)
        self.assertEqual(self.adapter.calls,[]);self.assertFalse((self.root/'.archive-state').exists())

    def test_sync_rejects_invalid_scope_options_before_network(self):
        for options in ({'history_pages':0},{'history_pages':501},{'history_pages':True},
                        {'scan_limit':0},{'scan_limit':True},{'scan_limit':'1'},
                        {'categories':['purchased']},{'categories':['recent','recent']},{'categories':[]},
                        {'categories':'recent'},{'categories':[7]}):
            with self.subTest(options=options),self.assertRaises(ArchiveError):
                sync(self.adapter,self.root,**options)
        self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_job_stops_with_its_queue_intact_when_the_free_disk_reserve_is_unavailable(self,output):
        result=archive_discovered(self.adapter,self.root,self.report,min_free_bytes=2**60)
        self.assertEqual(result['status'],'limited');self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(result['pending_episode_count'],2);self.assertEqual(self.adapter.downloads,[])
        self.assertFalse(any(isinstance(v,tuple) and v[0]=='capture' for v in self.adapter.calls))
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'],{'queued':2})
            self.assertEqual(inventory.db.execute("SELECT error_code FROM episodes WHERE id='82'").fetchone()[0],'run_limit')

    @patch('builtins.print')
    def test_free_only_job_drops_an_episode_whose_reader_shows_it_is_no_longer_free(self,output):
        capture=self.adapter.capture_automatic
        def converted(url):
            manifest=capture(url)
            return replace(manifest,episodes=(replace(manifest.episodes[0],access='tapas_unlocked'),))
        self.adapter.capture_automatic=converted
        result=archive_discovered(self.adapter,self.root,self.report,free_only=True)
        self.assertEqual(result['not_free_episode_ids'],['81']);self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(self.adapter.downloads,[]);self.assertEqual(list(self.root.rglob('*.cbz')),[])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT access_state FROM episodes WHERE id='81'").fetchone()[0],'tapas_unlocked')
            self.assertEqual(inventory.pending(True),[])

    @patch('builtins.print')
    def test_archive_that_fails_verification_after_publishing_is_marked_failed_and_repaired_later(self,output):
        with patch('comic_archive.jobs.verify_episode',return_value=['Page hash does not match']):
            with self.assertRaises(ArchiveError):archive_discovered(self.adapter,self.root,self.report,limit=1)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'],{'failed':1,'queued':1})
            self.assertEqual(inventory.status()['last_run']['status'],'paused')
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertIn('81',result['archived_episode_ids'])
        self.assertEqual(result['inventory']['episodes_by_status'],{'archived':2});self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_network_policy_pause_ends_the_run_paused_and_requeues_the_work_on_restart(self,output):
        from comic_archive.network import NetworkPolicyHold
        def held(url):raise NetworkPolicyHold('Metered connection')
        self.adapter.capture_automatic=held
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertEqual((result['status'],result['pause_reason']),('paused','network_policy'))
        self.assertEqual(self.adapter.downloads,[]);self.assertEqual(result['archived_episode_ids'],[])
        with Inventory(self.root) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['82','81'])

    @patch('builtins.print')
    def test_source_refusal_keeps_only_bounded_waits_and_the_exact_retry_candidate(self,output):
        limit=2**31-1
        cases=[(429,'html',120,'82',{'retry_after_seconds':120}),
               (403,'html',30,'82',{'retry_after_seconds':30,'source_retry_episode_id':'82'}),
               (403,'html',None,'81',{}),(403,'challenge',None,'82',{}),
               (403,'html',limit+1,'82',{'source_retry_episode_id':'82'}),
               (403,'html',-1,'82',{'source_retry_episode_id':'82'}),
               (403,'html','30','82',{'source_retry_episode_id':'82'}),
               (403,'html',True,'82',{'source_retry_episode_id':'82'})]
        for index,(status,kind,wait,retry,expected) in enumerate(cases):
            root=Path(self.work.name)/f'Refused{index}'
            def refused(url):
                error=ArchiveError('PRIVATE response');error.source_http_status=status
                error.source_response_kind=kind;error.source_retry_episode_id=retry
                if wait is not None:error.retry_after_seconds=wait
                raise error
            self.adapter.capture_automatic=refused
            with self.subTest(case=index):
                with self.assertRaises(ArchiveError):archive_discovered(self.adapter,root,self.report)
                summary=json.loads((root/'.archive-state/last-run.json').read_text())
                self.assertEqual((summary['pause_reason'],summary['source_http_status'],summary['source_response_kind']),
                                 ('source_refused',status,kind))
                for key in ('retry_after_seconds','source_retry_episode_id'):
                    self.assertEqual(summary.get(key),expected.get(key))
                self.assertNotIn('PRIVATE',json.dumps(summary))
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_history_pages_are_stored_privately_as_they_arrive_and_episode_links_stay_unresolved(self,output):
        state=self.root/'.archive-state';mid_run=[];resolved=[]
        def history(max_pages,on_page=None):
            on_page({'content_links':['https://tapas.io/series/older-owned-title'],'pages':[{'page':1}]})
            snapshot=state/'history.json'
            mid_run.append((json.loads(snapshot.read_text()),snapshot.stat().st_mode&0o777,
                            json.loads((state/'progress.json').read_text())))
            return {'authenticated':True,'pages':[{'page':1},{'page':2}],'blocker':None,
                    'content_links':['https://tapas.io/series/older-owned-title','https://tapas.io/episode/777']}
        self.adapter.inspect_transaction_history=history
        self.adapter.series_identity=lambda key:(resolved.append(key),{'source_series_id':'43','title':'Older title'})[1]
        inspect=self.adapter.inspect_series
        self.adapter.inspect_series=lambda key,strict_order=False:(inspect(key,strict_order) if key=='42' else
            {'authenticated':True,'complete':True,'source_series_id':key,'title':'Older title','episodes':[]})
        sync(self.adapter,self.root,categories=['subscribed'],include_history=True,scan_only=True)
        partial,mode,progress=mid_run[0]
        self.assertEqual(partial['state'],'running');self.assertEqual(partial['content_links'],['https://tapas.io/series/older-owned-title'])
        self.assertEqual(mode,PRIVATE_FILE)
        self.assertEqual((progress['phase'],progress['history_page_count'],progress['observed_history_series_count']),('history',1,1))
        final=json.loads((state/'history.json').read_text())
        self.assertNotIn('state',final);self.assertFalse(final['account_inventory_complete'])
        self.assertEqual(final['unresolved_episode_links'],['https://tapas.io/episode/777'])
        self.assertEqual(len(final['content_links']),2);self.assertEqual((state/'history.json').stat().st_mode&0o777,PRIVATE_FILE)
        self.assertEqual(resolved,['older-owned-title'])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertIn('43',[s['id'] for s in inventory.watched_series()])
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_unverified_account_history_ends_the_run_as_login_required_and_stores_nothing(self,output):
        self.adapter.inspect_transaction_history=lambda max_pages,on_page=None:{'authenticated':False,'content_links':[]}
        with self.assertRaises(AuthenticationError):
            sync(self.adapter,self.root,categories=['subscribed'],include_history=True)
        self.assertFalse((self.root/'.archive-state/history.json').exists())
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['last_run']['status'],'login_required')
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_unexpected_link_in_a_history_page_is_rejected_before_it_is_stored(self,output):
        for links in (['https://evil.example/series/x'],['http://tapas.io/series/x'],['https://tapas.io/episode/0'],
                      ['https://tapas.io/series/a/b'],'https://tapas.io/series/x',[42]):
            def history(max_pages,on_page=None,links=links):
                on_page({'content_links':links,'pages':[]})
                raise AssertionError('The page checkpoint must stop the history walk')
            self.adapter.inspect_transaction_history=history
            with self.subTest(links=links),self.assertRaises(MetadataError):
                sync(self.adapter,self.root,categories=['subscribed'],include_history=True)
            self.assertFalse((self.root/'.archive-state/history.json').exists())
        with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.watched_series(),[])

    @patch('builtins.print')
    def test_reading_list_with_an_unverified_scope_is_rejected_before_anything_is_observed(self,output):
        def page(category,**changes):
            return {'authenticated':True,'membership_verified':True,'scope':category,
                    'series':[{'source_series_id':'99','title':'S'}]}|changes
        class Paged(AccountAdapter):
            supports_library_checkpoint=True
            def __init__(inner,changes):
                super().__init__();inner.changes=changes
            def inspect_library(inner,category,on_page=None):
                on_page(page(category,**inner.changes))
                raise AssertionError('The unverified checkpoint must stop the list')
        for changes in ({'authenticated':False},{'membership_verified':False},{'scope':'updated'}):
            plain=AccountAdapter()
            plain.inspect_library=lambda category,changes=changes:page(category,**changes)
            for adapter in (plain,Paged(changes)):
                with self.subTest(changes=changes,adapter=type(adapter).__name__),self.assertRaises(MetadataError):
                    sync(adapter,self.root,categories=['recent'])
                self.assertEqual(adapter.downloads,[])
        with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.watched_series(),[])

    @patch('builtins.print')
    def test_reading_list_entries_without_a_source_id_are_ignored_not_stored(self,output):
        class Lists(AccountAdapter):
            supports_library_checkpoint=True
            def inspect_library(inner,category,on_page=None):
                head={'authenticated':True,'membership_verified':True,'scope':category}
                on_page(head|{'series':[{'source_series_id':None,'title':'Hidden'},{'source_series_id':'99','title':'Seen'}]})
                return head|{'series':[{'source_series_id':None,'title':'Hidden'},{'source_series_id':'100','title':'Later'}]}
            def inspect_series(inner,key,strict_order=False):
                return {'authenticated':True,'complete':True,'source_series_id':key,'title':'Series '+key,'episodes':[]}
        result=sync(Lists(),self.root,categories=['recent'],scan_only=True)
        self.assertEqual(result['refresh']['state'],'finished')
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(sorted(s['id'] for s in inventory.watched_series()),['100','99'])

    @patch('builtins.print')
    def test_watched_series_answer_that_is_incomplete_or_for_another_series_is_never_queued(self,output):
        with Inventory(self.root) as inventory:inventory.observe('42','S',[],'fixture')
        valid={'authenticated':True,'complete':True,'title':'S',
               'episodes':[{'id':'81','title':'E81','order':1,'access_state':'tapas_free'}]}
        for change in ({'complete':False},{'authenticated':False},{'source_series_id':'99'}):
            self.adapter.inspect_series=lambda key,strict_order=False,change=change:{'source_series_id':key}|valid|change
            with self.subTest(change=change),self.assertRaises(MetadataError):
                sync(self.adapter,self.root,refresh_lists=False)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'],{})
            self.assertIsNone(inventory.status()['last_completed_scan'])
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    @patch('comic_archive.metadata.enrich_with_store')
    def test_series_details_are_fetched_once_per_run_not_after_every_episode(self,enrich,output):
        self.adapter.supports_series_metadata=True
        self.adapter.access={'81':'tapas_free','82':'tapas_free'}
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertCountEqual(result['archived_episode_ids'],['81','82'])
        self.assertEqual(enrich.call_count,1)
        self.assertEqual(enrich.call_args.args[1],'42')
        self.assertEqual(result['enriched_series_ids'],['42'])
        self.assertEqual(result['metadata_unavailable_series'],[])

    @patch('builtins.print')
    def test_series_details_that_are_still_recent_are_not_fetched_again(self,output):
        self.adapter.supports_series_metadata=True
        self.adapter.access={'81':'tapas_free','82':'tapas_free'}
        requested=[]
        def details(key):
            requested.append(key)
            return replace(self.adapter.series,creators=('Artist',),description='A synopsis.',
                           cover_url='https://us-a.tapas.io/cover/page.png')
        self.adapter.series_metadata=details
        self.adapter.retrieve_cover=lambda series,max_bytes=None:png(3)
        first=archive_discovered(self.adapter,self.root,self.report,limit=1)
        self.assertEqual(first['enriched_series_ids'],['42']);self.assertEqual(requested,['42'])
        second=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(len(first['archived_episode_ids']),1);self.assertEqual(len(second['archived_episode_ids']),1)
        self.assertCountEqual(first['archived_episode_ids']+second['archived_episode_ids'],['81','82'])
        self.assertEqual(second['enriched_series_ids'],[]);self.assertEqual(second['metadata_unavailable_series'],[])
        self.assertEqual(requested,['42'])


class StoreContext:
    def __init__(self, root):
        self.store = Store(root)
    def __enter__(self):
        return self.store
    def __exit__(self, *args):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
