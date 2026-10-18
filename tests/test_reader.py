import json
import unittest
from unittest.mock import Mock
from comic_archive.adapters.reader import (
    ReaderHTML, ReaderObservation, json_shape, ordered_sources, safe_primitives,
    script_routes, source_summary, public_script_contexts, navigation_fragment_shape, public_access_filters,
)
from comic_archive.errors import MetadataError
from comic_archive.models import ArchiveError


class ReaderTests(unittest.TestCase):
    def test_supplied_episode_navigation_preserves_order_without_link_text_or_private_fields(self):
        parser=ReaderHTML('81')
        parser.feed('<nav><a href="/episode/82" rel="next noreferrer" data-cookie="PRIVATE">PRIVATE ACCOUNT TEXT</a>'
                    '<a href="https://tapas.io/episode/80" rel="prev">Previous</a><a href="/episode/82">Duplicate</a></nav>'
                    '<div id="episode-81"><a href="/episode/81" rel>Current</a>'
                    '<img class="content__img" data-src="https://us-a.tapas.io/pc/page.jpg?signature=PRIVATE"></div>')
        self.assertEqual([v['source_episode_id'] for v in parser.episode_links.values()],['82','80','81'])
        self.assertEqual(list(parser.episode_links.values())[0]['relations'],['next'])
        self.assertTrue(list(parser.episode_links.values())[-1]['inside_requested_episode'])
        self.assertNotIn('PRIVATE',json.dumps(list(parser.episode_links.values())))
        self.assertEqual(ordered_sources(parser.images),['https://us-a.tapas.io/pc/page.jpg?signature=PRIVATE'])

    def test_episode_navigation_rejects_private_query_credentials_other_origins_and_actions(self):
        parser=ReaderHTML('81')
        for href in ('https://user:PRIVATE@tapas.io/episode/82','https://other.test/episode/82',
                     'http://tapas.io/episode/82','https://tapas.io:444/episode/82','/episode/82?token=PRIVATE',
                     '/episode/82#PRIVATE','javascript:PRIVATE','/ink/purchase','/episode/0','/episode/82/unlock'):
            parser.feed(f'<a href="{href}">PRIVATE</a>')
        self.assertEqual(parser.episode_links,{})

    def test_episode_navigation_bounds_distinct_candidates_without_claiming_page_completeness(self):
        parser=ReaderHTML('81')
        for index in range(1,205):parser.feed(f'<a href="/episode/{index}">Episode</a>')
        self.assertEqual(len(parser.episode_links),200);self.assertTrue(parser.episode_links_truncated)
        self.assertEqual(list(parser.episode_links.values())[-1]['source_episode_id'],'200')

    def test_public_catalog_filters_keep_only_fixed_labels_and_codes(self):
        value={'data':{'bmList':[{'key':'P','value':'Premium'},
            {'key':'FREE','value':' Free Access '},{'key':'PRIVATE_COOKIE','value':'Free'},
            {'key':'ALL','value':'ACCOUNT_PRIVATE'}],'session':'PRIVATE_SESSION'}}
        filters=public_access_filters('story-api.tapas.io','/cosmos/api/v1/landing/daily',value)
        self.assertEqual(filters['recognized_options'],[
            {'label':'premium','code':'P','code_recognized':True},
            {'label':'free access','code':'FREE','code_recognized':True},
            {'label':'free','code':None,'code_recognized':False}])
        self.assertEqual(filters['option_count'],4)
        self.assertFalse(filters['authorization_evidence'])
        self.assertNotIn('PRIVATE',json.dumps(filters))
        self.assertIsNone(public_access_filters('tapas.io','/profile/transactions',value))
        self.assertIsNone(public_access_filters('other.tapas.io','/cosmos/api/v1/landing/daily',value))

    def test_public_filter_diagnostics_are_opt_in_get_only_and_bounded(self):
        response=Mock(url='https://story-api.tapas.io/cosmos/api/v1/landing/daily?token=PRIVATE',status=200)
        response.request.method='GET';response.request.resource_type='xhr'
        response.headers={'content-type':'application/json'}
        response.json.return_value={'data':{'bmList':[{'key':'FREE','value':'Free'}]*25}}
        default=ReaderObservation();default.on_response(response)
        self.assertNotIn('public_access_filters',default.summary()['get_responses'][0])
        observation=ReaderObservation(catalog_filters=True);observation.on_response(response)
        filters=observation.summary()['get_responses'][0]['public_access_filters']
        self.assertEqual(filters['option_count'],25);self.assertEqual(filters['inspected_option_count'],20)
        self.assertEqual(len(filters['recognized_options']),20)
        response.request.method='POST';observation.on_response(response)
        self.assertNotIn('public_access_filters',observation.summary()['other_observed_responses'][0])
        self.assertNotIn('PRIVATE',json.dumps(observation.summary()))

    def test_app_settings_filter_schema_does_not_expose_unknown_options(self):
        filters=public_access_filters('api.tapas.io','/app-settings',{
            'browse_filters':[{'code':'WUF','label':'Wait Until Free'},
                              {'code':'ACCOUNT_PRIVATE','label':'ACCOUNT_PRIVATE'}]})
        self.assertEqual(filters['recognized_options'],[
            {'label':'wait until free','code':'WUF','code_recognized':True}])
        self.assertNotIn('PRIVATE',json.dumps(filters))
        self.assertIsNone(public_access_filters('api.tapas.io','/app-settings',[]))

    def test_legacy_c_assets_keep_scope_source_order_and_original_signatures(self):
        parser=ReaderHTML('81')
        parser.feed('<div id="episode-81"><img class="content__img" src="data:image/gif;base64,blank" '
                    'data-src="https://us-a.tapas.io/c/aa/page-one.jpg?signature=TEST1">'
                    '<img class="content__img" data-src="https://us-a.tapas.io/c/bb/page-two.jpg?signature=TEST2"></div>'
                    '<div id="episode-82"><img class="content__img" data-src="https://us-a.tapas.io/c/cc/other.jpg"></div>')
        self.assertEqual(ordered_sources(parser.images),[
            'https://us-a.tapas.io/c/aa/page-one.jpg?signature=TEST1',
            'https://us-a.tapas.io/c/bb/page-two.jpg?signature=TEST2'])
        self.assertEqual(source_summary(parser.images)['data-src']['comic_asset_count'],2)
        self.assertIsNone(ordered_sources([{'tag':'IMG','sources':{'data-src':'https://evil.test/c/aa/page.jpg'}}]))
        self.assertIsNone(ordered_sources([{'tag':'IMG','sources':{'data-src':'https://us-a.tapas.io/cover/page.jpg'}}]))

    def test_numeric_credentials_and_account_ids_are_not_content_attributes(self):
        parser=ReaderHTML('81')
        parser.feed('<div id="episode-81" data-episode-id="81" data-series-id="42" '
                    'data-token="987654321" data-session-id="876543210" data-user-id="765432109" '
                    'data-password="654321098" data-is-free="true"></div>')
        self.assertEqual(parser.attributes,{'data-episode-id':'81','data-series-id':'42'})

    def test_fragment_shape_keeps_view_enums_and_redacts_unknown_tokens(self):
        result=navigation_fragment_shape('SUBSCRIBED%2FCOMIC%3Ftoken%3DPRIVATE_987654321')
        self.assertIn('subscribed',result['components'])
        self.assertIn('comic',result['components'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('987654321',json.dumps(result))
        self.assertFalse(navigation_fragment_shape('X'*201)['supported_size'])
    def test_history_query_structure_omits_private_cursor_and_credential_values(self):
        observation=ReaderObservation()
        response=Mock(url='https://tapas.io/profile/transactions?since=999888777&since_date=PRIVATE_DATE&index=2&token=PRIVATE',status=200)
        response.request.method='GET'; response.request.resource_type='fetch'
        response.headers={'content-type':'application/json'}
        response.json.return_value={'data':{'pagination':{'index':2}}}
        observation.on_response(response)
        result=observation.summary()
        self.assertEqual(result['get_responses'][0]['request_query_names'],['index','since','since_date'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('999888777',json.dumps(result))

    def test_history_cursor_continues_after_diagnostic_response_cap(self):
        observation=ReaderObservation()
        response=Mock(url='https://tapas.io/profile/transactions',status=200)
        response.request.method='GET'
        response.request.resource_type='fetch'
        response.headers={'content-type':'application/json'}
        for index in range(90):
            response.json.return_value={'data':{'pagination':{'index':index},'transactions':[
                {'id':123456789,'user_id':987654321,'balance':100,'human_url':'private'}]}}
            observation.on_response(response)
        self.assertEqual(len(observation.responses),80)
        self.assertEqual(observation.library_values['/profile/transactions']['data']['pagination']['index'],89)
        summary=json.dumps(observation.summary())
        self.assertNotIn('123456789',summary)
        self.assertNotIn('987654321',summary)
        self.assertNotIn('private',summary)
    def test_lazy_html_preserves_only_requested_episode_order_and_signature(self):
        parser = ReaderHTML("81")
        parser.feed('''<div id="episode-81" data-episode-id="81" data-token="PRIVATE">
          <article><img class="content__img js-lazy" src="data:image/gif;base64,PLACEHOLDER"
              data-src="https://us-a.tapas.io/pc/a/first.jpg?__token__=TEST&amp;version=v4">
          <img class="content__img" data-src="https://us-a.tapas.io/pc/b/second.jpg?__token__=TEST2"></article>
          <img class="avatar" src="https://us-a.tapas.io/pc/avatar.jpg"></div>
          <div id="episode-82"><img class="content__img" data-src="https://us-a.tapas.io/pc/next.jpg"></div>''')
        self.assertEqual(ordered_sources(parser.images), [
            "https://us-a.tapas.io/pc/a/first.jpg?__token__=TEST&version=v4",
            "https://us-a.tapas.io/pc/b/second.jpg?__token__=TEST2"])
        self.assertEqual(parser.attributes, {"data-episode-id": "81"})
        summary = source_summary(parser.images)
        self.assertEqual(summary["data-src"]["comic_asset_count"], 2)
        self.assertEqual(summary["src"]["comic_asset_count"], 0)
        self.assertNotIn("TEST", json.dumps(summary))
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_complete_manifest_required_and_invalid_lazy_source_cannot_escape_host(self):
        valid = {"tag": "IMG", "url": "https://us-a.tapas.io/pc/a.jpg", "sources": {}}
        self.assertIsNone(ordered_sources([]))
        self.assertIsNone(ordered_sources([valid, {"tag": "IMG", "url": "data:image/gif;base64,blank"}]))
        self.assertIsNone(ordered_sources([{"tag": "IMG", "sources": {"data-src": "https://evil.test/pc/a.jpg"}}]))
        self.assertIsNone(ordered_sources([{"tag": "DIV", "url": valid["url"]}]))
        self.assertIsNone(ordered_sources([valid] * 501))
        with self.assertRaises(ArchiveError):
            ordered_sources([valid, valid])

    def test_original_lazy_source_preferred_to_loaded_placeholder(self):
        self.assertEqual(ordered_sources([{"tag": "IMG", "url": "data:image/gif;base64,blank",
            "loaded": True, "sources": {"data-src": "https://us-a.tapas.io/pc/a.jpg?sig=TEST"}}]),
            ["https://us-a.tapas.io/pc/a.jpg?sig=TEST"])

    def test_primitives_are_restricted_to_booleans_and_platform_content_ids(self):
        values = safe_primitives('''var seriesId = 42; episodeId: 81, isFree: true,
            isPurchased: false, userId: 123, token: "PRIVATE", isUnlocked: "PRIVATE",
            unrelatedseriesId: 999, isAccessible: falseThing''')
        self.assertEqual(values, {"seriesId": 42, "episodeId": 81, "isFree": True, "isPurchased": False})

    def test_quoted_content_ids_supported_without_coercing_access_flags(self):
        values = safe_primitives('''seriesId="262669"; episodeID:'81'; isUnlocked:"true"; userId:"123";''')
        self.assertEqual(values, {"seriesId": 262669, "episodeID": 81})

    def test_series_ids_that_disagree_are_refused_instead_of_keeping_the_last_one(self):
        self.assertEqual(safe_primitives('seriesId=42; "seriesId":"42"; series_id: 42; episodeId: 81; episodeId: 82'),
                         {'seriesId': 42, 'series_id': 42, 'episodeId': 82})
        for text in ('seriesId=42; seriesId="43"', 'seriesId: 42; series_id: 43', "seriesID='42'; seriesId=7"):
            with self.subTest(text=text), self.assertRaises(MetadataError):
                safe_primitives(text)

    def test_valueless_attributes_are_read_as_empty_not_as_a_parser_failure(self):
        parser = ReaderHTML('81')
        parser.feed('<div data-series-id><div id="episode-81" data-series-id><img class><img src>'
                    '<img class="content__img" data-src><img class="content__img" data-src="https://us-a.tapas.io/pc/a.jpg?sig=TEST">'
                    '<a href>Empty</a><a href="/episode/82" rel>Next</a><script src>var seriesId=42;</script></div></div>')
        self.assertEqual(parser.series_ids, set())
        self.assertEqual(parser.attributes, {})
        self.assertEqual(len(parser.images), 2)
        self.assertIsNone(ordered_sources(parser.images))
        self.assertEqual([v['source_episode_id'] for v in parser.episode_links.values()], ['82'])
        self.assertEqual(parser.scripts, [])
        self.assertEqual(safe_primitives('\n'.join(parser.inline)), {'seriesId': 42})

    def test_json_summaries_never_include_values_or_dynamic_keys(self):
        value = {"episodes": [{"id": 81, "imageUrl": "SIGNED", "purchased": True}],
                 "email": "EMAIL", "sessionToken": "SECRET", "PRIVATE-DYNAMIC-KEY": "PRIVATE"}
        text = json.dumps(json_shape(value))
        for secret in ("SIGNED", "EMAIL", "SECRET", "PRIVATE", "sessionToken", "email"):
            self.assertNotIn(secret, text)
        self.assertEqual(json_shape(value)["episodes"]["item_shape"]["purchased"], "bool")

    def test_script_endpoint_literals_strip_queries_and_skip_dynamic_templates(self):
        self.assertEqual(script_routes('''"/tapas/series/episodes?token=PRIVATE";
            '/account/signin'; "/episode/" + id; '/episode/${token}'; 'https://evil.test/api';'''),
            ["/account/signin", "/episode/", "/tapas/series/episodes"])

    def test_published_library_and_history_routes_remain_visible_without_queries(self):
        self.assertEqual(script_routes('''"/reading-list?category=SUBSCRIBED";
            '/profile/transactions?since=PRIVATE'; '/profile/${accountId}';'''),
            ['/profile/transactions','/reading-list'])

    def test_public_script_context_keeps_code_and_redacts_escaped_string_values(self):
        source = r'''const secret="PRIVATE\"VALUE";function list(){get('/reading-list', {category:"SUBSCRIBED",token:'PRIVATE'});}'''
        contexts = public_script_contexts(source,'/resources/js/example.js')
        self.assertTrue(contexts)
        self.assertNotIn('PRIVATE',json.dumps(contexts))
        self.assertIn('function list()',contexts[0]['sanitized_context'])
        self.assertIn('SUBSCRIBED',contexts[0]['sanitized_context'])

    def test_observer_filters_foreign_responses_and_redacts_values_and_post_bodies(self):
        observation = ReaderObservation()
        response = Mock(url="https://tapas.io/tapas/episodes?cookie=PRIVATE", status=200)
        response.request.method = "GET"
        response.request.resource_type = "xhr"
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"episodes": [{"id": 81, "image": "SIGNED"}]}
        observation.on_response(response)
        response.request.method = "POST"
        observation.on_response(response)
        response.request.method = "GET"
        response.url = "https://tracker.example/episode/81"
        observation.on_response(response)
        result = observation.summary()
        self.assertEqual(len(result["get_responses"]), 1)
        self.assertEqual(result["other_observed_responses"][0]["method"], "POST")
        self.assertEqual(result["get_responses"][0]["path"], "/tapas/episodes")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SIGNED", json.dumps(result))

    def test_malformed_link_is_skipped_without_losing_the_rest_of_the_page(self):
        parser=ReaderHTML('81')
        parser.feed('<a href="//[broken">PRIVATE</a><script src="/resources/js/app.js">var a=1;</script>'
                    '<script>var seriesId=42;</script><a href="/episode/82" rel="next">Next</a>')
        self.assertEqual([v['source_episode_id'] for v in parser.episode_links.values()],['82'])
        self.assertEqual(parser.scripts,['/resources/js/app.js'])
        self.assertEqual(safe_primitives('\n'.join(parser.inline)),{'seriesId':42})

    def test_catalog_filters_skip_unexpected_option_lists_and_entries(self):
        daily=('story-api.tapas.io','/cosmos/api/v1/landing/daily')
        for data in ({'data':{'bmList':'Premium'}},{'data':{}},{'data':'x'},{'data':{'bmList':None}}):
            self.assertIsNone(public_access_filters(*daily,data))
        self.assertIsNone(public_access_filters('api.tapas.io','/app-settings',{'browse_filters':{'label':'Free'}}))
        filters=public_access_filters(*daily,{'data':{'bmList':['Free',None,{'value':5,'key':'F'},{'value':'Free'+' '*70,'key':'F'},
                                                               {'key':'F'},{'value':'Paid','key':'P'}]}})
        self.assertEqual(filters['recognized_options'],[{'label':'paid','code':'P','code_recognized':True}])
        self.assertEqual(filters['option_count'],6)

    def test_public_script_context_keeps_published_route_literals_but_not_other_strings(self):
        contexts=public_script_contexts("""fetch('/reading-list/page'); load("/series/episodes"); const t="PRIVATE_TOKEN";
            go('/reading-list');""",'/resources/js/example.js')
        text=contexts[0]['sanitized_context']
        self.assertIn("'/reading-list/page'",text);self.assertIn('"/series/episodes"',text)
        self.assertIn('"<string>"',text);self.assertNotIn('PRIVATE_TOKEN',text)

    def response(self,url,status=200,kind='fetch',method='GET',content_type='application/json',length=None,body=None,text=None):
        response=Mock(url=url,status=status)
        response.request.method=method;response.request.resource_type=kind
        response.headers={'content-type':content_type}
        if length is not None:response.headers['content-length']=length
        response.json.return_value=body if body is not None else {'data':{}}
        response.text.return_value=text
        return response

    def test_observer_ignores_paths_with_unexpected_characters_and_non_page_resource_kinds(self):
        observation=ReaderObservation()
        for url,kind in (('https://tapas.io/episode/81/PRIVATE%20TOKEN','fetch'),('https://tapas.io/'+'a'*161,'fetch'),
                         ('https://tapas.io/img/logo.png','image'),('https://tapas.io/css/site.css','stylesheet'),
                         ('http://tapas.io/episode/81','fetch'),('https://tapas.io/ink/purchase','websocket')):
            observation.on_response(self.response(url,kind=kind))
        self.assertEqual(observation.responses,[])

    def test_observer_after_the_response_cap_keeps_only_small_history_pages(self):
        observation=ReaderObservation()
        for index in range(80):observation.on_response(self.response(f'https://tapas.io/series/{index}'))
        self.assertEqual(len(observation.responses),80)
        other=self.response('https://tapas.io/series/99')
        large=self.response('https://tapas.io/profile/transactions',length=str(2*1024*1024+1))
        small=self.response('https://tapas.io/profile/transactions',length='100',body={'data':{'page':'last'}})
        post=self.response('https://tapas.io/profile/transactions',method='POST')
        for item in (other,large,post):observation.on_response(item)
        other.json.assert_not_called();large.json.assert_not_called();post.json.assert_not_called()
        self.assertEqual(observation.library_values,{})
        observation.on_response(small)
        self.assertEqual(observation.library_values['/profile/transactions'],{'data':{'page':'last'}})
        self.assertEqual(len(observation.responses),80)

    def test_observer_keeps_only_safe_pagination_query_values_and_reads_only_successful_bodies(self):
        observation=ReaderObservation()
        failed=self.response('https://tapas.io/series/42/episodes?page=2&token=PRIVATE',status=404)
        observation.on_response(failed)
        failed.json.assert_not_called()
        self.assertEqual(observation.responses[0]['pagination_query'],{'page':'2'})
        self.assertNotIn('shape',observation.responses[0])
        observation.on_response(self.response('https://tapas.io/series/43/episodes?page=PRIVATE&max_limit=20&sort=DESC&since=5'))
        self.assertEqual(observation.responses[1]['pagination_query'],{'max_limit':'20','sort':'DESC','since':'5'})
        observation.on_response(self.response('https://tapas.io/profile/transactions?since=5&page=3'))
        self.assertEqual(observation.responses[2]['pagination_query'],{'page':'3'})
        observation.body_budget=10
        big=self.response('https://tapas.io/series/44',length='11')
        observation.on_response(big)
        big.json.assert_not_called()
        self.assertNotIn('shape',observation.responses[3])

    def test_observer_retains_identity_and_series_bodies_in_memory_but_only_those_and_library_routes(self):
        observation=ReaderObservation()
        for url,body in (('https://tapas.io/who-am-i',{'data':{'user':{'id':1}}}),
                         ('https://tapas.io/episode/81/info',{'data':{'id':81}}),
                         ('https://tapas.io/series/42/episodes',{'data':{'episodes':[]}}),
                         ('https://tapas.io/series/42/other',{'data':{'x':1}}),
                         ('https://tapas.io/library/subscribed',{'data':{'items':[]}})):
            observation.on_response(self.response(url,body=body))
        self.assertEqual(sorted(observation.values),['/episode/81/info','/series/42/episodes','/who-am-i'])
        self.assertEqual(list(observation.library_values),['/library/subscribed'])
        post=self.response('https://tapas.io/who-am-i',method='POST',body={'data':{'user':{'id':2}}})
        observation.on_response(post)
        self.assertEqual(observation.values['/who-am-i'],{'data':{'user':{'id':1}}})

    def test_observer_records_only_static_script_routes_and_spends_its_body_budget(self):
        observation=ReaderObservation()
        script=self.response('https://us-a.tapas.io/resources/js/app.js',kind='script',content_type='application/javascript',
                             text='const a="/series/episodes?token=PRIVATE"; const b="/account/signin";')
        observation.on_response(script)
        self.assertEqual(observation.summary()['static_script_routes'],['/account/signin','/series/episodes'])
        self.assertEqual(observation.summary()['inspected_script_paths'],['/resources/js/app.js'])
        self.assertLess(observation.body_budget,4*1024*1024)
        other=self.response('https://other.tapas.io/resources/js/app.js',kind='script',text='"/x/episode"')
        observation.on_response(other)
        self.assertEqual(observation.summary()['inspected_script_paths'],['/resources/js/app.js'])

    def test_observer_json_bodies_spend_the_body_budget_with_or_without_a_length(self):
        observation=ReaderObservation()
        observation.body_budget=100
        observation.on_response(self.response('https://tapas.io/series/1',length='30',body={'data':{}}))
        self.assertEqual(observation.body_budget,70)
        unsized=self.response('https://tapas.io/series/2',body={'data':'x'*200})
        observation.on_response(unsized)
        # Without a length the parsed body's size is charged, so the budget is spent.
        self.assertIn('/series/2',observation.values)
        self.assertLessEqual(observation.body_budget,0)
        later=self.response('https://tapas.io/series/3')
        observation.on_response(later)
        later.json.assert_not_called()
        self.assertNotIn('/series/3',observation.values)
        self.assertNotIn('shape',observation.responses[-1])

    def test_observer_never_propagates_errors_that_could_carry_credentials(self):
        observation=ReaderObservation()
        response=self.response('https://tapas.io/series/42')
        response.json.side_effect=ValueError('PRIVATE_COOKIE')
        observation.on_response(response)
        observation.on_response(object())
        self.assertNotIn('PRIVATE',json.dumps(observation.summary()))

    def test_observer_records_documents_and_unrelated_json_without_reading_pages_or_inventing_filters(self):
        observation=ReaderObservation(catalog_filters=True)
        page=self.response('https://tapas.io/episode/81',kind='document',content_type='text/html; charset=utf-8',text='<p>PRIVATE</p>')
        observation.on_response(page)
        page.text.assert_not_called();page.json.assert_not_called()
        observation.on_response(self.response('https://api.tapas.io/other',body={'data':{'bmList':[{'key':'P','value':'Premium'}]}}))
        self.assertEqual([row['path'] for row in observation.summary()['get_responses']],['/episode/81','/other'])
        self.assertNotIn('public_access_filters',observation.summary()['get_responses'][1])
        self.assertNotIn('PRIVATE',json.dumps(observation.summary()))


if __name__ == "__main__":
    unittest.main()
