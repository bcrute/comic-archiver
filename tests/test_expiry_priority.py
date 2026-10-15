from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.adapters.tapas_http import access_state
from comic_archive.cli import parser, run
from comic_archive.errors import AccessDenied, UnsupportedContent
from comic_archive.expiry import expiration
from comic_archive.inventory import Inventory
from comic_archive.jobs import DownloadBudget, archive_discovered
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_inspection
from comic_archive.worker import execute
from test_jobs import AccountAdapter


class ExpiryPriorityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library';self.session=Path(temporary.name)/'session'
        self.time=datetime(2026,10,8,18,tzinfo=timezone.utc)
        self.patch=patch('comic_archive.expiry.now',return_value=self.time);self.patch.start();self.addCleanup(self.patch.stop)
        self.flags={'must_pay':False,'scheduled':False,'book':False,'free':False,'unlocked':True,'closing_date':None}

    def stamp(self,hours): return (self.time+timedelta(hours=hours)).isoformat()

    def test_temporary_access_requires_an_explicit_readable_grant_and_future_known_timestamp(self):
        self.assertEqual(access_state(self.flags|{'closing_date':self.stamp(1)}),'tapas_unlocked')
        self.assertEqual(access_state(self.flags|{'free':True,'closing_date':self.stamp(1)}),'tapas_free')
        for changes in ({'must_pay':True},{'unlocked':False},{'closing_date':self.stamp(0)},{'closing_date':self.stamp(-1)}):
            with self.assertRaises(AccessDenied): access_state(self.flags|{'closing_date':self.stamp(1)}|changes)
        for value in ('unknown','2026-10-08T20:00:00',True,False,123,{'expiry':'PRIVATE'}):
            with self.assertRaises(UnsupportedContent): access_state(self.flags|{'closing_date':value})
        self.assertEqual(expiration('2026-10-08T14:00:00-05:00'),self.stamp(1))

    def seed(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[
                {'id':'81','title':'Owned','order':1,'access_state':'tapas_unlocked'},
                {'id':'82','title':'Later temporary','order':2,'access_state':'tapas_unlocked','access_expires_at':self.stamp(2)},
                {'id':'83','title':'Soon temporary','order':3,'access_state':'tapas_free','access_expires_at':self.stamp(1)},
                {'id':'84','title':'Free','order':4,'access_state':'tapas_free'}],'fixture')

    def test_expiry_first_owned_first_and_restart_keep_earliest_expiration(self):
        self.seed()
        with Inventory(self.root) as inventory:
            self.assertEqual([r['id'] for r in inventory.pending()],['83','82','81','84'])
            inventory.set_queue_priority('owned_first')
            self.assertEqual([r['id'] for r in inventory.pending()],['81','83','82','84'])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.queue_priority(),'owned_first')
            self.assertEqual([r['id'] for r in inventory.pending()],['81','83','82','84'])
            self.assertEqual(inventory.status()['queue_priority'],'owned_first')

    def test_daily_queue_includes_current_temporary_unlocks_but_excludes_permanent_owned(self):
        self.seed()
        with Inventory(self.root) as inventory:
            self.assertEqual([r['id'] for r in inventory.pending(free_only=True)],['83','82','84'])
        with patch('comic_archive.expiry.now',return_value=self.time+timedelta(hours=3)),Inventory(self.root) as inventory:
            self.assertNotIn('82',[r['id'] for r in inventory.pending(free_only=True)])

    def test_expiring_grants_precede_cooled_asset_retry_without_renewing_its_wait(self):
        self.seed()
        with Inventory(self.root) as inventory:
            inventory.mark('81','queued','asset_read_unavailable')
            before=inventory.db.execute("SELECT retry_after FROM asset_deferrals WHERE episode_id='81'").fetchone()[0]
            with patch('comic_archive.inventory.timestamp',return_value=(self.time+timedelta(days=2)).isoformat()):
                selected,wait=inventory.archive_selection(inventory.pending(),limit=2)
            self.assertEqual([r['id'] for r in selected],['83','82']);self.assertIsNone(wait)
            self.assertEqual(inventory.db.execute("SELECT retry_after FROM asset_deferrals WHERE episode_id='81'").fetchone()[0],before)

    def test_dated_reports_do_not_clear_newer_expiry_and_fresh_permanent_grants_do(self):
        self.seed()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'82','title':'Old','order':2,'access_state':'tapas_unlocked','access_expires_at':None}],
                              'dated',observed_at='2026-10-01T00:00:00+00:00')
            self.assertEqual(next(r for r in inventory.pending() if r['id']=='82')['access_expires_at'],self.stamp(2))
            inventory.record_access('82','tapas_unlocked',expires_at=None)
            self.assertIsNone(next(r for r in inventory.pending() if r['id']=='82')['access_expires_at'])

    def test_download_guard_prevents_new_requests_at_expiry_and_keeps_expiration_private(self):
        adapter=AccountAdapter();budget=DownloadBudget(adapter);budget.access_expires_at=self.stamp(0)
        with self.assertRaises(AccessDenied): budget.retrieve(adapter.capture_automatic('https://tapas.io/episode/81').episodes[0].pages[0])
        self.assertEqual(adapter.downloads,[])
        with self.assertRaises(AccessDenied): budget.asset_request(adapter.capture_automatic('https://tapas.io/episode/81').episodes[0].pages[0])

    @patch('builtins.print')
    def test_daily_job_saves_temporary_unlocked_grant_when_fresh_checks_confirm_it(self,output):
        adapter=AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'82','title':'Temporary','order':2,'access_state':'tapas_unlocked','access_expires_at':self.stamp(1)}],'fixture')
        original_access=adapter.episode_access;original_capture=adapter.capture_automatic
        adapter.episode_access=lambda url:original_access(url)|{'access_expires_at':self.stamp(1)}
        def capture(url):
            manifest=original_capture(url)
            return replace(manifest,episodes=(replace(manifest.episodes[0],access_expires_at=self.stamp(1)),))
        adapter.capture_automatic=capture
        result=archive_discovered(adapter,self.root,free_only=True)
        self.assertEqual(result['archived_episode_ids'],['82'])
        metadata=json.loads(next(self.root.glob('*/*.json')).read_text())
        self.assertNotIn('access_expires_at',metadata)

    @patch('builtins.print')
    def test_changed_access_during_daily_recheck_skips_without_requesting_a_reader(self,output):
        adapter=AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'82','title':'Temporary','order':2,'access_state':'tapas_unlocked','access_expires_at':self.stamp(1)}],'fixture')
        result=archive_discovered(adapter,self.root,free_only=True)
        self.assertEqual(result['archived_episode_ids'],[]);self.assertEqual(adapter.downloads,[])
        self.assertFalse(any(isinstance(c,tuple) and c[0]=='capture' for c in adapter.calls))

    def test_settings_queue_is_fixed_no_source_actions_and_cli_can_use_the_worker(self):
        requests=self.session/'worker/requests';requests.mkdir(parents=True)
        task=submit_inspection(self.session,'configure_priority',{'priority':'owned_first'})
        request=json.loads((requests/(task['task_id']+'.json')).read_text())
        adapter=AccountAdapter()
        with patch('comic_archive.worker.Path') as path:
            path.return_value=self.root
            result=execute(adapter,request)
        self.assertEqual(result,{'queue_priority':'owned_first'});self.assertEqual(adapter.calls,[])
        for opts in ({'priority':'invalid'},{'priority':'owned_first','account':'PRIVATE'},{'priority':[]}):
            with self.assertRaises(ArchiveError): submit_inspection(self.session,'configure_priority',opts)
        args=parser().parse_args(['--session-dir',str(self.session),'queue-priority','expiring_first','--via-worker'])
        with patch('builtins.print'):self.assertEqual(run(args),0)
        self.assertEqual(len(list(requests.glob('*.json'))),2)

    def test_selection_follows_the_chosen_queue_order_for_expiring_and_owned_episodes(self):
        self.seed()
        with Inventory(self.root) as inventory:
            selected,wait=inventory.archive_selection(inventory.pending(),limit=3)
            self.assertEqual([r['id'] for r in selected],['83','82','81']);self.assertIsNone(wait)
            inventory.set_queue_priority('owned_first')
            selected,wait=inventory.archive_selection(inventory.pending(),limit=3)
            self.assertEqual([r['id'] for r in selected],['81','83','82']);self.assertIsNone(wait)


if __name__=='__main__': unittest.main()
