import json
import unittest
from unittest.mock import MagicMock, Mock

from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.errors import AuthenticationError


class FreeEpisodeFilterTests(unittest.TestCase):
    def setUp(self):
        self.adapter=object.__new__(TapasAdapter);self.adapter.page=Mock()
        self.adapter.remaining_timeout=Mock(return_value=30000);self.adapter.wait_normal=Mock()
        self.automatic=Mock();self.adapter.automatic_adapter=Mock(return_value=self.automatic)
        self.start='https://tapas.io/reading-list?category=SUBSCRIBED'
        self.first='https://tapas.io/reading-list?category=SUBSCRIBED&type=FREE'
        self.second='https://tapas.io/reading-list?pageNumber=2&type=FREE&category=SUBSCRIBED'
        self.adapter.page.url=self.start
        self.control=Mock();self.control.count.return_value=1;self.control.is_visible.return_value=True
        self.control.inner_text.return_value='Free Episodes'
        self.control.get_attribute.return_value=self.first
        parent=Mock()
        parent.get_attribute.side_effect=lambda name: 'FREE' if name=='data-type' else 'filter-item on'
        self.control.locator.return_value=parent
        base=Mock();base.evaluate_all.return_value=[0];base.nth.return_value=self.control
        self.pager=Mock();self.pager.count.return_value=1;self.pager.is_visible.return_value=True
        self.pager.get_attribute.side_effect=lambda name: (self.second if name=='href' else
            'paging__button--next'+(' disabled' if self.adapter.page.url==self.second else '') if name=='class' else None)
        self.rows=Mock();self.rows.evaluate_all.side_effect=[
            [{'url':'https://tapas.io/series/one','title':'One','ids':['42']}],
            [{'url':'https://tapas.io/series/two','title':'Two','ids':['43']}]]
        def locator(selector):
            if 'section-filter' in selector:return base
            if 'paging__button--next' in selector:return self.pager
            return self.rows
        self.adapter.page.locator.side_effect=locator
        self.control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.first)
        self.pager.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.second)
        event=MagicMock();event.__enter__.return_value.value.status=200
        self.adapter.page.expect_navigation.return_value=event
        def restore(url,**kwargs):self.adapter.page.url=url;return Mock(status=200)
        self.adapter.page.goto.side_effect=restore
        self.before={'authenticated':True,'membership_verified':True,'scope':'subscribed'}

    def inspect(self):return self.adapter._inspect_free_episode_filter(self.before)

    def test_visible_provided_filter_and_adjacent_pager_verify_only_that_view_and_restore_all(self):
        result=self.inspect()
        self.assertTrue(result['membership_verified']);self.assertTrue(result['complete'])
        self.assertTrue(result['restore_succeeded']);self.assertEqual(result['public_type_code'],'FREE')
        self.assertEqual(result['verified_filter'],'free_episodes');self.assertFalse(result['account_inventory_complete'])
        self.assertFalse(result['account_membership_verified'])
        self.assertEqual([row['source_series_id'] for row in result['series']],['42','43'])
        self.control.click.assert_called_once();self.pager.click.assert_called_once()
        self.assertEqual(self.automatic.check_session.call_count,2)
        self.adapter.page.goto.assert_called_once_with(self.start,wait_until='domcontentloaded',timeout=30000)
        self.adapter.page.mouse.wheel.assert_not_called()
        self.adapter.page.remove_listener.assert_called_once()

    def test_padded_rendered_label_is_normalized_before_normal_click(self):
        self.control.inner_text.return_value=' \n Free \t Episodes \n'
        result=self.inspect()
        self.assertTrue(result['complete']);self.control.click.assert_called_once()

    def category_filter(self,code='FREE'):
        self.first=f'https://tapas.io/reading-list?category={code}&type=ALL'
        self.second=f'https://tapas.io/reading-list?pageNumber=2&category={code}&type=ALL'
        self.control.get_attribute.return_value=self.first
        self.control.locator.return_value.get_attribute.side_effect=lambda name: code if name=='data-type' else 'filter-item on'

    def test_visible_filter_may_bind_the_provided_category_instead_of_type(self):
        self.category_filter()
        result=self.inspect()
        self.assertTrue(result['complete']);self.assertTrue(result['membership_verified'])
        self.assertEqual(result['filter_query_field'],'category');self.assertEqual(result['scope'],'free')
        self.assertEqual([row['source_series_id'] for row in result['series']],['42','43'])
        self.assertTrue(result['restore_succeeded']);self.assertFalse(result['account_inventory_complete'])

    def test_unknown_category_code_is_compared_privately_and_never_exported(self):
        self.category_filter('PRIVATE_CODE')
        result=self.inspect()
        self.assertTrue(result['complete']);self.assertTrue(result['membership_verified'])
        self.assertIsNone(result['scope']);self.assertIsNone(result['supplied_link_scope'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertTrue(result['restore_succeeded'])

    def test_category_filter_does_not_accept_changed_content_type(self):
        self.category_filter()
        self.control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.first.replace('ALL','COMICS'))
        result=self.inspect()
        self.assertFalse(result['membership_verified']);self.pager.click.assert_not_called()
        self.assertTrue(result['restore_succeeded'])

    def test_ambiguous_binding_missing_code_or_noninitial_page_never_clicks(self):
        for route in ('https://tapas.io/reading-list?category=FREE&type=FREE',
                      'https://tapas.io/reading-list?category=SUBSCRIBED&type=ALL',
                      'https://tapas.io/reading-list?category=FREE&type=ALL&pageNumber=2'):
            self.control.get_attribute.return_value=route
            self.assertFalse(self.inspect()['filter_clicked'])
        self.control.click.assert_not_called()

    def test_same_position_different_label_after_navigation_is_not_verified(self):
        self.control.inner_text.side_effect=['Free Episodes','Wait Until Free']
        result=self.inspect()
        self.assertFalse(result['membership_verified']);self.pager.click.assert_not_called()
        self.assertTrue(result['restore_succeeded'])

    def longer_category_filter(self,code='FREE_EPISODES'):
        from urllib.parse import parse_qs,urlsplit
        self.category_filter(code)
        def page_number():return int(parse_qs(urlsplit(self.adapter.page.url).query).get('pageNumber',['1'])[0])
        self.rows.evaluate_all.side_effect=[
            [{'url':f'https://tapas.io/series/title-{index}','title':'Title','ids':[str(41+index)]}] for index in range(1,4)]
        self.pager.get_attribute.side_effect=lambda name: (
            f'https://tapas.io/reading-list?category={code}&type=ALL&pageNumber={page_number()+1}' if name=='href' else
            'paging__button--next'+(' disabled' if page_number()==3 else '') if name=='class' else None)
        self.pager.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.pager.get_attribute('href'))

    def test_live_proven_category_shape_can_follow_more_pages_to_a_normal_boundary(self):
        self.longer_category_filter()
        result=self.adapter._inspect_free_episode_filter(self.before,max_pages=50)
        self.assertTrue(result['complete']);self.assertEqual(result['page_limit'],50)
        self.assertEqual([row['page'] for row in result['pages']],[1,2,3])
        self.assertEqual([row['source_series_id'] for row in result['series']],['42','43','44'])
        self.assertEqual(self.automatic.check_session.call_count,3)
        self.assertFalse(result['account_inventory_complete']);self.assertTrue(result['restore_succeeded'])

    def test_optional_discovery_uses_only_live_proven_scope_and_emits_non_account_checkpoints(self):
        self.longer_category_filter();checkpoint=Mock()
        result=self.adapter._inspect_free_episode_filter(self.before,max_pages=50,on_page=checkpoint,known_scope_only=True)
        self.assertTrue(result['complete']);self.assertEqual(checkpoint.call_count,3)
        for call in checkpoint.call_args_list:
            value=call.args[0]
            self.assertEqual(value['scope'],'free_episodes');self.assertTrue(value['membership_verified'])
            self.assertFalse(value['account_membership_verified']);self.assertFalse(value['complete'])
            self.assertFalse(value['account_inventory_complete'])
        self.assertEqual(checkpoint.call_args_list[0].args[0]['series'][0]['source_series_id'],'42')

    def test_optional_discovery_rejects_alternative_filter_before_click(self):
        checkpoint=Mock()
        result=self.adapter._inspect_free_episode_filter(self.before,on_page=checkpoint,known_scope_only=True)
        self.assertFalse(result['membership_verified']);self.control.click.assert_not_called();checkpoint.assert_not_called()

    def test_optional_discovery_preserves_checkpoint_when_budget_expires_and_restores(self):
        from comic_archive.errors import RunLimit
        self.longer_category_filter();retained=[]
        def checkpoint(value):retained.extend(value['series']);raise RunLimit('metadata budget')
        with self.assertRaises(RunLimit):
            self.adapter._inspect_free_episode_filter(self.before,max_pages=50,on_page=checkpoint,known_scope_only=True)
        self.assertEqual([v['source_series_id'] for v in retained],['42']);self.pager.click.assert_not_called()
        self.assertEqual(self.adapter.page.url,self.start)

    def test_library_optional_provider_starts_at_subscribed_all_instead_of_constructing_free_url(self):
        self.adapter._inspect_library_details=Mock(return_value=self.before)
        self.adapter._inspect_free_episode_filter=Mock(return_value={'scope':'free_episodes'})
        checkpoint=Mock();result=self.adapter.inspect_library(category='free_episodes',on_page=checkpoint)
        self.assertEqual(result['scope'],'free_episodes')
        self.adapter._inspect_library_details.assert_called_once_with('subscribed',research=False)
        self.adapter._inspect_free_episode_filter.assert_called_once_with(self.before,max_pages=50,on_page=checkpoint,known_scope_only=True)

    def test_unproven_source_shape_keeps_two_page_limit_and_invalid_limits_never_click(self):
        self.longer_category_filter('PRIVATE_CODE')
        result=self.adapter._inspect_free_episode_filter(self.before,max_pages=50)
        self.assertFalse(result['complete']);self.assertEqual(result['page_limit'],2)
        self.assertEqual(len(result['pages']),2);self.assertNotIn('PRIVATE',json.dumps(result))
        self.control.click.reset_mock()
        from comic_archive.models import ArchiveError
        for value in (0,51,True,2.5):
            with self.assertRaises(ArchiveError):self.adapter._inspect_free_episode_filter(self.before,max_pages=value)
        self.control.click.assert_not_called()

    def test_explicit_smaller_cap_retains_partial_candidates_without_following_next(self):
        self.longer_category_filter()
        result=self.adapter._inspect_free_episode_filter(self.before,max_pages=1)
        self.assertFalse(result['complete']);self.assertEqual(result['page_limit'],1)
        self.assertEqual(len(result['pages']),1);self.pager.click.assert_not_called()
        self.assertTrue(result['restore_succeeded'])

    def test_cross_page_overlap_retains_ids_and_boundary_but_does_not_claim_complete_membership(self):
        self.rows.evaluate_all.side_effect=[
            [{'url':'https://tapas.io/series/one','title':'One','ids':['42']},
             {'url':'https://tapas.io/series/two','title':'Two','ids':['43']}],
            [{'url':'https://tapas.io/series/renamed-one/info','title':'Renamed one','ids':['42']},
             {'url':'https://tapas.io/series/three','title':'Three','ids':['44']}]]
        result=self.inspect()
        self.assertTrue(result['pagination_complete']);self.assertTrue(result['boundary_observed'])
        self.assertFalse(result['complete']);self.assertFalse(result['account_membership_verified'])
        self.assertEqual(result['duplicate_series_on_later_pages'],1)
        self.assertEqual([row['source_series_id'] for row in result['series']],['42','43','44'])
        self.assertTrue(result['restore_succeeded']);self.assertFalse(result['account_inventory_complete'])

    def test_filter_semantics_keeps_fixed_label_counts_and_types_without_private_values(self):
        self.adapter.page.locator.side_effect=None
        self.adapter.page.locator.return_value.evaluate.return_value={
            'node_count':37,'truncated':False,'label_node_counts':{'ticket_count_label':2,'claim':1,'PRIVATE_NAME':1,'gift':True},
            'attribute_value_types':{'data-free-tickets':'integer','data-ticket-id':'string','data-gift-expires':'string',
                'data-user-ticket-id':'integer','data-ticket-token':'string','data-ticket-secret':'string',
                'data-ticket-cookie':'PRIVATE','data-ticket-info':['PRIVATE']},'raw_text':'PRIVATE_USER_BALANCE'}
        result=self.adapter._reading_filter_semantics()
        self.assertTrue(result['available']);self.assertEqual(result['label_node_counts'],{'ticket_count_label':2,'claim':1})
        self.assertEqual(result['attribute_value_types'],{'data-free-tickets':'integer','data-ticket-id':'string','data-gift-expires':'string'})
        self.assertFalse(result['controls_activated']);self.assertFalse(result['authorization_evidence'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.adapter.page.goto.assert_not_called()
        self.adapter.page.locator.return_value.click.assert_not_called()

    def test_filter_semantics_unavailable_dom_is_optional_and_does_not_leak_failure(self):
        self.adapter.page.locator.side_effect=None
        self.adapter.page.locator.return_value.evaluate.side_effect=RuntimeError('PRIVATE_COOKIE_URL')
        result=self.adapter._reading_filter_semantics()
        self.assertFalse(result['available']);self.assertNotIn('PRIVATE',json.dumps(result))
        self.adapter.page.goto.assert_not_called();self.adapter.page.locator.return_value.click.assert_not_called()

    def test_filter_semantics_respects_the_existing_time_budget(self):
        from comic_archive.errors import RunLimit
        self.adapter.remaining_timeout.side_effect=RunLimit('Time budget exhausted')
        with self.assertRaises(RunLimit):self.adapter._reading_filter_semantics()
        self.adapter.page.goto.assert_not_called()

    def test_hidden_ambiguous_or_unverified_initial_scope_never_clicks(self):
        self.control.is_visible.return_value=False
        self.assertFalse(self.inspect()['filter_clicked'])
        self.control.is_visible.return_value=True;self.control.count.return_value=2
        self.assertFalse(self.inspect()['filter_clicked'])
        self.control.count.return_value=1;self.before['authenticated']=False
        self.assertFalse(self.inspect()['filter_clicked'])
        self.control.click.assert_not_called();self.adapter.page.goto.assert_not_called()
        self.automatic.check_session.assert_not_called()

    def test_different_initial_category_is_not_treated_as_verified_scope(self):
        self.adapter.page.url='https://tapas.io/reading-list?category=RECENT'
        self.assertFalse(self.inspect()['filter_clicked'])
        self.control.click.assert_not_called()

    def test_unrecognized_source_type_code_stays_in_memory_and_out_of_results(self):
        private=self.first.replace('FREE','PRIVATE_CODE')
        self.control.get_attribute.return_value=private
        self.control.locator.return_value.get_attribute.side_effect=lambda name: 'PRIVATE_CODE' if name=='data-type' else 'filter-item on'
        self.control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',private)
        result=self.inspect()
        self.assertTrue(result['membership_verified']);self.assertIsNone(result['public_type_code'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.pager.click.assert_not_called()

    def test_payment_route_or_unexpected_type_does_not_become_a_filter(self):
        self.control.get_attribute.return_value='https://tapas.io/ink/purchase'
        self.assertFalse(self.inspect()['filter_clicked']);self.control.click.assert_not_called()
        self.control.get_attribute.return_value=self.first
        self.control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.first.replace('FREE','WUF'))
        result=self.inspect()
        self.assertFalse(result['membership_verified']);self.assertFalse(result['complete'])
        self.pager.click.assert_not_called();self.assertTrue(result['restore_succeeded'])

    def test_unfiltered_next_is_not_clicked_and_returned_candidates_are_retained(self):
        self.pager.get_attribute.side_effect=lambda name: self.second.replace('FREE','ALL') if name=='href' else ''
        result=self.inspect()
        self.assertFalse(result['complete']);self.assertEqual(len(result['pages']),1)
        self.assertEqual([row['source_series_id'] for row in result['series']],['42'])
        self.pager.click.assert_not_called();self.assertTrue(result['restore_succeeded'])

    def test_filter_redirect_to_another_category_is_unverified_and_restored(self):
        self.control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',self.first.replace('SUBSCRIBED','RECENT'))
        result=self.inspect()
        self.assertFalse(result['membership_verified']);self.assertFalse(result['complete'])
        self.pager.click.assert_not_called();self.assertTrue(result['restore_succeeded'])

    def test_login_failure_after_normal_filter_click_stops_and_restores_all(self):
        self.automatic.check_session.side_effect=AuthenticationError('Login required')
        with self.assertRaises(AuthenticationError):self.inspect()
        self.control.click.assert_called_once();self.pager.click.assert_not_called()
        self.assertEqual(self.adapter.page.url,self.start)

    def test_navigation_failure_keeps_verified_first_page_and_does_not_retry_or_leak_exception(self):
        self.pager.click.side_effect=RuntimeError('PRIVATE_COOKIE_URL')
        result=self.inspect()
        self.assertFalse(result['complete']);self.assertEqual(result['observed_series_count'],1)
        self.assertEqual([row['source_series_id'] for row in result['series']],['42'])
        self.pager.click.assert_called_once();self.assertTrue(result['restore_succeeded'])
        self.assertNotIn('PRIVATE',json.dumps(result))
