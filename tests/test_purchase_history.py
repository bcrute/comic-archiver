"""Durable discovery uses synthetic account records, never account traffic.

Indexed response contract observed on the normal pager and its public handler:
https://us-a.tapas.io/resources/js/tapas.min.47e0a69d33.js (9 November 2026).
Fixtures reproduce data.body and pagination.index/has_next without account data.
These tests exercise that contract; they do not prove all purchases are listed.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from comic_archive import download_control, purchase_history
from comic_archive.errors import AuthenticationError, MetadataError, RunLimit
from comic_archive.inventory import Inventory
from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.models import ArchiveError


class PurchaseHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'
        self.first = 'https://tapas.io/series/first'
        self.older = 'https://tapas.io/series/older'
        self.cursor = {'contract': 'tapas-transactions-index-v1', 'index': 1}
        self.identities = {self.first: {'source_series_id': '1', 'title': 'First'},
                           self.older: {'source_series_id': '2', 'title': 'Older'}}
        self.adapter = SimpleNamespace(purchase_history_page=Mock(),
                                       purchase_series_identity=Mock(side_effect=lambda url: self.identities.get(url)))

    def page(self, links, records, *, cursor=None, boundary=False, index=0):
        return {'authenticated': True, 'scope_verified': True, 'links': links, 'record_ids': records,
                'next_cursor': cursor, 'boundary_verified': boundary, 'page_identity': f'index:{index}'}

    def store(self):
        inventory = Inventory(self.root)
        inventory.bind_account('synthetic-account')
        return inventory

    def test_old_series_absent_from_lists_is_retained_across_workers(self):
        self.adapter.purchase_history_page.return_value = self.page([self.first], ['a'], cursor=self.cursor)
        with self.store() as inventory:
            first = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, pages=1)
            self.assertEqual(first['status'], 'pending')
            self.assertEqual(first['series_added'], 1)
        self.adapter.purchase_history_page.return_value = self.page([self.older], ['b'], boundary=True, index=1)
        with self.store() as inventory:
            second = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, pages=1)
            self.adapter.purchase_history_page.assert_called_with(self.cursor)
            self.assertEqual(second['status'], 'checked')
            self.assertEqual([row[0] for row in inventory.db.execute('SELECT id FROM series ORDER BY id')], ['1', '2'])
            self.assertIn('transaction_history', inventory.db.execute('SELECT provenance FROM series WHERE id=?', ('2',)).fetchone()[0])
            self.assertTrue(download_control.progressed({'refresh': {'purchase_history': second}}))

    def test_crash_after_page_commit_resumes_inbox_without_refetch(self):
        self.adapter.purchase_history_page.return_value = self.page([self.older], ['a'], boundary=True)
        self.adapter.purchase_series_identity.side_effect = RunLimit('synthetic interruption')
        with self.store() as inventory:
            with self.assertRaises(RunLimit):
                purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertTrue(purchase_history.pending(inventory))
        self.adapter.purchase_history_page.reset_mock()
        self.adapter.purchase_series_identity.side_effect = lambda url: self.identities[url]
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertEqual(result['status'], 'checked')
            self.adapter.purchase_history_page.assert_not_called()
            self.assertEqual(result['links_processed'], 1)

    def test_same_series_on_distinct_pages_does_not_end_discovery(self):
        self.adapter.purchase_history_page.side_effect = [self.page([self.first], ['a'], cursor=self.cursor),
                                                         self.page([self.first], ['b'], boundary=True, index=1)]
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, pages=2)
            self.assertEqual((result['status'], result['pages']), ('checked', 2))
            self.assertEqual(self.adapter.purchase_series_identity.call_count, 1)

    def test_repeated_page_is_partial_and_preserves_candidates(self):
        self.adapter.purchase_history_page.return_value = self.page([self.first], ['a'], cursor=self.cursor)
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, pages=2)
            self.assertEqual((result['status'], result['code']), ('partial', 'history_page_repeated'))
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM series').fetchone()[0], 1)

    def test_missing_cursor_is_partial_and_does_not_retry_in_the_same_round(self):
        self.adapter.purchase_history_page.return_value = self.page([self.first], ['a'])
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertEqual(result['code'], 'history_continuation_unverified')
            purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.adapter.purchase_history_page.assert_called_once()

    def test_inbox_is_drained_before_a_terminal_page_becomes_checked(self):
        self.adapter.purchase_history_page.return_value = self.page([self.first, self.older], ['a', 'b'], boundary=True)
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, links=1)
            self.assertEqual(result['status'], 'pending')
            self.assertTrue(purchase_history.pending(inventory))
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, links=1)
            self.assertEqual(result['status'], 'checked')
            self.adapter.purchase_history_page.assert_called_once()

    def test_unmatched_episode_keeps_coverage_partial(self):
        self.adapter.purchase_history_page.return_value = self.page(['https://tapas.io/episode/99'], ['a'], boundary=True)
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertEqual((result['status'], result['unresolved']), ('partial', 1))
            self.assertEqual(result['code'], 'history_association_unresolved')

    def test_unavailable_reader_does_not_loop_or_claim_history_was_checked(self):
        with self.store() as inventory:
            result = purchase_history.reconcile(SimpleNamespace(), inventory, 'round', lambda: None)
            self.assertEqual(result['code'], 'history_reader_unavailable')
            self.assertFalse(purchase_history.pending(inventory))

    def test_mismatched_account_checkpoint_is_refused(self):
        self.adapter.purchase_history_page.return_value = self.page([], [], boundary=False)
        with self.store() as inventory:
            purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            checkpoint = purchase_history.state(inventory)
            checkpoint['binding'] = '0' * 64
            purchase_history.save(inventory, checkpoint)
            with self.assertRaises(AuthenticationError):
                purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)

    def test_durable_cursor_cannot_change_account_or_issue_an_action_request(self):
        invalid = ({'url': 'https://tapas.io/account/delete', 'scope': 'series'},
                   self.cursor | {'index': True}, self.cursor | {'index': -1}, self.cursor | {'index': 10000},
                   self.cursor | {'contract': 'other'}, self.cursor | {'action': 'buy'})
        for cursor in invalid:
            with self.subTest(cursor=cursor), self.assertRaises(MetadataError):
                purchase_history.continuation_url(cursor)
        self.assertEqual(purchase_history.continuation_url(copy.deepcopy(self.cursor)),
                         'https://tapas.io/profile/transactions?to_prev=true&index=1')

    def test_identical_rows_on_distinct_verified_indices_do_not_end_discovery(self):
        self.adapter.purchase_history_page.side_effect = [self.page([self.first], ['same'], cursor=self.cursor),
                                                         self.page([self.first], ['same'], boundary=True, index=1)]
        with self.store() as inventory:
            result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None, pages=2)
            self.assertEqual((result['status'], result['pages']), ('checked', 2))
            self.assertEqual(self.adapter.purchase_series_identity.call_count, 1)

    def test_rate_limit_keeps_the_committed_inbox_for_the_controller_cooldown(self):
        error = ArchiveError('Synthetic source refusal')
        error.source_http_status = 429
        error.retry_after_seconds = 300
        self.adapter.purchase_history_page.return_value = self.page([self.older], ['a'], boundary=True)
        self.adapter.purchase_series_identity.side_effect = error
        with self.store() as inventory:
            with self.assertRaises(ArchiveError) as refused:
                purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertIs(refused.exception, error)
            self.assertTrue(purchase_history.pending(inventory))

    def test_a_purchase_link_that_is_gone_is_set_aside_and_any_other_refusal_still_stops(self):
        for status in (404, 410):
            error = ArchiveError('Synthetic source refusal')
            error.source_http_status = status
            self.root = self.root.parent / f'Library {status}'
            self.adapter.purchase_history_page.return_value = self.page([self.older], ['a'], boundary=True)
            self.adapter.purchase_series_identity.side_effect = error
            with self.subTest(status=status), self.store() as inventory:
                purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
                self.assertEqual([row[0] for row in inventory.db.execute('SELECT resolved FROM purchase_history_links')], [-1])
        error = ArchiveError('Synthetic source refusal')
        error.source_http_status = 400
        self.root = self.root.parent / 'Library 400'
        self.adapter.purchase_series_identity.side_effect = error
        with self.store() as inventory, self.assertRaises(ArchiveError):
            purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)

    def test_changed_response_markup_is_partial_without_blocking_normal_discovery(self):
        self.adapter.purchase_history_page.side_effect = MetadataError('PRIVATE response shape')
        with self.store() as inventory:
            with self.assertLogs('comic_archive.download', level='INFO') as captured:
                result = purchase_history.reconcile(self.adapter, inventory, 'round', lambda: None)
            self.assertEqual((result['status'], result['code']), ('partial', 'history_rows_unverified'))
            self.assertNotIn('PRIVATE', repr(result))
            self.assertNotIn('PRIVATE', '\n'.join(captured.output))
            self.assertIn('reason=metadata_unverified', '\n'.join(captured.output))

    def test_skipped_or_invalid_continuation_retains_candidates_without_following(self):
        for cursor in (self.cursor | {'index': 2}, self.cursor | {'index': 10000}, {'action': 'buy'}):
            with self.subTest(cursor=cursor), self.store() as inventory:
                self.adapter.purchase_history_page.return_value = self.page([self.first], ['a'], cursor=cursor)
                result = purchase_history.reconcile(self.adapter, inventory, str(cursor), lambda: None)
                self.assertEqual(result['code'], 'history_continuation_unverified')
                self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM series').fetchone()[0], 1)


class PurchasePageTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TapasAdapter(Path('unused-session'))
        self.http = Mock()
        self.adapter.automatic_adapter = Mock(return_value=self.http)
        self.adapter.expected_account_id = 'synthetic-account'
        self.body = '<div class="tr"><div>PRIVATE unlock episode</div><a href="/series/older?tracking=1">Title</a><a href="/episode/99">Episode</a></div>'
        self.answer = {'code': 200, 'data': {'body': self.body, 'pagination': {'index': 0, 'has_next': True}}}
        self.http.transport.get.side_effect = lambda *args, **kwargs: json.dumps(self.answer).encode()

    def test_indexed_api_uses_normal_read_transport_and_retains_no_financial_details(self):
        page = self.adapter.purchase_history_page()
        self.http.check_session.assert_not_called()
        self.http.transport.get.assert_called_with('https://tapas.io/profile/transactions?to_prev=false&index=0', max_bytes=4194304)
        self.assertEqual(page['links'], ['https://tapas.io/series/older'])
        self.assertNotIn('PRIVATE', repr(page))
        self.assertFalse(page['boundary_verified'])
        self.assertEqual(page['next_cursor'], {'contract': 'tapas-transactions-index-v1', 'index': 1})
        self.answer['data']['pagination'] = {'index': 1, 'has_next': False}
        second = self.adapter.purchase_history_page(page['next_cursor'])
        self.http.check_session.assert_not_called()
        self.assertTrue(second['boundary_verified'])
        self.assertIsNone(second['next_cursor'])

    def test_only_explicit_empty_body_and_oldest_boundary_is_verified_empty(self):
        for body, more, empty in (('', False, True), (' ', False, True), ('', True, False),
                                  ('<div>New markup</div>', False, False)):
            with self.subTest(body=body, more=more):
                self.answer['data'].update(body=body, pagination={'index': 0, 'has_next': more})
                page = self.adapter.purchase_history_page()
                self.assertEqual(page['empty_verified'], empty)
                self.assertEqual(page['record_ids'], [])

    def test_ink_purchases_are_not_unresolved_content_and_episode_only_rows_are_candidates(self):
        self.answer['data']['body'] = ('<div class="tr"><span>Purchased Ink</span></div>'
                                     '<div class="tr"><a href="/episode/99#reader">Unlocked episode</a></div>'
                                     '<div class="tr">Unlocked episode unavailable</div>')
        page = self.adapter.purchase_history_page()
        self.assertEqual(page['links'], ['https://tapas.io/episode/99'])
        self.assertEqual(page['unresolved_rows'], 1)
        self.assertEqual(len(page['record_ids']), 3)

    def test_wrong_index_unknown_shape_and_malformed_rows_are_refused(self):
        cases = [None, {}, {'index': 1, 'has_next': False}, {'index': True, 'has_next': False},
                 {'index': 0, 'has_next': 0}]
        for pagination in cases:
            with self.subTest(pagination=pagination), self.assertRaises(MetadataError) as caught:
                self.answer['data']['pagination'] = pagination
                self.adapter.purchase_history_page()
            self.assertEqual(caught.exception.history_reason, 'pagination_shape')
        self.answer['data']['pagination'] = {'index': 0, 'has_next': False}
        for body in ('<div class="tr">incomplete', '<div class="tr"><span>mismatch</div>',
                     '<div class="tr"><div class="tr">nested</div></div>', '<div class="tr">'+('x'*16385)+'</div>'):
            with self.subTest(body=body[:60]), self.assertRaises(MetadataError):
                self.answer['data']['body'] = body
                self.adapter.purchase_history_page()

    def test_foreign_and_action_links_are_never_content_associations(self):
        self.answer['data']['body'] = ('<div class="tr">Unlocked episode'
                                     '<a href="https://other.test/series/bad">Bad</a>'
                                     '<a href="/account/buy">Buy</a></div>')
        page = self.adapter.purchase_history_page()
        self.assertEqual(page['links'], [])
        self.assertEqual(page['unresolved_rows'], 1)

    def test_api_authentication_and_throttle_refusals_keep_existing_controller_semantics(self):
        self.answer['code'] = 401
        with self.assertRaises(AuthenticationError):
            self.adapter.purchase_history_page()
        self.answer['code'] = 429
        with self.assertRaises(ArchiveError) as caught:
            self.adapter.purchase_history_page()
        self.assertEqual(caught.exception.source_http_status, 429)

    def test_unbound_direct_reader_establishes_identity_once(self):
        self.adapter.expected_account_id = None
        self.adapter.account_identity = Mock(return_value='synthetic-account')
        self.adapter.purchase_history_page()
        self.adapter.purchase_history_page()
        self.adapter.account_identity.assert_called_once()

    def test_unreadable_or_oversized_responses_are_metadata_failures(self):
        for raw in (b'not json', b'null', b'{"data": {}}', b'\xff', b'x' * (4194304 + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(MetadataError):
                self.http.transport.get.side_effect = None
                self.http.transport.get.return_value = raw
                self.adapter.purchase_history_page()
