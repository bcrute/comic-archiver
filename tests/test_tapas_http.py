import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from comic_archive.adapters.tapas_http import TapasHTTPAdapter, access_state
from comic_archive.models import ArchiveError, Page, Series
from comic_archive.errors import MetadataError, UnsupportedContent


def envelope(value):
    return json.dumps({"data": value, "error_details": None}).encode()


class TapasHTTPTests(unittest.TestCase):
    def test_storage_preview_uses_authenticated_metadata_without_reader_or_asset_requests(self):
        import tempfile
        from comic_archive.preflight import preview_series
        from comic_archive.storage import NOVEL_BYTES
        for novel in (False, True):
            self.transport.get.reset_mock()
            self.transport.get.side_effect = [envelope({'user': {'id':123}}),
                envelope({'user': {'id':123}}),
                envelope({'id':42, 'title':'PRIVATE', 'book':novel, 'comic':not novel}),
                envelope({'episodes':[self.info | {'book':novel},
                    self.info | {'id':82, 'scene':2, 'book':novel, 'free':False, 'unlocked':False}],
                    'pagination':{'page':1, 'total':2, 'has_next':False}})]
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/'Library'
                result = preview_series(self.adapter, 'https://tapas.io/series/42/info', root)['storage_preview']
                self.assertFalse(root.exists())
            self.assertEqual(result['observed_episode_count'],2)
            self.assertEqual(result['planned_episode_count'],1)
            self.assertEqual(result['content_type'],'novel' if novel else 'comic')
            self.assertTrue(result['content_type_verified'])
            if novel:self.assertEqual(result['estimated_additional_archive_bytes'],NOVEL_BYTES)
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertEqual([call.args[0] for call in self.transport.get.call_args_list],[
                'https://tapas.io/who-am-i','https://tapas.io/who-am-i',
                'https://tapas.io/series/42','https://tapas.io/series/42/episodes?page=1&max_limit=20'])

    def test_comic_only_scan_holds_explicit_novel_before_episode_pagination(self):
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),
            envelope({'id':42,'title':'S','book':True,'comic':False,'token':'PRIVATE'})]
        result=self.adapter.inspect_series('42',comic_only=True)
        self.assertTrue(result['authenticated']);self.assertFalse(result['complete'])
        self.assertEqual(result['unsupported_series_reason'],'novel')
        self.assertEqual(result['episodes'],[]);self.assertTrue(result['episode_enumeration_skipped'])
        self.assertEqual([call.args[0] for call in self.transport.get.call_args_list],
                         ['https://tapas.io/who-am-i','https://tapas.io/series/42'])
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_comic_only_scan_keeps_unknown_contradictory_and_comic_series_enumeration(self):
        for flags in ({'book':False,'comic':True},{'book':True,'comic':True},
                      {'book':'true','comic':False},{'book':True,'comic':0},{'book':True},{}):
            self.transport.get.reset_mock()
            self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S'}|flags),
                envelope({'episodes':[self.info],'pagination':{'page':1,'total':1,'has_next':False}})]
            result=self.adapter.inspect_series('42',comic_only=True)
            self.assertTrue(result['complete']);self.assertEqual(len(result['episodes']),1)
            self.assertNotIn('episode_enumeration_skipped',result)
            self.transport.get.assert_called_with('https://tapas.io/series/42/episodes?page=1&max_limit=20')

    def test_default_series_inspection_still_enumerates_novels_and_invalid_option_stops(self):
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S','book':True,'comic':False}),
            envelope({'episodes':[self.info|{'book':True}],'pagination':{'page':1,'total':1,'has_next':False}})]
        result=self.adapter.inspect_series('42')
        self.assertTrue(result['complete']);self.assertIsNone(result['episodes'][0]['authorization_reason'])
        self.assertEqual(result['episodes'][0]['access_state'],'tapas_free')
        self.transport.get.reset_mock()
        with self.assertRaises(ArchiveError):self.adapter.inspect_series('42',comic_only=1)
        self.transport.get.assert_not_called()

    def test_authenticated_session_structure_never_copies_values_or_nested_account_keys(self):
        user={'id':123,'uname':'PRIVATE_ACCOUNT','email':'PRIVATE_EMAIL','auth_token':'PRIVATE_TOKEN',
              'library':{'PRIVATE_DYNAMIC_ID':[123]},'purchased_series':[123],
              'ink_balance':99999}
        self.transport.get.side_effect=[envelope({'user':user,'library':[{'id':'PRIVATE_ID'}]})]
        self.adapter.check_session()
        self.assertEqual(self.adapter._session_structure,{'fields':{'id':'int','uname':'str',
            'library':'dict','purchased_series':'list','ink_balance':'int'},'field_count':7,'truncated':False,
            'data_fields':{'user':'dict','library':'list'},'data_truncated':False})
        self.assertNotIn('PRIVATE',json.dumps(self.adapter._session_structure))
        self.assertNotIn('123',json.dumps(self.adapter._session_structure))
        self.transport.get.assert_called_once_with('https://tapas.io/who-am-i')
        self.responses(user={'id':0})
        with self.assertRaises(ArchiveError):self.adapter.check_session()
        self.assertEqual(self.adapter._session_structure,{})

    def test_count_gap_preserves_only_normalized_returned_candidates_without_claiming_completeness(self):
        from comic_archive.adapters.pagination import EpisodeCountMismatch
        for change in ({},{'free':'PRIVATE_UNKNOWN_SCHEMA'}):
            self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S'}),
                envelope({'episodes':[self.info|change|{'token':'PRIVATE'},self.info|{'id':82,'scene':2}],
                          'pagination':{'page':1,'total':3,'has_next':False,'token':'PRIVATE'}})]
            if change:
                with self.assertRaises(MetadataError) as caught:self.adapter.inspect_series('42',strict_order=False)
                self.assertNotIsInstance(caught.exception,EpisodeCountMismatch)
            else:
                with self.assertRaises(EpisodeCountMismatch) as caught:self.adapter.inspect_series('42',strict_order=False)
                partial=caught.exception.partial_inspection
                self.assertFalse(partial['complete']);self.assertTrue(partial['authenticated'])
                self.assertEqual(partial['source_series_id'],'42')
                self.assertEqual([row['id'] for row in partial['episodes']],['81','82'])
                self.assertEqual([row['order'] for row in partial['episodes']],[1,2])
                self.assertNotIn('PRIVATE',json.dumps(caught.exception.__dict__))
            self.transport.get.assert_called_with('https://tapas.io/series/42/episodes?page=1&max_limit=20')

    def test_page_size_probe_uses_one_normal_metadata_page_and_safe_projection(self):
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S'}),
            envelope({'episodes':[self.info|{'id':key} for key in range(1,51)],
                'pagination':{'page':2,'total':100,'has_next':True,'limit':50,'token':'PRIVATE'},'body':'PRIVATE'})]
        result=self.adapter.inspect_episode_page_size('42',50)
        self.assertEqual(result['returned_episode_count'],50)
        self.assertFalse(result['complete']);self.assertFalse(result['reader_or_comic_pages_requested'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual([c.args[0] for c in self.transport.get.call_args_list],[
            'https://tapas.io/who-am-i','https://tapas.io/series/42',
            'https://tapas.io/series/42/episodes?page=1&max_limit=50'])

    def test_metadata_diagnostics_report_denial_and_unsupported_reasons_without_reader_requests(self):
        for change,reason in (({},None),({'free':False,'unlocked':False},'access_denied'),
                ({'book':True},None),({'closing_date':'SESSION_PRIVATE'},'temporary_access'),
                ({'free':'SESSION_PRIVATE'},'authorization_unknown')):
            self.transport.reset_mock();self.responses(info=self.info|change)
            result=self.adapter.inspect_access('https://tapas.io/episode/81')
            self.assertEqual(result['reason'],reason)
            self.assertEqual(result['authorization_eligible'],reason is None)
            self.assertFalse(result['reader_or_comic_pages_requested'])
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertEqual([c.args[0] for c in self.transport.get.call_args_list],
                             ['https://tapas.io/who-am-i','https://tapas.io/episode/81/info'])

    def test_unsupported_metadata_has_specific_safe_reason_without_requesting_a_reader(self):
        for change,code in (({'scheduled':True},'scheduled'),
                            ({'closing_date':'PRIVATE_VALUE'},'temporary_access')):
            self.transport.reset_mock()
            self.responses(info=self.info|change)
            with self.assertRaises(UnsupportedContent) as error:
                self.adapter.capture('https://tapas.io/episode/81')
            self.assertEqual(error.exception.code,code)
            self.assertNotIn('PRIVATE',str(error.exception))
            self.assertEqual(self.transport.get.call_count,2)

    def test_series_metadata_uses_observed_markup_and_cover_without_reader_requests(self):
        from test_series_metadata import HTML
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),
            envelope({'id':42,'title':'Series','url':'series-slug','comic':True,'book':False,
                      'thumb_url':'https://us-a.tapas.io/sa/cover.jpg?signature=PRIVATE'}),HTML.encode()]
        series=self.adapter.series_metadata('42')
        self.assertEqual(series.creators,('Publisher & Artist',))
        self.assertEqual(series.description,'A comic.\nSecond line.')
        self.assertEqual(series.credits,'Created by Artist\nTranslated by Translator')
        self.assertNotIn('PRIVATE',repr(series))
        self.assertEqual([call.args[0] for call in self.transport.get.call_args_list],
            ['https://tapas.io/who-am-i','https://tapas.io/series/42','https://tapas.io/series/series-slug/info'])
        self.transport.get.side_effect=None
        self.transport.get.return_value=b'original-cover-bytes'
        self.assertEqual(self.adapter.retrieve_cover(series,max_bytes=10),b'original-cover-bytes')
        self.transport.get.assert_called_with('https://us-a.tapas.io/sa/cover.jpg?signature=PRIVATE',max_bytes=10)

    def test_series_inspection_pauses_on_authorization_schema_change(self):
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),
            envelope({'id':42,'title':'S'}),envelope({'pagination':{'page':2,'has_next':False,'total':1},
                'episodes':[self.info | {'free':'unexpected'}]})]
        with self.assertRaises(MetadataError):
            self.adapter.inspect_series('42')
        self.assertEqual(self.transport.get.call_count,3)

    def setUp(self):
        self.info = {"id": 81, "title": "Episode 1", "scene": 1,
                     "free": True, "unlocked": False, "must_pay": False,
                     "scheduled": False, "book": False, "closing_date": None,
                     "publish_date": "2023-07-19T18:00:00Z"}
        self.html = b'''<script>var seriesId = 42;</script><div id="episode-81"><article>
            <img class="content__img" src="data:image/gif;base64,PLACEHOLDER"
                 data-src="https://us-a.tapas.io/pc/a/1.jpg?signature=TEST">
            <img class="content__img" data-src="https://us-a.tapas.io/pc/a/2.jpg?signature=TEST">
            </article></div>'''
        self.transport = Mock()
        self.adapter = TapasHTTPAdapter(transport=self.transport)

    def responses(self, info=None, html=None, user=None):
        self.transport.get.side_effect = [envelope({"user": user if user is not None else {"id": 123, "uname": "PRIVATE_ACCOUNT"}}),
            envelope(info if info is not None else self.info), html if html is not None else self.html,
            envelope({"id": 42, "title": "Series", "url": "series-slug", "comic": True, "book": False})]

    def test_explicit_temporary_unlock_captures_expiry_after_normal_account_and_reader_checks(self):
        from datetime import datetime, timezone
        now=datetime(2026,10,8,18,tzinfo=timezone.utc)
        self.responses(info=self.info|{'free':False,'unlocked':True,'closing_date':'2026-10-08T20:00:00Z'})
        with patch('comic_archive.expiry.now',return_value=now):
            manifest=self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(manifest.episodes[0].access,'tapas_unlocked')
        self.assertEqual(manifest.episodes[0].access_expires_at,'2026-10-08T20:00:00+00:00')
        self.assertEqual(self.transport.get.call_count,4)

    def test_grant_expiring_between_metadata_and_reader_does_not_start_reader(self):
        from datetime import datetime, timezone
        from comic_archive.errors import AccessDenied
        before=datetime(2026,10,8,18,tzinfo=timezone.utc);deadline=datetime(2026,10,8,20,tzinfo=timezone.utc)
        self.responses(info=self.info|{'closing_date':deadline.isoformat()})
        with patch('comic_archive.expiry.now',side_effect=[before,deadline]),self.assertRaises(AccessDenied):
            self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(self.transport.get.call_count,2)

    def test_authenticated_info_gate_html_order_and_automatic_metadata(self):
        self.responses()
        result = self.adapter.capture("https://tapas.io/episode/81")
        self.assertEqual(result.series.id, "series-slug")
        self.assertEqual(result.series.platform_id, "42")
        episode = result.episodes[0]
        self.assertEqual(episode.access, "tapas_free")
        self.assertEqual(episode.title, "Episode 1")
        self.assertEqual(episode.published_at, "2023-07-19T18:00:00Z")
        self.assertEqual([p.order for p in episode.pages], [1, 2])
        self.assertEqual([call.args[0] for call in self.transport.get.call_args_list], [
            "https://tapas.io/who-am-i", "https://tapas.io/episode/81/info",
            "https://tapas.io/episode/81", "https://tapas.io/series/42"])
        self.assertNotIn("PRIVATE_ACCOUNT", repr(result))

    def test_only_reader_html_403_after_fresh_access_gate_is_retry_eligible(self):
        for kind,eligible in (('html',True),('challenge',False),('json',False),(None,False)):
            with self.subTest(kind=kind):
                failure=ArchiveError('PRIVATE response')
                failure.source_http_status=403; failure.source_response_kind=kind
                self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope(self.info),failure]
                with self.assertRaises(ArchiveError) as caught:self.adapter.capture('https://tapas.io/episode/81')
                self.assertEqual(getattr(caught.exception,'source_retry_episode_id',None),'81' if eligible else None)
        failure=ArchiveError('PRIVATE metadata response');failure.source_http_status=403;failure.source_response_kind='html'
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),failure]
        with self.assertRaises(ArchiveError) as caught:self.adapter.capture('https://tapas.io/episode/81')
        self.assertFalse(hasattr(caught.exception,'source_retry_episode_id'))

    def test_locked_wuf_preview_and_inconsistent_flags_stop_before_reader_or_assets(self):
        for updates in ({"free": False, "unlocked": False}, {"must_pay": True},
                        {"scheduled": True}, {"closing_date": "expired"},
                        {"free": "true", "unlocked": 1}, {"must_pay": 0}):
            with self.subTest(updates=updates):
                self.transport.reset_mock()
                self.responses(info=self.info | updates)
                with self.assertRaises(ArchiveError):
                    self.adapter.capture("https://tapas.io/episode/81")
                self.assertEqual(self.transport.get.call_count, 2)

    def test_unlocked_flag_requires_explicit_boolean_and_current_allowed_state(self):
        self.assertEqual(access_state(self.info | {"free": False, "unlocked": True}), "tapas_unlocked")
        with self.assertRaises(ArchiveError):
            access_state(self.info | {"free": False, "unlocked": True, "must_pay": True})

    def test_no_session_and_wrong_episode_stop_before_reader(self):
        for user in ({}, {"id": 0}, {"id": True}, {"id": "123"}):
            self.transport.reset_mock()
            self.responses(user=user)
            with self.assertRaises(ArchiveError):
                self.adapter.capture("https://tapas.io/episode/81")
            self.assertEqual(self.transport.get.call_count, 1)
        self.transport.reset_mock()
        self.responses(info=self.info | {"id": 82})
        with self.assertRaises(ArchiveError):
            self.adapter.capture("https://tapas.io/episode/81")
        self.assertEqual(self.transport.get.call_count, 2)

    def test_expected_account_change_stops_before_episode_metadata_or_reader(self):
        self.adapter.expected_account_id = "999"
        self.responses()
        with self.assertRaises(ArchiveError):
            self.adapter.capture("https://tapas.io/episode/81")
        self.assertEqual(self.transport.get.call_count, 1)

    def test_access_probe_never_requests_the_reader_or_page_assets(self):
        self.responses(info=self.info | {"free": False, "unlocked": True})
        result = self.adapter.episode_access("https://tapas.io/episode/81")
        self.assertEqual(result["access_state"], "tapas_unlocked")
        self.assertEqual([call.args[0] for call in self.transport.get.call_args_list],
                         ["https://tapas.io/who-am-i", "https://tapas.io/episode/81/info"])

    def test_partial_or_unrelated_html_does_not_produce_manifest(self):
        for html in (b'<div id="episode-82"><img class="content__img" data-src="https://us-a.tapas.io/pc/a.jpg"></div>',
                     self.html.replace(b'https://us-a.tapas.io/pc/a/2.jpg?signature=TEST', b'data:image/gif;base64,blank'),
                     self.html.replace(b'us-a.tapas.io', b'evil.example')):
            self.transport.reset_mock()
            self.responses(html=html)
            with self.assertRaises(ArchiveError):
                self.adapter.capture("https://tapas.io/episode/81")
            self.assertEqual(self.transport.get.call_count, 3)

    def test_wrong_series_id_and_error_payload_are_safe(self):
        self.responses()
        with self.assertRaises(ArchiveError):
            self.adapter.capture("https://tapas.io/episode/81", series_id="unrelated")
        self.transport.get.side_effect = [b'{"data":null,"error_details":"PRIVATE_TOKEN"}']
        with self.assertRaises(ArchiveError) as error:
            self.adapter.check_session()
        self.assertNotIn("PRIVATE_TOKEN", str(error.exception))

    def test_refresh_rechecks_authorization_before_reading_new_manifest(self):
        self.responses()
        episode = self.adapter.capture("https://tapas.io/episode/81").episodes[0]
        self.transport.reset_mock()
        self.responses(info=self.info | {"free": False, "unlocked": False})
        with self.assertRaises(ArchiveError):
            self.adapter.refresh(episode)
        self.assertEqual(self.transport.get.call_count, 2)

    def test_pagination_follows_server_cursor_orders_episodes_and_checks_total(self):
        self.transport.get.side_effect = [
            envelope({"pagination": {"page": 2, "has_next": True, "total": 3},
                      "episodes": [self.info | {"id": 83, "scene": 3}, self.info]}),
            envelope({"pagination": {"page": 3, "has_next": False, "total": 3},
                      "episodes": [self.info | {"id": 82, "scene": 2}]})]
        result = self.adapter.enumerate_episodes(42)
        self.assertEqual([item["id"] for item in result], [81, 82, 83])
        self.assertEqual([call.args[0] for call in self.transport.get.call_args_list], [
            "https://tapas.io/series/42/episodes?page=1&max_limit=20",
            "https://tapas.io/series/42/episodes?page=2&max_limit=20"])

    def test_pagination_repeats_missing_items_changed_totals_and_cursors_rejected(self):
        first = {"pagination": {"page": 2, "has_next": True, "total": 2}, "episodes": [self.info]}
        for second in (
            {"pagination": {"page": 3, "has_next": False, "total": 2}, "episodes": [self.info]},
            {"pagination": {"page": 3, "has_next": False, "total": 2}, "episodes": []},
            {"pagination": {"page": 3, "has_next": False, "total": 3}, "episodes": [self.info | {"id": 82, "scene": 2}]},
            {"pagination": {"page": 2, "has_next": True, "total": 2}, "episodes": [self.info | {"id": 82, "scene": 2}]},
        ):
            self.transport.get.side_effect = [envelope(first), envelope(second)]
            with self.assertRaises(ArchiveError):
                self.adapter.enumerate_episodes(42)

    def test_discovery_retains_access_without_inventing_ambiguous_episode_orders(self):
        for scene in (0, 1, None):
            items = [self.info, self.info | {"id": 82, "scene": scene, "free": False, "unlocked": True}]
            page = envelope({"pagination": {"page": 2, "has_next": False, "total": 2}, "episodes": items})
            self.transport.get.side_effect = [page]
            with self.assertRaises(ArchiveError):
                self.adapter.enumerate_episodes(42)
            self.transport.get.side_effect = [envelope({"user": {"id": 123}}),
                envelope({"id": 42, "title": "S"}), page]
            result = self.adapter.inspect_series("42", strict_order=False)
            self.assertTrue(result["complete"])
            self.assertFalse(result["ordering_verified"])
            episode = next(e for e in result["episodes"] if e["id"] == "82")
            self.assertIsNone(episode["order"])
            self.assertEqual(episode["source_scene"], scene)
            self.assertEqual(episode["access_state"], "tapas_unlocked")

    def test_series_metadata_retains_denied_and_unsupported_reasons_without_reader_or_private_values(self):
        items=[self.info|{'id':81,'scene':1},self.info|{'id':82,'scene':2,'must_pay':True},
               self.info|{'id':83,'scene':3,'book':True},self.info|{'id':84,'scene':4,'scheduled':True},
               self.info|{'id':85,'scene':5,'closing_date':'EXPIRY_PRIVATE'}]
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S'}),
            envelope({'pagination':{'page':2,'has_next':False,'total':5},'episodes':items})]
        result=self.adapter.inspect_series('42')
        self.assertEqual([row['authorization_reason'] for row in result['episodes']],
                         [None,'access_denied',None,'scheduled','temporary_access'])
        self.assertEqual([row['access_state'] for row in result['episodes']],
                         ['tapas_free','unavailable_or_unverified','tapas_free','unavailable_or_unverified','unavailable_or_unverified'])
        self.assertEqual(self.transport.get.call_count,3)
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_missing_or_nonboolean_allow_flags_fail_closed(self):
        for key in ("must_pay", "scheduled", "book", "closing_date"):
            info = dict(self.info)
            del info[key]
            with self.assertRaises(ArchiveError):
                access_state(info)

    def test_series_slug_resolves_quoted_server_id_and_checks_metadata_slug(self):
        self.transport.get.side_effect = [b'<script>var seriesId="42";</script>',
            envelope({"id": 42, "title": "Series", "url": "series-slug", "comic": True, "book": False})]
        self.assertEqual(self.adapter.resolve_series("series-slug")["id"], 42)
        self.assertEqual(self.transport.get.call_args_list[0].args[0], "https://tapas.io/series/series-slug/info")
        self.transport.get.side_effect = [b'<script>var seriesId="42";</script>',
            envelope({"id": 42, "url": "wrong-slug"})]
        with self.assertRaises(ArchiveError):
            self.adapter.resolve_series("series-slug")

    def test_sanitized_live_flags_allow_free_episode_and_reject_wuf_without_unlock(self):
        observed = json.loads((Path(__file__).parent / 'fixtures/tapas-live-api-observation.json').read_text())
        self.assertTrue(observed['session']['authenticated'])
        self.assertEqual(access_state(observed['episode']), 'tapas_free')
        locked = observed['known_locked_episode'] | {'closing_date': None}
        self.assertFalse(locked['must_pay'])
        with self.assertRaises(ArchiveError):
            access_state(locked)
        self.assertEqual(observed['initial_html']['data-src']['comic_asset_count'], 88)
        self.assertEqual(observed['dom_before_scroll']['current']['comic_asset_count'], 2)

    def test_sanitized_live_nonfree_episode_has_explicit_allowed_unlock_flags(self):
        observed = json.loads((Path(__file__).parent / 'fixtures/tapas-live-unlocked-observation.json').read_text())
        episode = observed['unlocked_episode']
        self.assertFalse(episode['authorization']['free'])
        self.assertIs(episode['authorization']['unlocked'], True)
        self.assertEqual(access_state(episode['authorization']), 'tapas_unlocked')
        self.assertEqual(episode['page_count'], 47)
        self.assertFalse(episode['scrolling_required'])
        self.assertFalse(observed['discovery']['complete'])
        with self.assertRaises(ArchiveError):
            access_state(episode['authorization'] | {'unlocked': False})

    def test_session_folder_builds_a_tapas_only_cookie_transport_inside_the_library_guard_and_closes_it(self):
        import tempfile
        from comic_archive.network import NetworkGuard
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'session'
            folder.mkdir()
            (folder / 'state.json').write_text(json.dumps({'cookies': [
                {'name': 'session', 'value': 'PRIVATE', 'domain': '.tapas.io', 'expires': -1},
                {'name': 'other', 'value': 'OTHER', 'domain': 'example.com', 'expires': -1}], 'origins': []}))
            with TapasHTTPAdapter(folder) as adapter:
                transport = adapter.transport
                self.assertEqual(transport.cookie_header('tapas.io'), 'session=PRIVATE')
                self.assertIsInstance(transport.network_guard, NetworkGuard)
                self.assertEqual(transport.network_guard.root, (Path(directory) / 'Library').absolute())
                self.assertFalse(transport.client.is_closed)
            self.assertTrue(transport.client.is_closed)
            with self.assertRaises(ArchiveError):
                TapasHTTPAdapter(Path(directory) / 'missing')

    def test_oversized_metadata_answer_is_refused_before_parsing(self):
        self.transport.get.return_value = envelope({'user': {'id': 123}, 'padding': 'x' * (2 * 1024 * 1024)})
        with self.assertRaisesRegex(ArchiveError, 'exceeds the supported size'):
            self.adapter.check_session()

    def test_access_checks_accept_only_exact_episode_urls_and_metadata_for_the_same_episode(self):
        for method in (self.adapter.episode_access, self.adapter.inspect_access):
            self.transport.reset_mock()
            for url in ('https://tapas.io/series/42/info', 'https://tapas.io/episode/abc',
                        'https://tapas.io/episode/81/unlock', 'https://example.com/episode/81',
                        'http://tapas.io/episode/81', None):
                with self.subTest(url=url), self.assertRaises(ArchiveError):
                    method(url)
            self.transport.get.assert_not_called()
            self.transport.reset_mock()
            self.responses(info=self.info | {'id': 82})
            with self.assertRaises(MetadataError):
                method('https://tapas.io/episode/81')
            self.assertEqual(self.transport.get.call_count, 2)
        self.transport.reset_mock()
        with self.assertRaisesRegex(ArchiveError, 'exact observed'):
            self.adapter.inspect_access('https://tapas.io/episode/081')
        self.transport.get.assert_not_called()

    def test_page_size_probe_refuses_unsupported_limits_and_malformed_or_repeated_episode_lists(self):
        for limit in (10, 0, True, '50', 101, None):
            with self.subTest(limit=limit), self.assertRaises(ArchiveError):
                self.adapter.inspect_episode_page_size('42', limit)
        self.transport.get.assert_not_called()
        for episodes, pagination in (('x', {}), ([], None), ([self.info | {'id': key} for key in range(1, 102)], {}),
                ([{'id': 0}], {}), ([{'id': True}], {}), (['row'], {}), ([{'title': 'no id'}], {}),
                ([{'id': 5}, {'id': 5}], {})):
            self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope({'id': 42, 'title': 'S'}),
                envelope({'episodes': episodes, 'pagination': pagination})]
            with self.subTest(episodes=str(episodes)[:40]), self.assertRaises(MetadataError):
                self.adapter.inspect_episode_page_size('42', 100)

    def test_series_identity_reports_only_the_id_and_title(self):
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}),
                                          envelope({'id': 42, 'title': 'S', 'token': 'PRIVATE'})]
        self.assertEqual(self.adapter.series_identity('42'), {'source_series_id': '42', 'title': 'S'})

    def test_missing_or_non_text_titles_are_metadata_errors_not_crashes(self):
        from test_series_metadata import HTML
        for series in ({'id': 42}, {'id': 42, 'title': None}, {'id': 42, 'title': 5}):
            with self.subTest(series=series):
                self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope(series)]
                with self.assertRaises(MetadataError):
                    self.adapter.series_identity('42')
                self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope(series)]
                with self.assertRaises(MetadataError):
                    self.adapter.inspect_series('42')
                self.transport.get.side_effect = [envelope({'user': {'id': 123}}),
                    envelope(series | {'url': 'series-slug', 'comic': True, 'book': False}), HTML.encode()]
                with self.assertRaises(MetadataError):
                    self.adapter.series_metadata('42')
        untitled = {key: value for key, value in self.info.items() if key != 'title'}
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope({'id': 42, 'title': 'S'}),
            envelope({'episodes': [untitled], 'pagination': {'page': 1, 'total': 1, 'has_next': False}})]
        with self.assertRaises(MetadataError):
            self.adapter.inspect_series('42')
        self.responses(info=untitled)
        with self.assertRaisesRegex(ArchiveError, 'title'):
            self.adapter.capture('https://tapas.io/episode/81')
        self.responses(info=untitled)
        self.assertEqual(self.adapter.capture('https://tapas.io/episode/81', title='Supplied').episodes[0].title, 'Supplied')

    def test_series_page_that_is_oversized_or_undecodable_is_refused_before_parsing(self):
        for page in (b'<div data-series-id="42"></div>' + b' ' * (8 * 1024 * 1024), b'<div data-series-id="42">\xff</div>'):
            self.transport.reset_mock()
            self.transport.get.side_effect = [page]
            with self.subTest(size=len(page)), self.assertRaises(MetadataError):
                self.adapter.resolve_series('series-slug')
            self.assertEqual(self.transport.get.call_count, 1)

    def test_reader_that_names_two_series_ids_is_refused_before_series_metadata(self):
        self.responses(html=self.html.replace(b'var seriesId = 42;', b'var seriesId = 42; var series_id = "43";'))
        with self.assertRaises(MetadataError):
            self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(self.transport.get.call_count, 3)

    def test_publish_time_is_kept_only_when_it_is_a_short_iso_date(self):
        for value, expected in (('2023-07-19T18:00:00Z', '2023-07-19T18:00:00Z'),
                                ('2023-07-19T18:00:00+09:00', '2023-07-19T18:00:00+09:00'), ('2023-07-19', '2023-07-19'),
                                ('yesterday', None), ('', None), ('2023-07-19T18:00:00Z' + ' ' * 50, None),
                                ('<script>PRIVATE</script>', None), (1689789600, None), ({'at': 'PRIVATE'}, None), (None, None)):
            self.responses(info=self.info | {'publish_date': value})
            with self.subTest(value=value):
                self.assertEqual(self.adapter.capture('https://tapas.io/episode/81').episodes[0].published_at, expected)

    def test_series_metadata_refuses_unverified_slug_oversized_or_undecodable_page_and_foreign_covers(self):
        from test_series_metadata import HTML
        base = {'id': 42, 'title': 'Series', 'url': 'series-slug', 'comic': True, 'book': False}
        for change, page, requests in (
                ({'url': None}, HTML.encode(), 2), ({'url': 'bad slug'}, HTML.encode(), 2),
                ({}, b'x' * (8 * 1024 * 1024 + 1), 3), ({}, b'\xff\xfe<script>', 3),
                ({'thumb_url': 5}, HTML.encode(), 3),
                ({'thumb_url': 'https://evil.example/cover.jpg'}, HTML.encode(), 3),
                ({'thumb_url': 'http://us-a.tapas.io/cover.jpg'}, HTML.encode(), 3)):
            self.transport.reset_mock()
            self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope(base | change), page]
            with self.subTest(change=change, size=len(page)), self.assertRaises(ArchiveError):
                self.adapter.series_metadata('42')
            self.assertEqual(self.transport.get.call_count, requests)
        self.transport.reset_mock()
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope(base), HTML.encode()]
        series = self.adapter.series_metadata('42')
        self.assertIsNone(series.cover_url)
        self.transport.get.reset_mock()
        self.assertIsNone(self.adapter.retrieve_cover(series))
        self.transport.get.assert_not_called()

    def test_cover_download_is_capped_at_ten_megabytes_whatever_budget_remains(self):
        series = Series('s', 'S', cover_url='https://us-a.tapas.io/sa/cover.jpg')
        self.transport.get.return_value = b'cover'
        for requested, sent in ((None, 10 * 1024 * 1024), (50 * 1024 * 1024, 10 * 1024 * 1024), (100, 100)):
            self.assertEqual(self.adapter.retrieve_cover(series, max_bytes=requested), b'cover')
            self.transport.get.assert_called_with('https://us-a.tapas.io/sa/cover.jpg', max_bytes=sent)

    def test_episode_url_is_built_only_from_a_plain_numeric_identity(self):
        self.assertEqual(self.adapter.episode_url('81'), 'https://tapas.io/episode/81')
        for key in ('0', '081', 'abc', '81/unlock', '../81', '', 81, None, '1' * 21):
            with self.subTest(key=key), self.assertRaises(MetadataError):
                self.adapter.episode_url(key)

    def test_series_resolution_uses_page_attribute_only_when_unambiguous_and_checks_returned_identity(self):
        series = envelope({'id': 42, 'title': 'S', 'url': 'series-slug'})
        self.transport.get.side_effect = [b'<div data-series-id="42"></div>', series]
        self.assertEqual(self.adapter.resolve_series('series-slug')['id'], 42)
        for page in (b'<div data-series-id="42"></div><p data-series-id="43"></p>', b'<p>no id</p>',
                     b'<script>var seriesId=0;</script>'):
            self.transport.get.reset_mock()
            self.transport.get.side_effect = [page, series]
            with self.subTest(page=page), self.assertRaises(ArchiveError):
                self.adapter.resolve_series('series-slug')
            self.assertEqual(self.transport.get.call_count, 1)
        self.transport.get.reset_mock()
        for key in ('0', 'a b', '', None, 'x' * 101, '../42'):
            with self.subTest(key=key), self.assertRaises(ArchiveError):
                self.adapter.resolve_series(key)
        self.transport.get.assert_not_called()
        self.transport.get.side_effect = [envelope({'id': 43, 'title': 'S', 'url': 'series-slug'})]
        with self.assertRaisesRegex(ArchiveError, 'inconsistent'):
            self.adapter.resolve_series('42')

    def test_episode_enumeration_refuses_bad_series_ids_structures_and_episode_rows(self):
        for platform_id in (0, True, '42', None):
            with self.subTest(platform_id=platform_id), self.assertRaises(ArchiveError):
                self.adapter.enumerate_episodes(platform_id)
        self.transport.get.assert_not_called()
        def page(episodes, total=1):
            return envelope({'pagination': {'page': 2, 'has_next': False, 'total': total}, 'episodes': episodes})
        for body in (envelope({'episodes': [self.info]}), envelope({'pagination': {'total': 1}, 'episodes': None}),
                     envelope({'pagination': [], 'episodes': []}), page([], total=-1),
                     page([self.info | {'id': 0}]), page([self.info | {'id': '81'}]),
                     page([self.info | {'id': True}]), page(['row']), page([{'title': 'no id'}])):
            self.transport.get.side_effect = [body]
            with self.subTest(body=body[:60]), self.assertRaises(ArchiveError):
                self.adapter.enumerate_episodes(42)

    def test_enumeration_stops_after_500_pages_even_if_the_server_keeps_offering_more(self):
        self.transport.get.side_effect = [envelope({'pagination': {'page': n + 1, 'has_next': True, 'total': 501},
                                                    'episodes': [self.info | {'id': n, 'scene': n}]})
                                          for n in range(1, 501)]
        with self.assertRaisesRegex(ArchiveError, '500-page limit'):
            self.adapter.enumerate_episodes(42)
        self.assertEqual(self.transport.get.call_count, 500)

    def test_capture_refuses_other_urls_and_unverified_order_before_requesting_the_reader(self):
        for url in ('https://tapas.io/series/42/info', 'http://tapas.io/episode/81', 'https://tapas.io/episode/x', None):
            with self.subTest(url=url), self.assertRaises(ArchiveError):
                self.adapter.capture(url)
        self.transport.get.assert_not_called()
        for scene in (None, 0, '1', True):
            self.transport.reset_mock()
            self.responses(info=self.info | {'scene': scene})
            with self.subTest(scene=scene), self.assertRaises(UnsupportedContent) as error:
                self.adapter.capture('https://tapas.io/episode/81')
            self.assertEqual(error.exception.code, 'order_unverified')
            self.assertEqual(self.transport.get.call_count, 2)
        self.responses(info=self.info | {'scene': None})
        self.assertEqual(self.adapter.capture('https://tapas.io/episode/81', order=7).episodes[0].order, 7)

    def test_reader_answer_that_is_oversized_undecodable_or_without_series_identity_is_not_archived(self):
        for html, message in ((b'x' * (8 * 1024 * 1024 + 1), 'exceeds'),
                              (self.html.replace(b'PLACEHOLDER', b'\xff'), 'unsupported encoding'),
                              (self.html.replace(b'var seriesId = 42;', b''), 'numeric series ID')):
            self.transport.reset_mock()
            self.responses(html=html)
            with self.subTest(message=message), self.assertRaisesRegex(ArchiveError, message):
                self.adapter.capture('https://tapas.io/episode/81')
            self.assertEqual(self.transport.get.call_count, 3)
        self.transport.reset_mock()
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope(self.info), self.html,
            envelope({'id': 42, 'title': 'Series', 'url': 'bad slug', 'comic': True, 'book': False})]
        with self.assertRaisesRegex(ArchiveError, 'slug'):
            self.adapter.capture('https://tapas.io/episode/81')

    def test_refresh_accepts_rotated_signatures_but_refuses_a_changed_page_manifest(self):
        self.responses()
        manifest = self.adapter.capture('https://tapas.io/episode/81')
        self.assertIs(self.adapter.manifest(), manifest)
        episode = manifest.episodes[0]
        self.responses(html=self.html.replace(b'signature=TEST', b'signature=ROTATED'))
        renewed = self.adapter.refresh(episode)
        self.assertEqual([page.location for page in renewed.pages], [
            'https://us-a.tapas.io/pc/a/1.jpg?signature=ROTATED', 'https://us-a.tapas.io/pc/a/2.jpg?signature=ROTATED'])
        self.assertEqual((renewed.title, renewed.order), (episode.title, episode.order))
        second = b'<img class="content__img" data-src="https://us-a.tapas.io/pc/a/2.jpg?signature=TEST">'
        for html in (self.html.replace(b'/pc/a/2.jpg', b'/pc/a/3.jpg'), self.html.replace(second, b'')):
            self.responses(html=html)
            with self.assertRaisesRegex(ArchiveError, 'manifest changed'):
                self.adapter.refresh(episode)

    def test_threaded_asset_requests_are_prepared_only_for_a_real_transport_and_tapas_hosts(self):
        page = Page(1, 'https://us-a.tapas.io/pc/a/1.jpg?signature=TEST', False)
        self.assertIsNone(self.adapter.asset_request(page))
        with self.assertRaises(ArchiveError):
            self.adapter.asset_request(Page(1, 'https://evil.example/a.jpg', False))


if __name__ == "__main__":
    unittest.main()
