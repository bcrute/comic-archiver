import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.adapters.pagination import EpisodeCountMismatch
from comic_archive.audit import audit_library
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_watched_series, sync
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


class ScanGapTests(unittest.TestCase):
    @patch('builtins.print')
    def test_novel_series_hold_preserves_prior_archive_skips_cycle_and_later_recovers(self,output):
        from comic_archive.archive import Store
        store=Store(self.root)
        try:
            folder=store.archive(self.adapter.capture_automatic('https://tapas.io/episode/81'),self.adapter,progress=lambda _:None)
        finally:
            store.close()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'E81','order':1,
                'access_state':'tapas_free'}],'fixture')
            inventory.reconcile()
        saved={path.name:path.read_bytes() for path in folder.iterdir() if path.is_file()}
        self.adapter.downloads.clear();self.adapter.calls.clear()
        self.adapter.supports_comic_series_classification=True
        calls=[]
        def inspect(key,strict_order=False,comic_only=False):
            self.assertTrue(comic_only);calls.append(key)
            if key=='42':
                return {'authenticated':True,'complete':False,'source_series_id':key,'title':'Novel',
                    'episodes':[],'episode_enumeration_skipped':True,'unsupported_series_reason':'novel',
                    'series_content_flags':{'book':True,'comic':False,'token':'PRIVATE'}}
            return {'authenticated':True,'complete':True,'source_series_id':key,'title':'S','episodes':[]}
        self.adapter.inspect_series=inspect
        first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=2,limit=1)
        self.assertEqual(calls,['42','43']);self.assertEqual(first['refresh']['held_series_checks'],1)
        self.assertEqual(first['refresh']['successful_series_checks'],1)
        self.assertEqual(first['refresh']['series_scan_gaps'][0]['code'],'unsupported_novel_series')
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.scan_candidates()],['44'])
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())
            self.assertEqual(inventory.status()['episodes_by_status']['archived'],1)
        self.assertEqual({path.name:path.read_bytes() for path in folder.iterdir() if path.is_file()},saved)
        self.assertNotIn('PRIVATE',json.dumps(first));self.assertEqual(self.adapter.downloads,[])
        sync(self.adapter,self.root,refresh_lists=False,scan_limit=3,limit=1)
        self.adapter.inspect_series=lambda key,**kwargs:{'authenticated':True,'complete':True,'source_series_id':key,'title':'S','episodes':[]}
        sync(self.adapter,self.root,refresh_lists=False,scan_limit=3,limit=1)
        with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])

    @patch('builtins.print')
    def test_novel_series_skip_requires_exact_identity_and_boolean_evidence(self,output):
        from comic_archive.errors import MetadataError
        self.adapter.supports_comic_series_classification=True
        base={'authenticated':True,'complete':False,'source_series_id':'42','episodes':[],
              'episode_enumeration_skipped':True,'unsupported_series_reason':'novel','series_content_flags':{'book':True,'comic':False}}
        for change in ({'source_series_id':'99'},{'authenticated':False},{'complete':True},
                       {'series_content_flags':{'book':1,'comic':False}},
                       {'series_content_flags':{'book':True,'comic':0}}, {'episodes':[{'id':'81'}]}):
            self.adapter.inspect_series=lambda *args,**kwargs:base|change
            with self.assertRaises(MetadataError):sync(self.adapter,self.root,refresh_lists=False,limit=1)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.scan_gaps(),[])
            with self.assertRaises(ArchiveError):inventory.record_novel_series_hold('99')
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_missing_metadata_retains_series_skips_only_this_cycle_and_later_recovers(self,output):
        from comic_archive.errors import MetadataError
        calls=[]
        def inspect(key,strict_order=False):
            calls.append(key)
            if key=='42':
                error=MetadataError('PRIVATE_SOURCE');error.source_http_status=404
                error.source_series_id=key;error.source_route='/series/'+key;raise error
            return {'complete':True,'authenticated':True,'source_series_id':key,'title':'S','episodes':[]}
        self.adapter.inspect_series=inspect
        first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=2,limit=1)
        self.assertEqual(calls,['42','43']);self.assertFalse(first['inventory_complete'])
        self.assertEqual({key:first['refresh'][key] for key in ('series_checks_completed',
            'successful_series_checks','held_series_checks','series_check_target_count')},
            {'series_checks_completed':2,'successful_series_checks':1,'held_series_checks':1,'series_check_target_count':2})
        self.assertEqual(first['refresh']['series_scan_gaps'][0]['code'],'metadata_http_404')
        self.assertNotIn('PRIVATE',json.dumps(first))
        second=sync(self.adapter,self.root,refresh_lists=False,scan_limit=2,limit=1)
        self.assertEqual(calls,['42','43','44'])
        self.assertEqual(second['refresh']['series_checks_completed'],1)
        self.assertEqual(second['refresh']['successful_series_checks'],1)
        self.assertEqual(second['refresh']['held_series_checks'],0)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertIsNone(inventory.status()['last_completed_scan'])
            self.assertEqual(inventory.scan_gaps()[0]['source_http_status'],404)
        self.adapter.inspect_series=lambda key,strict_order=False:{'complete':True,'authenticated':True,'source_series_id':key,'title':'S','episodes':[]}
        sync(self.adapter,self.root,refresh_lists=False,scan_limit=3,limit=1)
        with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_metadata_hold_never_swallows_wrong_identity_route_or_authorization_refusal(self,output):
        from comic_archive.errors import MetadataError
        for status,key,route in ((403,'42','/series/42'),(404,'99','/series/42'),(404,'42','/series/42/episodes')):
            error=MetadataError('PRIVATE_SOURCE');error.source_http_status=status
            error.source_series_id=key;error.source_route=route
            self.adapter.inspect_series=lambda *args,**kwargs:(_ for _ in ()).throw(error)
            with self.assertRaises(MetadataError):sync(self.adapter,self.root,refresh_lists=False,limit=1)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.scan_gaps(),[])
            for key,status in (('99',404),('42',403),('42',True)):
                with self.assertRaises(ArchiveError):inventory.record_series_unavailable(key,status)
    def partial_inspection(self,key,strict_order=False):
        error=EpisodeCountMismatch(key,2,3)
        error.partial_inspection={'complete':False,'authenticated':True,'source_series_id':key,
            'title':'Series '+key,'episode_count':2,'episodes':[
                {'id':'81','title':'First','order':1,'access_state':'tapas_free'},
                {'id':'82','title':'Second','order':2,'access_state':'tapas_unlocked'}]}
        raise error

    @patch('builtins.print')
    def test_sync_retains_explicit_candidates_but_keeps_the_incomplete_series_gap(self,output):
        self.adapter.inspect_series=self.partial_inspection
        result=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,limit=1)
        self.assertEqual(result['archived_episode_ids'],['82'])
        self.assertEqual(result['refresh']['state'],'partial')
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episode_count'],2)
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())
            self.assertEqual(inventory.status()['series_scan_gap_count'],1)

    @patch('builtins.print')
    def test_selected_gapped_series_rechecks_access_and_does_not_guess_missing_episode(self,output):
        self.adapter.inspect_series=self.partial_inspection;self.adapter.denied={'82'}
        result=archive_watched_series(self.adapter,'42',self.root,limit=2)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertEqual(result['unavailable_episode_ids'],['82'])
        self.assertEqual(self.adapter.downloads,[1,2])
        self.assertEqual(result['refresh']['state'],'partial');self.assertFalse(result['inventory_complete'])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episode_count'],2)
            self.assertEqual(inventory.status()['series_scan_gap_count'],1)
            self.assertIsNone(inventory.status()['last_completed_scan'])

    @patch('builtins.print')
    def test_partial_payload_for_a_different_series_is_rejected_before_queueing_or_retrieval(self,output):
        def wrong(key,strict_order=False):
            try:self.partial_inspection(key,strict_order)
            except EpisodeCountMismatch as error:
                error.partial_inspection['source_series_id']='999'
                raise
        self.adapter.inspect_series=wrong
        with self.assertRaises(ArchiveError):archive_watched_series(self.adapter,'42',self.root,limit=1)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episode_count'],0)
            self.assertEqual(inventory.scan_gaps(),[])
        self.assertEqual(self.adapter.downloads,[])

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library'
        with Inventory(self.root) as inventory:
            for key in ('42','43','44'):inventory.observe(key,'Series '+key,[],'fixture')
        self.adapter=AccountAdapter()
        inspect=self.adapter.inspect_series
        def inspection(key,strict_order=False):
            if key=='42':
                self.adapter.calls.append(('gap',key))
                raise EpisodeCountMismatch(key,2,3)
            return inspect(key,strict_order)|{'episodes':[]}
        self.adapter.inspect_series=inspection

    @patch('builtins.print')
    def test_gap_is_retained_without_marking_a_successful_checkpoint_and_other_scans_continue(self,output):
        first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=2,limit=1)
        self.assertEqual(first['refresh']['state'],'partial')
        gap=first['refresh']['series_scan_gaps'][0]
        self.assertEqual(gap['source_series_id'],'42')
        self.assertEqual(gap['observed_episode_count'],2);self.assertEqual(gap['reported_episode_count'],3)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['scan_remaining_series_count'],1)
            self.assertEqual([row['id'] for row in inventory.scan_candidates()],['44'])
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())
        self.assertEqual(self.adapter.calls.count(('gap','42')),1)
        second=sync(self.adapter,self.root,refresh_lists=False,scan_limit=2,limit=1)
        self.assertEqual(self.adapter.calls.count(('gap','42')),1)
        self.assertEqual(second['refresh']['state'],'partial')
        with Inventory(self.root,read_only=True) as inventory:
            status=inventory.status()
            self.assertIsNone(status['scan_cycle_started'])
            self.assertIsNone(status['last_completed_scan'])
            self.assertEqual(status['series_scan_gap_count'],1)
            self.assertEqual(status['series_without_scan_checkpoint_count'],1)
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_later_complete_observation_clears_the_gap_and_restores_normal_daily_cycles(self,output):
        sync(self.adapter,self.root,refresh_lists=False,scan_limit=3,limit=1)
        self.adapter.inspect_series=lambda key,strict_order=False:{'complete':True,'authenticated':True,
            'source_series_id':key,'title':'Series '+key,'episodes':[]}
        result=sync(self.adapter,self.root,refresh_lists=False,scan_limit=3,limit=1)
        self.assertEqual(result['refresh']['state'],'finished')
        with Inventory(self.root,read_only=True) as inventory:
            status=inventory.status()
            self.assertEqual(status['series_scan_gap_count'],0)
            self.assertIsNotNone(status['last_completed_scan'])
            self.assertIsNone(status['scan_cycle_started'])

    def test_a_previous_successful_scan_does_not_hide_a_new_gap_in_portable_audit(self):
        with Inventory(self.root) as inventory:
            for key in ('42','43','44'):inventory.record_series_scan(key)
            inventory.record_scan_gap('42',2,3)
        report=audit_library(self.root)
        self.assertFalse(report['observed_snapshot_archived'])
        self.assertEqual(report['series_scan_gap_count'],1)
        self.assertIn('series_episode_counts_unreconciled',report['coverage_blockers'])

    @patch('builtins.print')
    def test_wrong_series_gap_or_general_schema_error_stops_the_run(self,output):
        for error in (EpisodeCountMismatch('999',2,3),ArchiveError('Unsupported response')):
            def fail(key,strict_order=False):raise error
            self.adapter.inspect_series=fail
            with self.assertRaises(ArchiveError):sync(self.adapter,self.root,refresh_lists=False,limit=1)
            with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])
        self.assertEqual(self.adapter.downloads,[])

    def test_invalid_scan_gap_cannot_create_unknown_series_or_private_codes(self):
        with Inventory(self.root) as inventory:
            for key,seen,total in [('999',2,3),('../private',2,3),('42',True,3),('42',2,-1)]:
                with self.assertRaises(ArchiveError):inventory.record_scan_gap(key,seen,total)
            self.assertEqual(inventory.scan_gaps(),[])


if __name__=='__main__':unittest.main()
