import json
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comic_archive.archive import verify
from comic_archive.errors import AssetReadUnavailable, NetworkUnavailable
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


@patch('builtins.print')
class AssetDeferralTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026,10,8,14,tzinfo=timezone.utc)
        clock = patch('comic_archive.inventory.timestamp', side_effect=lambda:self.now.isoformat())
        clock.start();self.addCleanup(clock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = AccountAdapter()
        self.adapter.access = {str(k):'tapas_unlocked' for k in range(81,87)}
        self.key = None
        capture = self.adapter.capture_automatic
        def tracked(url):
            self.key = url.rsplit('/',1)[1]
            return capture(url)
        self.adapter.capture_automatic = tracked
        self.retrieve = self.adapter.retrieve
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':str(k),'title':'Episode','order':k-80,
                'access_state':'tapas_unlocked'} for k in range(81,87)],'fixture')

    def fail(self):
        error = AssetReadUnavailable('PRIVATE response details')
        error.network_failure_code = 'read_timeout'
        raise error

    def test_isolated_failed_asset_retains_pages_and_continues_then_resumes(self,output):
        def retrieve(page,max_bytes=None):
            if self.key == '81' and page.order == 2:
                self.fail()
            return self.retrieve(page,max_bytes)
        self.adapter.retrieve = retrieve
        first = archive_discovered(self.adapter,self.root,limit=2)
        self.assertEqual(first['status'],'limited')
        self.assertEqual(first['archived_episode_ids'],['82'])
        self.assertEqual(first['deferred_asset_episode_ids'],['81'])
        self.assertEqual(first['downloaded_pages'],3)
        self.assertNotIn('PRIVATE',json.dumps(first))
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([r['id'] for r in inventory.pending()],['83','84','85','86','81'])
            self.assertEqual(inventory.status()['episodes_by_status']['queued'],5)
        self.adapter.retrieve = self.retrieve
        self.adapter.downloads.clear()
        self.now += timedelta(seconds=300)
        second = archive_discovered(self.adapter,self.root)
        self.assertEqual(second['status'],'finished')
        self.assertEqual(second['archived_episode_ids'],['81','83','84','85','86'])
        self.assertEqual(second['downloaded_pages'],9)
        self.assertEqual(verify(self.root),[])

    def test_three_consecutive_asset_failures_pause_before_fourth_episode(self,output):
        self.adapter.retrieve = lambda *a,**k:self.fail()
        result = archive_discovered(self.adapter,self.root)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['pause_reason'],'network_unavailable')
        self.assertEqual(result['deferred_asset_episode_ids'],['81','82','83'])
        self.assertEqual(result['network_failure_code'],'read_timeout')
        self.assertNotIn(('capture','84'),self.adapter.calls)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(len(inventory.pending()),6)

    def test_unchanged_discovery_refresh_preserves_deferred_queue_position(self,output):
        with Inventory(self.root) as inventory:
            inventory.mark('81','queued','asset_read_unavailable')
            inventory.observe('42','Series',[{'id':'81','title':'Episode','order':1,
                'access_state':'tapas_unlocked'}],'fresh_fixture')
            self.assertEqual([r['id'] for r in inventory.pending()],['82','83','84','85','86','81'])
            self.assertEqual(inventory.pending()[-1]['error_code'],'asset_read_unavailable')

    def test_metadata_outage_is_not_deferred_or_followed_by_more_captures(self,output):
        def capture(url):
            raise NetworkUnavailable('PRIVATE metadata failure')
        self.adapter.capture_automatic = capture
        result = archive_discovered(self.adapter,self.root)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['deferred_asset_episode_ids'],[])
        self.assertEqual(self.adapter.downloads,[])
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_five_total_failures_pause_even_with_successes_between_them(self,output):
        self.adapter.access = {str(k):'tapas_unlocked' for k in range(81,92)}
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':str(k),'title':'Episode','order':k-80,
                'access_state':'tapas_unlocked'} for k in range(81,92)],'fixture')
        def retrieve(page,max_bytes=None):
            if int(self.key)%2:
                self.fail()
            return self.retrieve(page,max_bytes)
        self.adapter.retrieve = retrieve
        result = archive_discovered(self.adapter,self.root)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['deferred_asset_episode_ids'],['81','83','85','87','89'])
        self.assertEqual(result['archived_episode_ids'],['82','84','86','88'])
        self.assertNotIn(('capture','90'),self.adapter.calls)

    def test_http_refusal_is_not_deferred(self,output):
        def retrieve(*args,**kwargs):
            raise ArchiveError('Tapas rate limit reached; this job has stopped.')
        self.adapter.retrieve = retrieve
        with self.assertRaises(ArchiveError):
            archive_discovered(self.adapter,self.root)
        self.assertNotIn(('capture','82'),self.adapter.calls)
        summary = json.loads((self.root/'.archive-state/last-run.json').read_text())
        self.assertEqual(summary['deferred_asset_episode_ids'],[])

    def test_deferral_does_not_override_new_authorization_denial(self,output):
        self.adapter.retrieve = lambda *a,**k:self.fail()
        archive_discovered(self.adapter,self.root,limit=1)
        self.adapter.retrieve = self.retrieve
        self.adapter.denied.add('81')
        self.now += timedelta(seconds=300)
        result = archive_discovered(self.adapter,self.root)
        self.assertEqual(result['unavailable_episode_ids'],['81'])
        self.assertEqual(result['archived_episode_ids'],['82','83','84','85','86'])
        self.assertEqual(result['downloaded_pages'],10)

    def test_one_cooled_retry_precedes_large_healthy_queue_and_preserves_partial_cache(self,output):
        def retrieve(page,max_bytes=None):
            if self.key in {'81','82'} and page.order == 2:
                self.fail()
            return self.retrieve(page,max_bytes)
        self.adapter.retrieve=retrieve
        first=archive_discovered(self.adapter,self.root,limit=2)
        self.assertEqual(first['deferred_asset_episode_ids'],['81','82'])
        self.assertEqual(first['downloaded_pages'],2)
        self.now += timedelta(seconds=300)
        self.adapter.retrieve=self.retrieve
        second=archive_discovered(self.adapter,self.root,limit=2)
        self.assertEqual(second['archived_episode_ids'],['81','83'])
        self.assertEqual(second['downloaded_pages'],3)
        third=archive_discovered(self.adapter,self.root,limit=1,max_pages=1)
        self.assertEqual(third['archived_episode_ids'],['82'])
        self.assertEqual(third['downloaded_pages'],1)
        self.assertEqual(verify(self.root),[])

    def test_repeated_failure_renews_only_its_cooldown_and_refresh_cannot_reset_it(self,output):
        with Inventory(self.root) as inventory:
            inventory.mark('81','queued','asset_read_unavailable')
            inventory.mark('82','queued','asset_read_unavailable')
        self.now += timedelta(seconds=300)
        self.adapter.retrieve=lambda *a,**k:self.fail()
        retry=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(retry['deferred_asset_episode_ids'],['81'])
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'Episode','order':1,
                              'access_state':'tapas_unlocked'}],'refresh')
            selected,wait=inventory.archive_selection(inventory.pending(),1)
            self.assertEqual([row['id'] for row in selected],['82'])
            self.assertIsNone(wait)
        self.adapter.retrieve=self.retrieve
        self.assertEqual(archive_discovered(self.adapter,self.root,limit=1)['archived_episode_ids'],['82'])
        self.now += timedelta(seconds=300)
        self.assertEqual(archive_discovered(self.adapter,self.root,limit=1)['archived_episode_ids'],['81'])

    def test_only_cooling_episodes_remain_queued_without_reader_or_asset_requests(self,output):
        with Inventory(self.root) as inventory:
            for row in inventory.pending():inventory.mark(row['id'],'queued','asset_read_unavailable')
        calls=len(self.adapter.calls)
        result=archive_discovered(self.adapter,self.root)
        self.assertEqual(result['status'],'limited')
        self.assertEqual(result['asset_retry_after_seconds'],300)
        self.assertEqual(result['pending_episode_count'],6)
        self.assertEqual(result['downloaded_pages'],0)
        self.assertEqual(self.adapter.calls[calls:],['identity'])
        self.assertEqual(self.adapter.downloads,[])

    def test_legacy_deferrals_upgrade_without_renewing_their_wait(self,output):
        with Inventory(self.root) as inventory:
            inventory.mark('81','queued','asset_read_unavailable')
            inventory.db.execute('DROP TABLE asset_deferrals');inventory.db.commit()
        self.now += timedelta(seconds=301)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.archive_selection(inventory.pending(),1)[0][0]['id'],'81')
        with Inventory(self.root) as inventory:
            self.assertEqual(inventory.archive_selection(inventory.pending(),1)[0][0]['id'],'81')

    def test_source_refusal_on_cooled_retry_stops_before_healthy_assets(self,output):
        with Inventory(self.root) as inventory:inventory.mark('81','queued','asset_read_unavailable')
        self.now += timedelta(seconds=300)
        def refused(url):
            error=ArchiveError('PRIVATE response');error.source_http_status=403;raise error
        self.adapter.capture_automatic=refused
        with self.assertRaises(ArchiveError):archive_discovered(self.adapter,self.root)
        result=json.loads((self.root/'.archive-state/last-run.json').read_text())
        self.assertEqual(result['pause_reason'],'source_refused')
        self.assertEqual(result['deferred_asset_episode_ids'],[])
        self.assertEqual(result['downloaded_pages'],0)
        self.assertNotIn('asset_retry_after_seconds',result)
        self.assertEqual(self.adapter.downloads,[])

    def test_uncertified_asset_subtype_keeps_global_network_hold(self,output):
        def unavailable(*a,**k):
            error=AssetReadUnavailable('PRIVATE connection failure')
            error.network_failure_code='connect_timeout'
            raise error
        self.adapter.retrieve=unavailable
        result=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(result['pause_reason'],'network_unavailable')
        self.assertEqual(result['deferred_asset_episode_ids'],[])
        self.assertNotIn('asset_retry_after_seconds',result)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(list(inventory.db.execute('SELECT * FROM asset_deferrals')),[])

    def test_partial_retry_keeps_priority_across_page_limits_and_interruption(self,output):
        with Inventory(self.root) as inventory:inventory.mark('81','queued','asset_read_unavailable')
        self.now += timedelta(seconds=300)
        partial=archive_discovered(self.adapter,self.root,limit=1,max_pages=1)
        self.assertEqual(partial['downloaded_pages'],1)
        self.assertEqual(partial['archived_episode_ids'],[])
        with Inventory(self.root) as inventory:inventory.mark('81','downloading',attempted=True)
        with Inventory(self.root) as inventory:
            selected,_=inventory.archive_selection(inventory.pending(),1)
            self.assertEqual(selected[0]['id'],'81')
        repaired=archive_discovered(self.adapter,self.root,limit=1,max_pages=1)
        self.assertEqual(repaired['archived_episode_ids'],['81'])
        self.assertEqual(repaired['downloaded_pages'],1)
        self.assertEqual(verify(self.root),[])
