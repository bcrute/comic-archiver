"""History continuation tests use synthetic cursors, never live finances."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.errors import AuthenticationError, RunLimit
from comic_archive.models import ArchiveError


class HistoryNavigationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TapasAdapter(Path('unused-session'))
        self.adapter.network_guard = Mock()
        self.main = Mock()
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.adapter.page = self.main
        self.adapter.context = Mock()
        self.adapter.context.new_page.return_value = self.page
        self.adapter.account_identity = Mock(return_value=4000000001)
        self.adapter.wait_normal = Mock()
        self.adapter.remaining_timeout = Mock(return_value=30000)
        self.index = 0
        self.boundary = None
        self.older = Mock()
        self.older.count.return_value = 1
        self.older.get_attribute.side_effect = lambda name: 'disabled' if self.index == self.boundary else ''
        self.older.click.side_effect = self.advance
        self.newer = Mock()
        self.newer.count.return_value = 1
        self.newer.get_attribute.side_effect = lambda name: 'disabled' if self.index == 0 else ''
        self.rows = Mock()
        self.rows.count.return_value = 20
        self.links = Mock()
        self.links.evaluate_all.side_effect = lambda code: [f'https://tapas.io/series/sample-{self.index}']
        self.page.locator.side_effect = lambda selector: {
            '.tp-table.transactions .contents .tr':self.rows,
            '.tp-table.transactions .contents .tr a[href]':self.links,
            '.tp-table.transactions .js-prev-page':self.older,
            '.tp-table.transactions .js-next-page':self.newer,
        }[selector]
        self.adapter.inspect_account_navigation = Mock(side_effect=self.open_history)

    def publish(self):
        self.adapter._history_scan['observation'].history_status = 200
        self.adapter._history_scan['observation'].library_values['/profile/transactions'] = {'data':{
            'transactions':[{'id':9000000000+self.index*20+i,'source_type':'IN_APP_PURCHASE',
                             'amount':'PRIVATE_AMOUNT','wallet_id':'PRIVATE_WALLET'} for i in range(20)],
            'pagination':{'index':self.index,'page':1,'total':0,'max_limit':20,
                          'has_next':True,'since':'PRIVATE_CURSOR_DATE'}}}

    def open_history(self):
        self.publish()
        return {'history_clicked':True}

    def advance(self,**options):
        self.index += 1
        self.publish()

    def test_continue_uses_private_tab_without_replaying_earlier_pages(self):
        first = self.adapter.inspect_transaction_history(3)
        self.assertEqual(len(first['pages']),3)
        self.assertEqual(self.older.click.call_count,2)
        self.assertIs(self.adapter.page,self.main)
        second = self.adapter.inspect_transaction_history(3,resume=True)
        self.assertEqual([p['server_pagination']['index'] for p in second['pages']],list(range(6)))
        self.assertEqual(second['new_page_count'],3)
        self.assertEqual(self.older.click.call_count,5)
        self.adapter.inspect_account_navigation.assert_called_once()
        self.assertIs(self.adapter.page,self.main)
        self.assertTrue(second['resume_available'])
        self.assertFalse(second['complete'])
        for secret in ('PRIVATE','4000000001','9000000000','wallet_id','amount'):
            self.assertNotIn(secret,json.dumps(second))

    def test_changed_account_stops_before_navigation(self):
        self.adapter.inspect_transaction_history(1)
        self.adapter.account_identity.return_value = 4000000002
        with self.assertRaises(AuthenticationError):
            self.adapter.inspect_transaction_history(2,resume=True)
        self.older.click.assert_not_called()
        self.assertIs(self.adapter.page,self.main)

    def test_resume_requires_live_cursor_and_a_boolean(self):
        for options in ({'resume':True},{'resume':'yes'}):
            with self.assertRaises(ArchiveError):
                self.adapter.inspect_transaction_history(1,**options)
        self.adapter.inspect_account_navigation.assert_not_called()

    def test_boundary_is_retained_without_additional_clicks(self):
        self.boundary = 2
        first = self.adapter.inspect_transaction_history(5)
        second = self.adapter.inspect_transaction_history(5,resume=True)
        self.assertEqual(first['new_page_count'],3)
        self.assertEqual(second['new_page_count'],0)
        self.assertTrue(second['observed_boundary'])
        self.assertFalse(second['resume_available'])
        self.assertFalse(second['complete'])
        self.assertEqual(self.older.click.call_count,2)

    def test_interrupt_after_click_resumes_without_a_second_click(self):
        self.adapter.inspect_transaction_history(1)
        self.adapter.wait_normal.side_effect = RunLimit('synthetic time limit')
        with self.assertRaises(RunLimit):
            self.adapter.inspect_transaction_history(1,resume=True)
        self.assertIs(self.adapter.page,self.main)
        self.assertEqual(self.older.click.call_count,1)
        self.adapter.wait_normal.side_effect = None
        resumed = self.adapter.inspect_transaction_history(1,resume=True)
        self.assertEqual(resumed['new_page_count'],1)
        self.assertEqual(resumed['pages'][-1]['server_pagination']['index'],1)
        self.assertEqual(self.older.click.call_count,1)

    def test_checkpoint_interruption_retains_consumed_page(self):
        checkpoint = Mock(side_effect=RunLimit('synthetic limit'))
        with self.assertRaises(RunLimit):
            self.adapter.inspect_transaction_history(1,on_page=checkpoint)
        resumed = self.adapter.inspect_transaction_history(1,resume=True)
        self.assertEqual([p['server_pagination']['index'] for p in resumed['pages']],[0,1])
        self.assertEqual(self.older.click.call_count,1)

    def test_interrupted_initial_navigation_cannot_resume_an_unopened_view(self):
        self.adapter.inspect_account_navigation.side_effect = RunLimit('synthetic limit')
        with self.assertRaises(RunLimit):
            self.adapter.inspect_transaction_history(1)
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(1,resume=True)
        self.older.click.assert_not_called()

    def test_refused_cursor_is_not_retried_or_reported_resumable(self):
        self.adapter.inspect_transaction_history(1)
        def refused(**options):
            self.adapter._history_scan['observation'].history_status = 400
        self.older.click.side_effect = refused
        result = self.adapter.inspect_transaction_history(3,resume=True)
        self.assertIn('HTTP 400',result['blocker'])
        self.assertFalse(result['resume_available'])
        self.assertEqual(result['new_page_count'],0)
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(3,resume=True)
        self.assertEqual(self.older.click.call_count,1)

    def test_expired_login_during_pagination_stops(self):
        self.adapter.inspect_transaction_history(1)
        def expired(**options):
            self.adapter._history_scan['observation'].history_status = 401
        self.older.click.side_effect = expired
        with self.assertRaises(AuthenticationError):
            self.adapter.inspect_transaction_history(3,resume=True)
        self.assertIs(self.adapter.page,self.main)

    def test_unavailable_control_preserves_discoveries_without_forcing_click(self):
        self.adapter.inspect_transaction_history(1)
        self.older.click.side_effect = TimeoutError('PRIVATE browser data')
        result = self.adapter.inspect_transaction_history(3,resume=True)
        self.assertEqual(len(result['pages']),1)
        self.assertEqual(result['content_links'],['https://tapas.io/series/sample-0'])
        self.assertFalse(result['resume_available'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(3,resume=True)

    def test_page_limit_must_be_a_whole_number_from_one_to_five_hundred(self):
        for limit in (0,501,True,'3',None):
            with self.assertRaises(ArchiveError):
                self.adapter.inspect_transaction_history(limit)
        self.adapter.inspect_account_navigation.assert_not_called()
        self.adapter.context.new_page.assert_not_called()

    def test_unopened_history_view_closes_the_private_tab_and_leaves_nothing_to_resume(self):
        self.adapter.inspect_account_navigation.side_effect = None
        self.adapter.inspect_account_navigation.return_value = {'history_clicked':False}
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(2)
        self.page.close.assert_called_once()
        self.assertIsNone(self.adapter._history_scan)
        self.assertIs(self.adapter.page,self.main)
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(2,resume=True)

    def test_new_scan_closes_the_previous_private_tab(self):
        self.adapter.inspect_transaction_history(1)
        earlier = self.page
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.page.locator.side_effect = earlier.locator.side_effect
        self.adapter.context.new_page.return_value = self.page
        self.adapter.inspect_transaction_history(1)
        earlier.close.assert_called_once()
        self.page.close.assert_not_called()
        self.assertEqual(self.adapter.inspect_account_navigation.call_count,2)

    def test_stalled_cursor_stops_without_a_second_click_and_cannot_be_resumed(self):
        self.adapter.inspect_transaction_history(1)
        self.older.click.side_effect = None
        clock = Mock(monotonic=Mock(side_effect=[0,1,100]))
        with patch('comic_archive.adapters.tapas.time',clock):
            result = self.adapter.inspect_transaction_history(3,resume=True)
        self.assertEqual(len(result['pages']),1)
        self.assertEqual(result['new_page_count'],0)
        self.assertFalse(result['resume_available'])
        self.assertEqual(self.older.click.call_count,1)
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(3,resume=True)

    def test_refusal_seen_only_in_earlier_observed_responses_still_stops_the_scan(self):
        self.adapter.inspect_transaction_history(1)
        def refused(**options):
            self.adapter._history_scan['observation'].responses.append({'method':'GET','path':'/profile/transactions','status':429,'kind':'xhr'})
        self.older.click.side_effect = refused
        result = self.adapter.inspect_transaction_history(3,resume=True)
        self.assertEqual(result['new_page_count'],0)
        self.assertFalse(result['resume_available'])
        self.assertEqual(self.older.click.call_count,1)
        with self.assertRaises(ArchiveError):
            self.adapter.inspect_transaction_history(3,resume=True)

    def test_repeated_transaction_page_is_not_counted_as_progress(self):
        def repeat(**options):
            self.advance()
            for position,item in enumerate(self.adapter._history_scan['observation'].library_values['/profile/transactions']['data']['transactions']):
                item['id'] = 9000000000+position
        self.older.click.side_effect = repeat
        checkpoint = Mock()
        result = self.adapter.inspect_transaction_history(3,on_page=checkpoint)
        self.assertEqual(len(result['pages']),1)
        checkpoint.assert_called_once()
        self.assertFalse(result['observed_boundary'])
        self.assertEqual(self.older.click.call_count,1)

    def test_time_limit_while_preparing_the_click_keeps_the_cursor_resumable(self):
        self.adapter.inspect_transaction_history(1)
        self.adapter.remaining_timeout.side_effect = RunLimit('synthetic time limit')
        with self.assertRaises(RunLimit):
            self.adapter.inspect_transaction_history(2,resume=True)
        self.older.click.assert_not_called()
        self.assertIs(self.adapter.page,self.main)
        self.adapter.remaining_timeout.side_effect = None
        resumed = self.adapter.inspect_transaction_history(1,resume=True)
        self.assertEqual(resumed['new_page_count'],1)
        self.assertEqual(self.older.click.call_count,1)
