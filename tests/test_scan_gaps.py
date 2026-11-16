import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.adapters.pagination import EpisodeCountMismatch
from comic_archive.audit import audit_library
from comic_archive.inventory import Inventory
from comic_archive.jobs import sync
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


def answered(key,strict_order=False):
    return {'complete':True,'authenticated':True,'source_series_id':key,'title':'S','episodes':[]}


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
        for status,key,route in ((403,'42','/series/42'),(404,'99','/series/42'),(404,'42','/series/43/episodes')):
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
    def test_a_second_check_with_the_same_short_list_finishes_the_series(self,output):
        self.adapter.inspect_series=self.partial_inspection
        first=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,limit=2)
        self.assertEqual(first['refresh']['held_series_checks'],1)
        with Inventory(self.root) as inventory:
            self.assertEqual(inventory.prepare_completion_retry()[0],1)
        second=sync(self.adapter,self.root,refresh_lists=False,scan_limit=1,limit=2)
        self.assertEqual((second['refresh']['held_series_checks'],second['refresh']['successful_series_checks']),(0,1))
        self.assertEqual(second['refresh']['series_scan_gaps'],[])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['series_scan_gap_count'],0)
            self.assertIsNotNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())

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
    def test_wrong_series_gap_stops_the_run(self,output):
        def fail(key,strict_order=False):raise EpisodeCountMismatch('999',2,3)
        self.adapter.inspect_series=fail
        with self.assertLogs('comic_archive.download','INFO') as logged, self.assertRaises(ArchiveError):
            sync(self.adapter,self.root,refresh_lists=False,limit=1)
        # The log names the series the step stopped on.
        self.assertIn('INFO:comic_archive.download:series check stopped: series=42 error=EpisodeCountMismatch',logged.output)
        with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_the_same_failure_in_three_series_in_a_row_stops_the_run_and_sets_none_aside(self,output):
        calls=[]
        def fail(key,strict_order=False):
            calls.append(key);raise ArchiveError('Unsupported response')
        self.adapter.inspect_series=fail
        for _ in range(2):  # Every later step meets the same: the fault is never hidden as set-aside series.
            calls.clear()
            with self.assertLogs('comic_archive.download','INFO') as logged, self.assertRaises(ArchiveError):
                sync(self.adapter,self.root,refresh_lists=False,limit=1)
            self.assertEqual(calls,['42','43','44'])
            self.assertIn('INFO:comic_archive.download:series check stopped: series=44 error=ArchiveError '
                          'reason=same_failure_in_a_row count=3',logged.output)
            with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])
        self.assertEqual(self.adapter.downloads,[])

    @patch('builtins.print')
    def test_one_series_with_unreadable_data_is_set_aside_and_the_rest_are_checked(self,output):
        from comic_archive.errors import MetadataError
        inspect=answered
        calls=[]
        def inspection(key,strict_order=False):
            calls.append(key)
            if key=='42':raise MetadataError('Tapas pagination repeated an episode; completeness is unverified.')
            return inspect(key,strict_order)
        self.adapter.inspect_series=inspection
        with Inventory(self.root) as inventory:
            for key in ('45','46'):inventory.observe(key,'Series '+key,[],'fixture')
        with self.assertLogs('comic_archive.download','INFO') as logged:
            result=sync(self.adapter,self.root,refresh_lists=False,limit=1)
        self.assertEqual(calls,['42','43','44','45','46'])
        self.assertIn('WARNING:comic_archive.download:series set aside: series=42 error=MetadataError '
                      'reason=series_data_unreadable checked_again=after_the_rest',logged.output)
        self.assertEqual(result['refresh']['state'],'partial')
        self.assertEqual(result['refresh']['held_series_checks'],1)
        self.assertEqual(result['refresh']['successful_series_checks'],4)
        with Inventory(self.root) as inventory:
            status=inventory.status()
            self.assertEqual([gap['code'] for gap in status['series_scan_gaps']],['series_set_aside'])
            # Never recorded as checked: it stays incomplete.
            self.assertIsNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())
            self.assertIsNone(status['last_completed_scan'])
            # The next round checks it after every other series, never first, though it was never checked.
            inventory.observe('47','Series 47',[],'fixture')
            self.assertEqual([row['id'] for row in inventory.scan_candidates()],['47','43','44','45','46','42'])
            # Continue retries it.
            self.assertEqual(inventory.prepare_completion_retry()[0],1)
            self.assertEqual([row['id'] for row in inventory.scan_candidates()],['42'])
        calls.clear()
        self.adapter.inspect_series=lambda key,strict_order=False:(calls.append(key),inspect(key,strict_order))[1]
        sync(self.adapter,self.root,refresh_lists=False,limit=1)
        self.assertEqual(calls[0],'42')
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.scan_gaps(),[])
            self.assertIsNotNone(inventory.db.execute("SELECT 1 FROM series_scans WHERE series_id='42'").fetchone())

    @patch('builtins.print')
    def test_different_failures_do_not_add_up_and_a_success_breaks_the_run(self,output):
        from comic_archive.errors import MetadataError, UnsupportedContent
        with Inventory(self.root) as inventory:
            for key in ('45','46','47'):inventory.observe(key,'Series '+key,[],'fixture')
        inspect=answered
        # A different failure starts the count again, and so does a series checked in between.
        failures={'42':MetadataError('One'),'43':MetadataError('One'),'44':UnsupportedContent('Two'),
                  '46':UnsupportedContent('Two'),'47':UnsupportedContent('Two')}
        def inspection(key,strict_order=False):
            if key in failures:raise failures[key]
            return inspect(key,strict_order)
        self.adapter.inspect_series=inspection
        result=sync(self.adapter,self.root,refresh_lists=False,limit=1)
        self.assertEqual(result['refresh']['held_series_checks'],5)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(sorted(gap['source_series_id'] for gap in inventory.scan_gaps()),['42','43','44','46','47'])

    @patch('builtins.print')
    def test_inconsistent_or_invalid_series_answers_are_set_aside(self,output):
        inspect=answered
        answers={'42':{'complete':False},'43':{'source_series_id':'99'},'44':{'episodes':[{'id':'../x','title':'E'}]}}
        self.adapter.inspect_series=lambda key,strict_order=False:inspect(key,strict_order)|answers[key]
        with Inventory(self.root) as inventory:inventory.observe('45','Series 45',[],'fixture')
        answers['45']={}
        result=sync(self.adapter,self.root,refresh_lists=False,limit=1)
        self.assertEqual(result['refresh']['successful_series_checks'],1)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(sorted(gap['source_series_id'] for gap in inventory.scan_gaps()),['42','43','44'])

    @patch('builtins.print')
    def test_account_connection_refusal_and_limits_still_stop_the_run(self,output):
        from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError, NetworkUnavailable, RunLimit
        from comic_archive.network import NetworkPolicyHold
        def refused(status):
            error=ArchiveError('Refused');error.source_http_status=status;return error
        for error in (AuthenticationError('Signed out'),AccessDenied('No'),NetworkUnavailable('Down'),RunLimit('Limit'),
                      NetworkPolicyHold('Wi-Fi'),refused(429),refused(403),refused(401),refused(503),refused(500),
                      MetadataError('Not signed in')):
            calls=[]
            def fail(key,strict_order=False,error=error):
                calls.append(key);raise error
            self.adapter.inspect_series=fail
            if str(error)=='Not signed in':
                self.adapter.inspect_series=lambda key,strict_order=False:(calls.append(key),{'authenticated':False})[1]
            try:  # Some of these the run reports as its result rather than raising; none goes on to another series.
                sync(self.adapter,self.root,refresh_lists=False,limit=1)
            except ArchiveError as raised:
                self.assertIsInstance(raised,type(error))
            self.assertEqual(calls,['42'],error)
            with Inventory(self.root,read_only=True) as inventory:self.assertEqual(inventory.scan_gaps(),[])

    def test_set_aside_needs_a_known_series_and_forgetting_leaves_other_gaps(self):
        with Inventory(self.root) as inventory:
            for key in ('999','../private'):
                with self.assertRaises(ArchiveError):inventory.record_series_set_aside(key)
            inventory.record_series_set_aside('43');inventory.record_scan_gap('42',2,3)
            inventory.forget_series_set_aside(['42','43'])
            self.assertEqual([gap['source_series_id'] for gap in inventory.scan_gaps()],['42'])

    def test_invalid_scan_gap_cannot_create_unknown_series_or_private_codes(self):
        with Inventory(self.root) as inventory:
            for key,seen,total in [('999',2,3),('../private',2,3),('42',True,3),('42',2,-1)]:
                with self.assertRaises(ArchiveError):inventory.record_scan_gap(key,seen,total)
            self.assertEqual(inventory.scan_gaps(),[])


if __name__=='__main__':unittest.main()
