import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from comic_archive.adapters.tapas_http import TapasHTTPAdapter, access_state
from comic_archive.models import ArchiveError, Page, Series
from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError, UnsupportedContent


def envelope(value):
    return json.dumps({"data": value, "error_details": None}).encode()


class TapasHTTPTests(unittest.TestCase):
    def reader_document_responses(self, info=None, html=None, reader_error=None):
        self.responses()
        normal = self.transport.get.side_effect
        fragment = (html if html is not None else self.html).decode().replace(
            '<div id="episode-81">', '<div id="episode-81" data-series-id="42">')
        def get(url, **options):
            if url.endswith('/episode/81/info'):
                raise self.header_failure(404)
            if url.endswith('/episode/81'):
                if reader_error is not None:
                    raise reader_error
                self.assertEqual(options, {'reader_json': True, 'max_bytes': 8 * 1024 * 1024})
                return envelope({'episode': self.info if info is None else info, 'html': fragment})
            return normal(url, **options)
        self.transport.get.side_effect = get

    def test_missing_info_uses_website_reader_document_without_duplicate_reader_or_info_requests(self):
        self.reader_document_responses()
        with self.assertLogs('comic_archive.download', level='INFO') as logs:
            result = self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(result.series.platform_id, '42')
        self.assertEqual((result.episodes[0].id, result.episodes[0].order, result.episodes[0].access), ('81', 1, 'tapas_free'))
        self.assertEqual(len(result.episodes[0].pages), 2)
        self.assertEqual(self.requested(), ['https://tapas.io/episode/81/info', 'https://tapas.io/episode/81',
                                          'https://tapas.io/series/42'])
        self.assertIn('fallback=website_reader_document', '\n'.join(logs.output))
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))

    def test_missing_reader_is_episode_local_but_auth_throttle_and_outages_remain_holds(self):
        for status in (404, 410, 401, 403, 429, 503):
            with self.subTest(status=status):
                self.transport.get.reset_mock()
                error = self.header_failure(status)
                self.reader_document_responses(reader_error=error)
                with self.assertRaises(ArchiveError) as caught:
                    self.adapter.capture('https://tapas.io/episode/81')
                self.assertEqual(isinstance(caught.exception, AccessDenied), status in (404, 410))
                self.assertEqual(caught.exception.source_http_status, status)
                self.assertEqual(self.requested(), ['https://tapas.io/episode/81/info', 'https://tapas.io/episode/81'])

    def test_missing_info_does_not_accept_wrong_episode_series_or_partial_comic_document(self):
        cases = [(self.info | {'id': 82}, self.html),
                 (self.info, self.html.replace(b'var seriesId = 42', b'var seriesId = 43')),
                 (self.info, b'<div id="episode-82" data-series-id="42"><img class="content__img" src="https://us-a.tapas.io/pc/1.jpg"></div>'),
                 (self.info, self.html.replace(b'</div>', b'<span data-series-id="43"></span></div>'))]
        for info, html in cases:
            with self.subTest(info_id=info['id'], html=html[:20]):
                self.reader_document_responses(info=info, html=html)
                with self.assertRaises(ArchiveError):
                    self.adapter.capture('https://tapas.io/episode/81')

    def test_reader_document_preserves_novel_type_and_expiration_without_positive_flags(self):
        info = self.info | {'book': True, 'free': False, 'unlocked': False, 'closing_date': '2099-01-01T00:00:00Z'}
        html = b'<div id="episode-81"><div id="epub-81" class="ep-epub-content"><p>A complete chapter.</p></div></div>'
        self.reader_document_responses(info=info, html=html)
        normal = self.transport.get.side_effect
        self.transport.get.side_effect = lambda url, **options: (
            envelope({'id': 42, 'title': 'Series', 'url': 'series-slug', 'comic': False, 'book': True})
            if url.endswith('/series/42') else normal(url, **options))
        result = self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(result.episodes[0].content_type, 'novel')
        self.assertEqual(result.episodes[0].access, 'tapas_readable')
        self.assertIsNotNone(result.episodes[0].access_expires_at)
        self.assertIn('complete chapter', result.episodes[0].text_html)

    def test_free_only_access_check_can_use_missing_info_reader_metadata(self):
        self.reader_document_responses()
        result = self.adapter.episode_access('https://tapas.io/episode/81')
        self.assertEqual(result['access_state'], 'tapas_free')
        self.assertEqual(self.requested(), ['https://tapas.io/who-am-i', 'https://tapas.io/episode/81/info',
                                          'https://tapas.io/episode/81'])

    def test_existing_info_with_missing_reader_is_also_episode_local(self):
        self.responses()
        normal = self.transport.get.side_effect
        self.transport.get.side_effect = lambda url, **options: (
            (_ for _ in ()).throw(self.header_failure(404)) if url.endswith('/episode/81') else normal(url, **options))
        with self.assertRaises(AccessDenied):
            self.adapter.capture('https://tapas.io/episode/81')

    def test_reader_document_still_works_when_optional_series_header_is_missing(self):
        self.reader_document_responses()
        normal = self.transport.get.side_effect
        self.transport.get.side_effect = lambda url, **options: (
            (_ for _ in ()).throw(self.header_failure(404)) if url.endswith('/series/42') else normal(url, **options))
        self.adapter.seed_series('42', 'Observed title')
        result = self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(result.series.platform_id, '42')
        self.assertEqual(len(result.episodes[0].pages), 2)

    def test_reader_document_refuses_error_envelopes_and_private_details_are_not_logged(self):
        for payload in ([], {'data': {}}, {'code': 403, 'data': {'episode': self.info, 'html': ''}},
                        {'data': {'episode': self.info, 'html': ''}, 'errorDetails': 'PRIVATE_RESPONSE'}):
            self.reader_document_responses()
            normal = self.transport.get.side_effect
            self.transport.get.side_effect = lambda url, **options: (
                json.dumps(payload).encode() if url.endswith('/episode/81') else normal(url, **options))
            with self.assertRaises(MetadataError) as caught:
                self.adapter.capture('https://tapas.io/episode/81')
            self.assertNotIn('PRIVATE', str(caught.exception))


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
            with self.assertRaises(EpisodeCountMismatch) as caught:
                self.adapter.inspect_series('42', strict_order=False)
            partial = caught.exception.partial_inspection
            self.assertFalse(partial['complete'])
            self.assertTrue(partial['authenticated'])
            self.assertEqual(partial['source_series_id'], '42')
            self.assertEqual([row['id'] for row in partial['episodes']], ['81', '82'])
            self.assertEqual([row['order'] for row in partial['episodes']], [1, 2])
            self.assertEqual(partial['episodes'][0]['access_state'],
                             'unavailable_or_unverified' if change else 'tapas_free')
            self.assertNotIn('PRIVATE', json.dumps(caught.exception.__dict__))
            self.transport.get.assert_called_with('https://tapas.io/series/42/episodes?page=1&max_limit=20')

    def test_advisory_scheduling_and_expiry_do_not_veto_complete_reader(self):
        for change in ({'scheduled': True}, {'closing_date': 'PRIVATE_VALUE'}):
            self.transport.reset_mock()
            self.responses(info=self.info | change)
            self.assertEqual(len(self.adapter.capture('https://tapas.io/episode/81').episodes[0].pages), 2)
            self.assertEqual(self.transport.get.call_count, 3)

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
        result = self.adapter.inspect_series('42')
        self.assertEqual(result['episodes'][0]['access_state'], 'unavailable_or_unverified')
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
        """Tapas as the adapter reads it, answered by address, so each test says which requests were made."""
        answers = {"https://tapas.io/who-am-i": envelope({"user": user if user is not None else {"id": 123, "uname": "PRIVATE_ACCOUNT"}}),
                   "https://tapas.io/episode/81/info": envelope(info if info is not None else self.info),
                   "https://tapas.io/episode/81": html if html is not None else self.html,
                   "https://tapas.io/series/42": envelope({"id": 42, "title": "Series", "url": "series-slug", "comic": True, "book": False})}
        self.transport.get.side_effect = lambda url, **options: answers[url]

    def requested(self):
        return [call.args[0] for call in self.transport.get.call_args_list]

    def header_failure(self, status):
        error = MetadataError('Unavailable metadata')
        error.source_http_status = status
        return error

    def missing_header_responses(self, status=404, episodes=None):
        self.responses()
        normal = self.transport.get.side_effect
        def get(url, **options):
            if url == 'https://tapas.io/series/42':
                raise self.header_failure(status)
            if '/series/42/episodes?' in url:
                rows = [self.info] if episodes is None else episodes
                return envelope({'episodes': rows, 'pagination': {'page': 1, 'total': len(rows), 'has_next': False}})
            return normal(url, **options)
        self.transport.get.side_effect = get

    def test_account_seed_allows_missing_header_scan_capture_and_metadata_without_repeated_404(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.adapter = TapasHTTPAdapter(transport=self.transport)
                self.transport.reset_mock()
                self.missing_header_responses(status)
                self.adapter.seed_series('42', 'Observed title')
                with self.assertLogs('comic_archive.download', level='INFO') as logs:
                    scanned = self.adapter.inspect_series('42', strict_order=False)
                    manifest = self.adapter.capture('https://tapas.io/episode/81')
                    with self.assertRaises(MetadataError):
                        self.adapter.series_metadata('42')
                self.assertTrue(scanned['complete'])
                self.assertEqual(scanned['episodes'][0]['id'], '81')
                self.assertEqual((manifest.series.platform_id, manifest.series.title), ('42', 'Observed title'))
                self.assertEqual(len(manifest.episodes[0].pages), 2)
                self.assertIsNone(manifest.series.source_url)
                self.assertEqual(self.requested().count('https://tapas.io/series/42'), 1)
                self.assertIn('stage=series_header', ' '.join(logs.output))
                self.assertIn(f'http_status={status}', ' '.join(logs.output))
                self.assertNotIn('Observed title', ' '.join(logs.output))
                self.assertNotIn('PRIVATE', ' '.join(logs.output))

    def test_missing_header_without_seed_and_non_missing_errors_never_fall_back(self):
        self.missing_header_responses()
        with self.assertRaises(MetadataError):
            self.adapter.inspect_series('42')
        self.assertFalse(any('/episodes?' in url for url in self.requested()))
        for status in (401, 403, 429, 500):
            self.transport.reset_mock()
            self.missing_header_responses(status)
            self.adapter.seed_series('42', 'Observed')
            with self.assertRaises(MetadataError):
                self.adapter.inspect_series('42')
            self.assertFalse(any('/episodes?' in url for url in self.requested()))

    def test_seed_does_not_hide_malformed_or_wrong_identity_metadata(self):
        for response in (b'not json', envelope({'id': 43, 'title': 'Other'})):
            self.transport.get.side_effect = [envelope({'user': {'id': 123}}), response]
            self.adapter.seed_series('42', 'Observed')
            with self.assertRaises(ArchiveError):
                self.adapter.inspect_series('42')
        for key, title in (('slug', 'Title'), ('42', ''), ('42', 'x' * 501), (42, 'Title')):
            with self.assertRaises(MetadataError):
                self.adapter.seed_series(key, title)

    def test_missing_header_keeps_reader_identity_and_completeness_checks(self):
        self.missing_header_responses()
        self.adapter.seed_series('42', 'Observed')
        with self.assertRaises(MetadataError):
            self.adapter.capture('https://tapas.io/episode/81', series_id='invented-slug')
        self.responses(html=self.html.replace(b'seriesId = 42', b'seriesId = 43'))
        normal = self.transport.get.side_effect
        self.transport.get.side_effect = lambda url, **options: (
            envelope({'id': 43, 'title': 'Other', 'url': 'other', 'comic': True, 'book': False})
            if url == 'https://tapas.io/series/43' else normal(url, **options))
        with self.assertRaises(ArchiveError):
            self.adapter.capture('https://tapas.io/episode/81', series_id='42')
        self.missing_header_responses()
        normal = self.transport.get.side_effect
        self.transport.get.side_effect = lambda url, **options: (
            b'<script>var seriesId=42;</script><div id="episode-81"></div>'
            if url == 'https://tapas.io/episode/81' else normal(url, **options))
        with self.assertRaises(UnsupportedContent):
            self.adapter.capture('https://tapas.io/episode/81')

    def test_missing_header_does_not_turn_a_partial_episode_list_into_a_complete_scan(self):
        from comic_archive.adapters.pagination import EpisodeCountMismatch
        self.adapter.seed_series('42', 'Observed')
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}), self.header_failure(404),
            envelope({'episodes': [self.info], 'pagination': {'page': 1, 'total': 2, 'has_next': False}})]
        with self.assertRaises(EpisodeCountMismatch) as caught:
            self.adapter.inspect_series('42', strict_order=False)
        self.assertFalse(caught.exception.partial_inspection['complete'])
        self.assertEqual(caught.exception.partial_inspection['episodes'][0]['id'], '81')

    def test_header_and_episode_list_missing_is_typed_as_held_series(self):
        self.adapter.seed_series('42', 'Observed')
        self.transport.get.side_effect = [envelope({'user': {'id': 123}}),
                                         self.header_failure(404), self.header_failure(410)]
        with self.assertRaises(MetadataError) as caught:
            self.adapter.inspect_series('42')
        self.assertEqual(caught.exception.source_series_id, '42')
        self.assertEqual(caught.exception.source_http_status, 410)
        self.assertEqual(caught.exception.source_route, '/series/42/episodes')

    def test_refresh_retains_previously_verified_slug_when_header_disappears(self):
        self.responses()
        self.adapter.seed_series('42', 'Observed')
        episode = self.adapter.capture('https://tapas.io/episode/81').episodes[0]
        self.missing_header_responses()
        renewed = self.adapter.refresh(episode)
        self.assertEqual(renewed.series_id, episode.series_id)
        self.assertEqual([page.location for page in renewed.pages], [page.location for page in episode.pages])

    def test_explicit_temporary_unlock_captures_expiry_after_normal_account_and_reader_checks(self):
        from datetime import datetime, timezone
        now=datetime(2026,10,8,18,tzinfo=timezone.utc)
        self.responses(info=self.info|{'free':False,'unlocked':True,'closing_date':'2026-10-08T20:00:00Z'})
        with patch('comic_archive.expiry.now',return_value=now):
            manifest=self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(manifest.episodes[0].access,'tapas_limited')
        self.assertEqual(manifest.episodes[0].access_expires_at,'2026-10-08T20:00:00+00:00')
        self.assertEqual(self.transport.get.call_count,3)

    def test_past_expiry_is_metadata_and_reader_still_decides(self):
        self.responses(info=self.info | {'closing_date': '2000-01-01T00:00:00Z'})
        self.assertEqual(len(self.adapter.capture('https://tapas.io/episode/81').episodes[0].pages), 2)
        self.assertEqual(self.transport.get.call_count, 3)

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
        # No sign-in check before each episode: the step checked it when it started.
        self.assertEqual(self.requested(), [
            "https://tapas.io/episode/81/info", "https://tapas.io/episode/81", "https://tapas.io/series/42"])
        self.assertNotIn("PRIVATE_ACCOUNT", repr(result))

    def test_only_reader_html_403_after_fresh_access_gate_is_retry_eligible(self):
        for kind,eligible in (('html',True),('challenge',False),('json',False),(None,False)):
            with self.subTest(kind=kind):
                failure=ArchiveError('PRIVATE response')
                failure.source_http_status=403; failure.source_response_kind=kind
                self.transport.get.side_effect=[envelope(self.info),failure]
                with self.assertRaises(ArchiveError) as caught:self.adapter.capture('https://tapas.io/episode/81')
                self.assertEqual(getattr(caught.exception,'source_retry_episode_id',None),'81' if eligible else None)
        failure=ArchiveError('PRIVATE metadata response');failure.source_http_status=403;failure.source_response_kind='html'
        self.transport.get.side_effect=[failure]
        with self.assertRaises(ArchiveError) as caught:self.adapter.capture('https://tapas.io/episode/81')
        self.assertFalse(hasattr(caught.exception,'source_retry_episode_id'))

    def test_contradictory_flags_cannot_veto_complete_authenticated_reader(self):
        for updates in ({'free': False, 'unlocked': False}, {'scheduled': True},
                        {'closing_date': 'expired'}, {'free': 'true', 'unlocked': 1}):
            with self.subTest(updates=updates):
                self.transport.reset_mock()
                self.responses(info=self.info | updates)
                episode = self.adapter.capture('https://tapas.io/episode/81').episodes[0]
                self.assertEqual(len(episode.pages), 2)
                self.assertEqual(self.transport.get.call_count, 3)

    def test_a_locked_reader_with_no_pages_sets_the_episode_aside_only_while_the_sign_in_holds(self):
        empty = b'<script>var seriesId = 42;</script><div id="episode-81"><article></article></div>'
        locked = self.info | {'free': False, 'unlocked': False, 'must_pay': True}
        self.responses(info=locked, html=empty)
        with self.assertLogs('comic_archive.download', level='INFO') as captured, self.assertRaises(AccessDenied):
            self.adapter.capture('https://tapas.io/episode/81')
        self.assertIn('reason=reader_locked', '\n'.join(captured.output))
        self.assertEqual(self.requested()[-1], 'https://tapas.io/who-am-i')
        # A sign-in that lapsed looks the same from the reader: that holds for the person, it is not a refusal.
        self.responses(info=locked, html=empty, user={})
        with self.assertRaises(AuthenticationError):
            self.adapter.capture('https://tapas.io/episode/81')
        # An episode the account may read whose reader comes back empty is still a reader that changed.
        for readable in (self.info, self.info | {'free': False, 'unlocked': True, 'must_pay': True}):
            self.responses(info=readable, html=empty)
            with self.subTest(readable=readable), self.assertRaises(UnsupportedContent) as caught:
                self.adapter.capture('https://tapas.io/episode/81')
            self.assertEqual(caught.exception.code, 'reader_manifest_incomplete')
        # Pages that cannot all be placed are never taken for a locked episode.
        partial = self.html.replace(b'data-src="https://us-a.tapas.io/pc/a/2.jpg?signature=TEST"', b'data-src="https://example.com/x.jpg"')
        self.responses(info=locked, html=partial)
        with self.assertRaises(UnsupportedContent):
            self.adapter.capture('https://tapas.io/episode/81')

    def test_bulk_hints_require_positive_booleans_without_claiming_denial(self):
        self.assertEqual(access_state(self.info | {'free': False, 'unlocked': True}), 'tapas_unlocked')
        self.assertEqual(access_state(self.info | {'free': False, 'unlocked': 'true'}), 'unavailable_or_unverified')

    def test_bulk_discovery_avoids_probing_every_unowned_paid_episode(self):
        paid_only = self.info | {'free': False, 'must_pay': True}
        self.assertEqual(access_state(paid_only | {'unlocked': True}), 'tapas_unlocked')
        self.assertEqual(access_state(paid_only | {'unlocked': False}), 'unavailable_or_unverified')

    def test_session_identity_is_checked_at_step_start_and_episode_identity_at_capture(self):
        for user in ({}, {'id': 0}, {'id': True}, {'id': '123'}):
            self.responses(user=user)
            with self.assertRaises(AuthenticationError):
                self.adapter.account_identity()
        self.transport.reset_mock()
        self.responses(info=self.info | {'id': 82})
        with self.assertRaises(ArchiveError):
            self.adapter.capture('https://tapas.io/episode/81')
        self.assertEqual(self.transport.get.call_count, 1)

    def test_expected_account_change_stops_before_episode_metadata_or_reader(self):
        self.adapter.expected_account_id = "999"
        self.responses()
        # Checked as each step starts, before any episode.
        with self.assertRaises(AuthenticationError):
            self.adapter.account_identity()
        self.assertEqual(self.requested(), ["https://tapas.io/who-am-i"])

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
            self.assertEqual(self.transport.get.call_count, 2)

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
        renewed = self.adapter.refresh(episode)
        self.assertEqual(renewed.pages, episode.pages)
        self.assertEqual(renewed.access, 'tapas_readable')
        self.assertEqual(self.transport.get.call_count, 3)

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
        ):
            self.transport.get.side_effect = [envelope(first), envelope(second)]
            with self.assertRaises(ArchiveError):
                self.adapter.enumerate_episodes(42)

    def test_a_list_with_no_usable_next_page_ends_there_for_that_series_alone(self):
        from comic_archive.adapters.pagination import EpisodeCountMismatch
        second = self.info | {"id": 82, "scene": 2}
        # More is promised, but the page is empty, its number does not advance, or more is not stated at all.
        for pagination, items, reason in (
                ({"page": 2, "has_next": True, "total": 3}, [], 'empty_page'),
                ({"page": 1, "has_next": True, "total": 3}, [self.info, second], 'page_number_not_advancing'),
                ({"page": 'PRIVATE', "has_next": True, "total": 3}, [self.info, second], 'page_number_not_advancing'),
                ({"page": 2, "total": 3}, [self.info, second], 'more_pages_unstated')):
            pages = [envelope({"pagination": {"page": 2, "has_next": True, "total": 3}, "episodes": [self.info, second]}),
                     envelope({"pagination": pagination, "episodes": items})] if not items else [
                     envelope({"pagination": pagination, "episodes": items})]
            self.transport.get.side_effect = [envelope({'user': {'id': 123}}), envelope({'id': 42, 'title': 'S'}), *pages]
            with self.assertLogs('comic_archive.download', 'INFO') as logged, \
                    self.assertRaises(EpisodeCountMismatch) as caught:
                self.adapter.inspect_series('42', strict_order=False)
            self.assertEqual((caught.exception.series_id, caught.exception.observed_count, caught.exception.reported_count),
                             ('42', 2, 3))
            partial = caught.exception.partial_inspection
            self.assertEqual((partial['complete'], [row['id'] for row in partial['episodes']]), (False, ['81', '82']))
            self.assertEqual(logged.output, [
                'INFO:comic_archive.download:series list ended early: series=42 listed_episodes=2 '
                f'reported_episodes=3 reason={reason} page={len(pages)}'])
            self.assertNotIn('PRIVATE', ''.join(logged.output) + json.dumps(caught.exception.__dict__))

    def test_a_list_that_reaches_its_count_is_whole_even_when_more_is_promised(self):
        self.transport.get.side_effect = [
            envelope({"pagination": {"page": 2, "has_next": True, "total": 2}, "episodes": [self.info]}),
            envelope({"pagination": {"page": 2, "has_next": True, "total": 2},
                      "episodes": [self.info | {"id": 82, "scene": 2}]})]
        with self.assertLogs('comic_archive.download', 'INFO'):
            result = self.adapter.enumerate_episodes(42)
        self.assertEqual([item["id"] for item in result], [81, 82])
        self.assertEqual(self.transport.get.call_count, 2)

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
        items=[self.info|{'id':81,'scene':1},self.info|{'id':82,'scene':2,'free':False,'must_pay':True},
               self.info|{'id':83,'scene':3,'book':True},self.info|{'id':84,'scene':4,'scheduled':True},
               self.info|{'id':85,'scene':5,'closing_date':'EXPIRY_PRIVATE'},
               self.info|{'id':86,'scene':6,'free':False,'unlocked':True,'must_pay':True}]
        self.transport.get.side_effect=[envelope({'user':{'id':123}}),envelope({'id':42,'title':'S'}),
            envelope({'pagination':{'page':2,'has_next':False,'total':6},'episodes':items})]
        result=self.adapter.inspect_series('42')
        self.assertEqual([row['authorization_reason'] for row in result['episodes']],
                         [None] * 6)
        self.assertEqual([row['access_state'] for row in result['episodes']],
                         ['tapas_free','unavailable_or_unverified','tapas_free','tapas_free','tapas_free',
                          'tapas_unlocked'])
        self.assertEqual(self.transport.get.call_count,3)
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_missing_advisory_flags_do_not_override_a_positive_bulk_hint(self):
        for key in ('scheduled', 'closing_date'):
            info = dict(self.info)
            del info[key]
            self.assertEqual(access_state(info), 'tapas_free')

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
        self.assertEqual(access_state(locked), 'unavailable_or_unverified')
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
        self.assertEqual(access_state(episode['authorization'] | {'unlocked': False}), 'unavailable_or_unverified')

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
        for url in ('https://tapas.io/series/42/info', 'https://tapas.io/episode/abc',
                    'https://tapas.io/episode/81/unlock', 'https://example.com/episode/81',
                    'http://tapas.io/episode/81', None):
            with self.subTest(url=url), self.assertRaises(ArchiveError):
                self.adapter.episode_access(url)
        self.transport.get.assert_not_called()
        self.responses(info=self.info | {'id': 82})
        with self.assertRaises(MetadataError):
            self.adapter.episode_access('https://tapas.io/episode/81')
        self.assertEqual(self.transport.get.call_count, 2)

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
        self.assertEqual(self.transport.get.call_count, 2)

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
            self.assertEqual(self.transport.get.call_count, 1)
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
            self.assertEqual(self.transport.get.call_count, 2)
        self.transport.reset_mock()
        self.transport.get.side_effect = [envelope(self.info), self.html,
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
