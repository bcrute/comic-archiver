"""Purchased/unlocked preservation must not wait behind the free backlog."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from test_jobs import AccountAdapter


class QueuePriorityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library';self.adapter=AccountAdapter()

    def observe(self,series='42',free='81',unlocked='82'):
        with Inventory(self.root) as inventory:
            inventory.observe(series,'Series',[
                {'id':free,'title':'Free','order':1,'access_state':'tapas_free'},
                {'id':unlocked,'title':'Unlocked','order':2,'access_state':'tapas_unlocked'},
            ],'fixture')

    def test_bulk_queue_prioritizes_existing_unlocks_with_stable_order_and_failed_resume(self):
        self.observe('43','91','92');self.observe()
        with Inventory(self.root) as inventory:
            inventory.mark('82','failed','network_unavailable')
            self.assertEqual([row['id'] for row in inventory.pending()],['82','92','81','91'])
            self.assertEqual([row['id'] for row in inventory.pending(free_only=True)],['81','91'])

    @patch('builtins.print')
    def test_one_episode_budget_archives_unlocked_first_after_fresh_capture(self,output):
        self.observe()
        result=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(result['priority_policy'],'unlocked_first')
        self.assertEqual(result['archived_episode_ids'],['82'])
        self.assertEqual(self.adapter.calls[0],'identity')
        self.assertIn(('capture','82'),self.adapter.calls)
        self.assertNotIn(('capture','81'),self.adapter.calls)
        self.assertEqual(self.adapter.downloads,[1,2])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])

    @patch('builtins.print')
    def test_revoked_unlock_never_downloads_from_old_queue_evidence(self,output):
        self.observe();self.adapter.denied={'82'}
        result=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(result['unavailable_episode_ids'],['82'])
        self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(self.adapter.downloads,[])
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])

    @patch('builtins.print')
    def test_daily_free_only_keeps_unlocks_queued(self,output):
        self.observe()
        result=archive_discovered(self.adapter,self.root,free_only=True,limit=1)
        self.assertEqual(result['priority_policy'],'recent_free')
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertNotIn(('capture','82'),self.adapter.calls)
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()],['82'])
