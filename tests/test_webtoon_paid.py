"""Synthetic mobile schemas; no account records, episode text or real images."""
import base64
from copy import deepcopy
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from comic_archive import sites
from comic_archive.adapters.webtoon import WebtoonAdapter
from comic_archive.android_readers import reader as android_reader
from comic_archive.archive import verify
from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError, RunLimit
from comic_archive.inventory import Inventory
from comic_archive.jobs import DownloadBudget, sync
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport, asset_url
from comic_archive.webtoon_gateway import BASE, CLOCK, PUBLIC_GATEWAY_KEY, PAGE_SIZE, signed_url
from test_archive import png
from test_webtoon import HOME, Response, series_pages, subscriptions_page, subscription, viewer_link, who
from test_webtoon import LIST_API, listed


def envelope(result):
    return json.dumps({'message': {'type': 'response', 'service': 'com.naver.webtoon', 'result': result}}).encode()


def grant(number=9, *, title_no=7, policy='PAID_POLICY', has_right=True, infinite=True):
    return {'titleNo': title_no, 'titleName': 'Sample Series', 'episodeNo': number,
            'episodeTitleName': f'Episode {number}', 'contentType': 'WEBTOON',
            'rightInfo': {'hasRight': has_right, 'infinite': infinite, 'productPolicy': policy,
                          'rewardAd': False, 'timeDeal': False}}


def title(number=7):
    return {'titleNo': number, 'title': 'Sample Series', 'contentType': 'WEBTOON', 'titlePurchase': False}


class MobileSite:
    def __init__(self):
        self.pages = series_pages()
        self.calls = []
        self.account = 'fixture-account'
        self.titles = [title()]
        self.rights = [grant(), grant(8, infinite=False), grant(7, has_right=False), grant(6, policy='FREE_POLICY')]
        self.api_errors = {}
        self.denied_episodes = {7, 99}
        self.version = 'one'
        self.fail_second_image_once = False
        self.manifest_changes = {}
        self.omit_manifest_fields = []
        self.repeat_list = False
        self.switch_account_on_manifest = False
        self.pictures = [png(41), png(42), png(43)]

    def stream(self, method, url, headers):
        self.calls.append((url, dict(headers)))
        assert method == 'GET'
        parts, query = urlsplit(url), parse_qs(urlsplit(url).query)
        if url == HOME + '/en/member/userInfo':
            return Response(who(self.account))
        if url == HOME + '/en/favorite':
            return Response(subscriptions_page(subscription(HOME + '/en/fantasy/sample-series/list?title_no=7', 'Sample Series')))
        if url == CLOCK:
            assert 'Cookie' not in headers and 'Authorization' not in headers
            return Response(b'1700000000000')
        if parts.hostname == 'global.apis.naver.com':
            route = parts.path.removeprefix('/lineWebtoon/webtoon/')
            assert 'Cookie' in headers and 'Authorization' not in headers
            assert headers['Cookie'] == 'NEO_SES=SESSION_A; NEO_CHK=CHECK_A'
            assert query['platform'] == ['APP_ANDROID'] and query['language'] == ['en']
            unsigned = url.rsplit('&msgpad=', 1)[0]
            expected = base64.b64encode(hmac.digest(PUBLIC_GATEWAY_KEY,
                unsigned.encode()[:255] + b'1700000000000', 'sha1')).decode()
            assert query['md'] == [expected]
            if route in self.api_errors:
                status, code = self.api_errors[route]
                return Response(json.dumps({'message': {'type': 'response', 'service': 'com.naver.webtoon',
                    'code': code, 'message': 'SYNTHETIC_PRIVATE_REFUSAL'}}).encode(), status)
            if route in ('myUnlockedTitleProductsV1.json', 'myUnlockedEpisodeProductsV1.json'):
                offset, size = int(query['offset'][0]), int(query['pageSize'][0])
                if self.repeat_list:
                    offset = 0
                field = 'unlockedTitleProducts' if route.startswith('myUnlockedTitle') else 'unlockedEpisodeProducts'
                rows = self.titles if field == 'unlockedTitleProducts' else self.rights
                if field == 'unlockedEpisodeProducts' and query['titleNo'] != ['7']:
                    rows = []
                return Response(envelope({field: deepcopy(rows[offset:offset + size])}))
            if route == 'titleHomeMainV3':
                assert query['titleNo'] == ['7']
                return Response(envelope({'title': {'titleNo': 7, 'title': 'Sample Series',
                    'synopsis': 'Synthetic metadata.', 'authorList': [{'authorName': 'Fixture Author'}],
                    'posterThumbnailUrl': '/cover.png?type=portrait'}, 'episodeMeta': {
                    'titleHomePcWebUrl': HOME + '/en/fantasy/sample-series/list?title_no=7'}}))
            if route == 'episodeInfoWithLogin.json':
                number = int(query['episodeNo'][0])
                assert query['titleNo'] == ['7'] and query['v'] == ['4']
                if number in self.denied_episodes:
                    return Response(json.dumps({'message': {'type': 'response', 'service': 'com.naver.webtoon', 'code': 1002}}).encode())
                row = next((row for row in self.rights if row['episodeNo'] == number), {})
                paid = (row.get('rightInfo') or {}).get('productPolicy') == 'PAID_POLICY'
                images = [{'sortOrder': i, 'cutId': i, 'width': 1, 'height': 1, 'fileSize': len(data),
                    'url': (f'https://webtoons-static.pstatic.net/paid/notice.png?dt={self.version}' if i == 0
                            else f'/paid/{i}.png?type=q90&delivery={self.version}')}
                    for i, data in enumerate(self.pictures)]
                info = {'episodeSeq': number, 'episodeTitle': f'Episode {number}',
                        'linkUrl': HOME + viewer_link('fantasy', 'sample-series', 7, number),
                        'serviceStatus': 'STANDBY', 'titleServiceStatus': 'SERVICE',
                        'productPolicy': 'PAID_POLICY' if paid else 'FREE_POLICY', 'product': True,
                        'count': len(images), 'imageInfo': images}
                info.update(deepcopy(self.manifest_changes))
                for name in self.omit_manifest_fields:
                    info.pop(name, None)
                if self.switch_account_on_manifest:
                    self.account = 'another-fixture-account'
                return Response(envelope({'episodeInfo': info}))
            raise AssertionError('Unexpected synthetic mobile route')
        if parts.hostname in {'webtoon-phinf.pstatic.net', 'webtoons-static.pstatic.net'}:
            assert 'Cookie' not in headers and 'Authorization' not in headers
            if parts.path == '/paid/notice.png':
                return Response(self.pictures[0])
            if parts.path in ('/paid/1.png', '/paid/2.png'):
                if self.fail_second_image_once and parts.path == '/paid/1.png':
                    self.fail_second_image_once = False
                    self.version = 'two'
                    return Response(b'SYNTHETIC_EXPIRED_ASSET', 403)
                assert query == {'type': ['q90'], 'delivery': [self.version]}
                return Response(self.pictures[int(parts.path.rsplit('/', 1)[1][0])])
            if parts.path == '/cover.png':
                return Response(png(44))
        if url in self.pages:
            return Response(self.pages[url])
        raise AssertionError('Unexpected synthetic website or image request')

    def close(self):
        pass

    def asset_calls(self):
        return [(u, h) for u, h in self.calls if '/paid/' in urlsplit(u).path]

    def content_calls(self):
        return [(u, h) for u, h in self.calls if 'episodeInfoWithLogin.json' in u]


def cookies(session='SESSION_A'):
    return [{'domain': '.webtoons.com', 'name': name, 'value': value}
            for name, value in [('NEO_SES', session), ('NEO_CHK', 'CHECK_A'), ('analytics', 'NOT_FOR_GATEWAY')]]


def adapter(site=None, **options):
    site = site or MobileSite()
    transport = Transport(cookies(), client=site, site=sites.named('webtoon'), **options)
    return WebtoonAdapter(transport=transport), site


@patch('comic_archive.transport.Transport._wait', lambda self, seconds: self._check_budget())
class PaidWebtoonTests(unittest.TestCase):
    def test_unlocked_titles_are_an_independent_account_list(self):
        reader, site = adapter()
        subscribed = reader.inspect_library()
        unlocked = reader.inspect_library(category='unlocked')
        self.assertEqual(subscribed['scope'], 'subscribed')
        self.assertEqual(unlocked, {'authenticated': True, 'membership_verified': True, 'scope': 'unlocked',
            'complete': True, 'boundary_observed': True, 'series': [{'source_series_id': '7', 'title': 'Sample Series'}]})
        self.assertEqual(site.content_calls(), [])
        self.assertEqual(site.asset_calls(), [])

    def test_pagination_observes_a_real_boundary_and_rejects_repeated_entries(self):
        reader, site = adapter()
        site.titles = [title(i) for i in range(1, PAGE_SIZE + 2)]
        result = reader.inspect_library(category='unlocked')
        self.assertEqual(len(result['series']), PAGE_SIZE + 1)
        queries = [parse_qs(urlsplit(url).query) for url, _ in site.calls if 'myUnlockedTitleProducts' in url]
        self.assertEqual([q['offset'] for q in queries], [['0'], [str(PAGE_SIZE)]])
        site.repeat_list = True
        with self.assertRaisesRegex(MetadataError, 'repeated'):
            reader.inspect_library(category='unlocked')

    def test_discovery_publishes_completed_pages_before_a_later_pagination_failure(self):
        reader, site = adapter()
        site.titles = [title(i) for i in range(1, PAGE_SIZE + 2)]
        site.repeat_list = True
        checkpoints = []
        with self.assertRaises(MetadataError):
            reader.inspect_library(category='unlocked', on_page=checkpoints.append)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(len(checkpoints[0]['series']), PAGE_SIZE)
        self.assertFalse(checkpoints[0]['complete'])
        self.assertFalse(checkpoints[0]['boundary_observed'])
        self.assertIsNone(reader._mobile_titles_snapshot)

    def test_an_account_without_mobile_credentials_cannot_claim_an_empty_unlocked_library(self):
        site = MobileSite()
        reader = WebtoonAdapter(transport=Transport(client=site, site=sites.named('webtoon')))
        with self.assertRaises(AuthenticationError):
            reader.inspect_library(category='unlocked')
        self.assertEqual(site.content_calls(), [])

    def test_paid_only_series_uses_provider_supplied_metadata_without_website_episodes(self):
        reader, site = adapter()
        api = LIST_API.format(kind='webtoon', title=7)
        site.pages[api] = listed('fantasy', 'sample-series', 7, [], 0)
        result = reader.inspect_series('7')
        self.assertEqual(result['title'], 'Sample Series')
        manifest = reader.capture_automatic(reader.episode_url('7000009'))
        self.assertEqual(manifest.series.slug, 'sample-series')

    def test_series_merges_website_and_account_candidates_without_reader_requests(self):
        reader, site = adapter()
        result = reader.inspect_series('7')
        states = {row['order']: (row['access_state'], row['authorization_reason']) for row in result['episodes']}
        self.assertEqual(states[9], ('webtoon_unlocked', None))
        self.assertEqual(states[8], ('webtoon_limited', None))
        self.assertEqual(states[7], ('webtoon_readable', None))
        self.assertEqual(states[6], ('webtoon_free', None))
        self.assertEqual([states[n][0] for n in (1, 2, 4)], ['webtoon_free'] * 3)
        self.assertEqual(result['accessible_episode_count'], 7)
        self.assertEqual(result['episode_count'], 7)
        self.assertEqual(site.content_calls(), [])

    def test_default_sync_reads_webtoon_lists_including_unlocked(self):
        with tempfile.TemporaryDirectory() as temporary, patch('builtins.print'):
            reader, site = adapter()
            result = sync(reader, Path(temporary) / 'Library', scan_only=True)
            self.assertEqual([row['category'] for row in result['refresh']['reading_lists']], ['subscribed', 'unlocked'])
            self.assertEqual(site.content_calls(), [])
            self.assertEqual(site.asset_calls(), [])

    def test_owned_episode_uses_the_authenticated_manifest_and_preserves_notice_and_queries(self):
        reader, site = adapter()
        url = reader.episode_url('7000009')
        manifest = reader.capture_automatic(url)
        episode = manifest.episodes[0]
        self.assertEqual((episode.id, episode.order, episode.access, len(episode.pages)), ('7000009', 9, 'webtoon_unlocked', 3))
        self.assertEqual((manifest.series.platform_id, manifest.series.slug), ('7', 'sample-series'))
        self.assertEqual(len(site.content_calls()), 1)
        self.assertEqual([reader.retrieve(page) for page in episode.pages], site.pictures)
        self.assertIn('?dt=one', episode.pages[0].location)
        self.assertIn('?type=q90&delivery=one', episode.pages[1].location)
        self.assertFalse(any('/episode-9/viewer' in u for u, _ in site.calls))

    def test_free_mobile_policy_may_omit_paid_policy_fields(self):
        reader, site = adapter()
        site.omit_manifest_fields = ['productPolicy']
        manifest = reader.capture_automatic(reader.episode_url('7000006'))
        self.assertEqual(manifest.episodes[0].access, 'webtoon_free')

    def test_product_entry_flag_does_not_determine_free_or_paid_account_access(self):
        for number, policy, expected in ((6, 'FREE_POLICY', 'webtoon_free'), (9, 'PAID_POLICY', 'webtoon_unlocked')):
            for product in (True, False):
                with self.subTest(policy=policy, product=product):
                    reader, site = adapter()
                    site.manifest_changes['product'] = product
                    manifest = reader.capture_automatic(reader.episode_url(f'7{number:06d}'))
                    self.assertEqual(manifest.episodes[0].access, expected)
                    self.assertEqual([reader.retrieve(page) for page in manifest.episodes[0].pages], site.pictures)

    def test_manifest_failure_logs_name_the_field_without_private_response_data(self):
        for change, reason in (({'episodeSeq': 8}, 'episode_identity'),
                               ({'episodeTitle': ''}, 'episode_title'),
                               ({'count': 4}, 'image_count')):
            reader, site = adapter()
            site.manifest_changes = change
            with self.assertLogs('comic_archive.download', level='INFO') as captured:
                with self.assertRaises(MetadataError) as caught:
                    reader.episode_url('7000009')
            self.assertEqual(caught.exception.source_validation_reason, reason)
            logs = '\n'.join(captured.output)
            self.assertIn('reason=' + reason, logs)
            self.assertIn('listed_images=3', logs)
            self.assertNotIn('PRIVATE', logs)
            self.assertNotIn('https://', logs)
            self.assertEqual(site.asset_calls(), [])

    def test_discovered_access_is_rechecked_after_it_is_revoked(self):
        reader, site = adapter()
        reader.inspect_series('7')
        site.rights[0]['rightInfo']['hasRight'] = False
        site.denied_episodes.add(9)
        with self.assertRaises(AccessDenied):
            reader.episode_url('7000009')
        self.assertEqual(len(site.content_calls()), 1)
        self.assertEqual(site.asset_calls(), [])

    def test_reader_supplied_temporary_content_is_accepted_and_actual_denials_stop(self):
        reader, site = adapter()
        self.assertEqual(len(reader.capture_automatic(reader.episode_url('7000008')).episodes[0].pages), 3)
        for number in (7, 99):
            reader, site = adapter()
            with self.subTest(number=number), self.assertRaises(AccessDenied):
                reader.episode_url(f'7{number:06d}')
            self.assertEqual(len(site.content_calls()), 1)
            self.assertEqual(site.asset_calls(), [])

    def test_stale_negative_rights_and_pricing_flags_do_not_veto_reader(self):
        reader, site = adapter()
        reader.inspect_series('7')
        site.rights[0]['rightInfo']['hasRight'] = False
        site.manifest_changes = {'product': 'unknown', 'productPolicy': 'UNKNOWN',
                                 'serviceStatus': 'REMOVED', 'titleServiceStatus': 'UNKNOWN'}
        episode = reader.capture_automatic(reader.episode_url('7000009')).episodes[0]
        self.assertEqual([reader.retrieve(page) for page in episode.pages], site.pictures)
        grants = [url for url, _ in site.calls if 'myUnlockedEpisodeProducts' in url]
        self.assertEqual(len(grants), 1)

    def test_login_expiry_and_account_switches_do_not_reuse_queued_access(self):
        for status, code, kind in ((401, 1001, AuthenticationError), (200, 1001, AuthenticationError),
                                   (200, 1002, MetadataError)):
            reader, site = adapter()
            site.api_errors['myUnlockedEpisodeProductsV1.json'] = status, code
            with self.subTest(status=status, code=code), self.assertRaises(kind):
                reader.episode_url('7000009')
            self.assertEqual(site.content_calls(), [])
        reader, site = adapter()
        site.switch_account_on_manifest = True
        with self.assertRaises(AuthenticationError):
            reader.episode_url('7000009')
        self.assertEqual(site.asset_calls(), [])

    def test_session_change_blocks_images_cached_publication_and_budget_checks(self):
        reader, site = adapter()
        manifest = reader.capture_automatic(reader.episode_url('7000009'))
        budget = DownloadBudget(reader)
        budget.check()
        reader.transport.cookies = (('.webtoons.com', 'NEO_SES=SESSION_B'),)
        for operation in (budget.check, budget.access_check, reader.check,
                          lambda: reader.retrieve(manifest.episodes[0].pages[0])):
            with self.assertRaises(AuthenticationError):
                operation()
        self.assertEqual(site.asset_calls(), [])

    def test_prefetched_manifest_is_single_use_and_short_lived(self):
        reader, site = adapter()
        with patch('comic_archive.adapters.webtoon.time.monotonic', return_value=10):
            url = reader.episode_url('7000009')
        with patch('comic_archive.adapters.webtoon.time.monotonic', return_value=41):
            reader.capture_automatic(url)
        self.assertEqual(len(site.content_calls()), 2)
        reader.capture_automatic(url)
        self.assertEqual(len(site.content_calls()), 3)

    def test_generic_http_refusal_and_unknown_gateway_code_hold_instead_of_marking_unowned(self):
        for status, code in ((403, 1002), (200, 9999)):
            reader, site = adapter()
            site.api_errors['episodeInfoWithLogin.json'] = status, code
            with self.subTest(status=status, code=code), self.assertRaises(ArchiveError) as caught:
                reader.episode_url('7000009')
            self.assertNotIsInstance(caught.exception, AccessDenied)
            self.assertEqual(len(site.content_calls()), 1)
            self.assertEqual(site.asset_calls(), [])

    def test_expected_job_account_is_checked_even_before_the_first_mobile_request(self):
        reader, site = adapter()
        reader.expected_account_id = 'another-fixture-account'
        with self.assertRaises(AuthenticationError):
            reader.episode_url('7000009')
        self.assertEqual(site.content_calls(), [])

    def test_incomplete_wrong_or_external_manifests_do_not_request_assets(self):
        examples = [{'count': 4}, {'count': 0}, {'episodeSeq': 10}, {'episodeTitle': ''},
                    {'linkUrl': HOME + viewer_link('fantasy', 'sample-series', 8, 9)},
                    {'linkUrl': 'https://example.com/en/fantasy/sample-series/episode-9/viewer?title_no=7&episode_no=9'},
                    {'imageInfo': []}, {'imageInfo': [None] * 3}]
        for change in examples:
            reader, site = adapter()
            site.manifest_changes = change
            with self.subTest(change=change), self.assertRaises((MetadataError, ArchiveError)):
                reader.episode_url('7000009')
            self.assertEqual(site.asset_calls(), [])

    def test_invalid_page_urls_and_ordering_are_refused_before_any_image_request(self):
        original = [{'sortOrder': i, 'cutId': i, 'width': 1, 'height': 1, 'fileSize': 1,
                     'url': f'/synthetic/{i}.png?delivery=PRIVATE'} for i in range(3)]
        variants = []
        for url in (None, '', ' https://webtoon-phinf.pstatic.net/image.png',
                    '//example.com/image.png', 'https://global.apis.naver.com/lineWebtoon/webtoon/episodeInfoWithLogin.json'):
            pages = deepcopy(original)
            pages[1]['url'] = url
            variants.append(pages)
        for field, value in (('sortOrder', 0), ('sortOrder', True), ('width', 0), ('cutId', False)):
            pages = deepcopy(original)
            pages[1][field] = value
            variants.append(pages)
        for pages in variants:
            reader, site = adapter()
            site.manifest_changes['imageInfo'] = pages
            with self.subTest(pages=pages), self.assertRaises(MetadataError):
                reader.episode_url('7000009')
            self.assertEqual(site.asset_calls(), [])

    def test_a_failed_mobile_refresh_does_not_replace_a_completed_archive(self):
        with tempfile.TemporaryDirectory() as temporary, patch('builtins.print'):
            root = Path(temporary) / 'Library'
            reader, site = adapter()
            with Inventory(root) as inventory:
                inventory.observe('7', 'Sample Series', [], 'fixture')
            result = sync(reader, root, refresh_lists=False, limit=1)
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            archive = next(root.rglob('*.cbz'))
            before = archive.read_bytes()
            fresh, revoked = adapter()
            revoked.rights[0]['rightInfo']['hasRight'] = False
            revoked.denied_episodes.add(9)
            with self.assertRaises(AccessDenied):
                fresh.episode_url('7000009')
            self.assertEqual(archive.read_bytes(), before)
            self.assertEqual(revoked.asset_calls(), [])

    def test_url_refresh_requires_current_rights_and_stable_content_but_allows_new_queries(self):
        reader, site = adapter()
        episode = reader.capture_automatic(reader.episode_url('7000009')).episodes[0]
        site.version = 'two'
        renewed = reader.refresh(episode)
        self.assertNotEqual(episode.pages[1].location, renewed.pages[1].location)
        self.assertIn('delivery=two', renewed.pages[1].location)
        site.rights[0]['rightInfo']['hasRight'] = False
        site.denied_episodes.add(9)
        with self.assertRaises(AccessDenied):
            reader.refresh(renewed)

    def test_actual_publication_pipeline_saves_paid_pages_and_reuses_a_bounded_partial_cache(self):
        with tempfile.TemporaryDirectory() as temporary, patch('builtins.print'):
            root = Path(temporary) / 'Library'
            reader, site = adapter()
            with Inventory(root) as inventory:
                inventory.observe('7', 'Sample Series', [], 'fixture')
            first = sync(reader, root, refresh_lists=False, limit=1, max_pages=1)
            self.assertEqual(first['status'], 'limited')
            self.assertEqual(first['archived_episode_ids'], [])
            self.assertEqual(len(site.asset_calls()), 1)
            resumed, next_site = adapter()
            result = sync(resumed, root, refresh_lists=False, limit=1)
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            self.assertEqual(verify(root, decode_images=True), [])
            archive = next(root.rglob('*.cbz'))
            with zipfile.ZipFile(archive) as packet:
                self.assertEqual(packet.namelist(), ['0001.png', '0002.png', '0003.png'])
                self.assertEqual([packet.read(name) for name in packet.namelist()], site.pictures)
            self.assertEqual(len(next_site.asset_calls()), 2)
            public = archive.with_suffix('.json').read_text()
            self.assertNotIn('SESSION_A', public)
            self.assertNotIn('delivery=', public)

    def test_asset_expiry_refreshes_manifest_without_discarding_completed_pages(self):
        with tempfile.TemporaryDirectory() as temporary, patch('builtins.print'):
            root = Path(temporary) / 'Library'
            reader, site = adapter()
            site.fail_second_image_once = True
            with Inventory(root) as inventory:
                inventory.observe('7', 'Sample Series', [], 'fixture')
            result = sync(reader, root, refresh_lists=False, limit=1)
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            self.assertEqual(verify(root), [])
            notice_calls = [u for u, _ in site.asset_calls() if 'notice.png' in u]
            self.assertEqual(len(notice_calls), 1)

    def test_clock_signing_and_mobile_metadata_do_not_enter_asset_metrics(self):
        reader, site = adapter()
        reader.inspect_library(category='unlocked')
        snapshot = reader.transport.metrics.snapshot()
        self.assertEqual(snapshot['requests'], 0)
        self.assertTrue(any(u.startswith(BASE) for u, _ in site.calls))


@patch('comic_archive.transport.Transport._wait', lambda self, seconds: self._check_budget())
class GatewayBoundaryTests(unittest.TestCase):
    def test_only_observed_read_routes_can_receive_the_session_pair(self):
        reader, site = adapter()
        reader.inspect_library(category='unlocked')
        for url in ('https://global.apis.naver.com/lineWebtoon/webtoon/purchase.json',
                    'https://global.apis.naver.com/other-service/profile',
                    'https://global.apis.naver.com/lineWebtoon/webtoon/../purchase.json',
                    'https://global.apis.naver.com/lineWebtoon/webtoon/myUnlockedTitleProductsV1.json#fragment',
                    'https://global.apis.naver.com:443/lineWebtoon/webtoon/myUnlockedTitleProductsV1.json',
                    'https://global.apis.naver.com.evil.test/lineWebtoon/webtoon/myUnlockedTitleProductsV1.json'):
            before = len(site.calls)
            with self.subTest(url=url), self.assertRaises(ArchiveError):
                reader.transport.get(url)
            self.assertEqual(len(site.calls), before)
        with self.assertRaises(ArchiveError):
            asset_url(BASE + 'myUnlockedTitleProductsV1.json', sites.named('webtoon'))

    def test_other_sites_and_ambiguous_sessions_cannot_delegate_credentials(self):
        site = MobileSite()
        transport = Transport(cookies(), client=site, site=sites.named('tappytoon'))
        with self.assertRaises(ArchiveError):
            transport.get(CLOCK)
        transport = Transport(cookies(), client=site, site=sites.named('webtoon'),
                              cookie_source=lambda url: 'NEO_SES=ONE; NEO_SES=TWO')
        with self.assertRaises(AuthenticationError):
            transport.has_webtoon_session()
        self.assertEqual(site.calls, [])

    def test_android_uses_the_existing_live_cookie_authority_without_a_second_session(self):
        site = MobileSite()
        class Store:
            def __init__(self):
                self.calls = []
            def headerFor(self, url):
                self.calls.append(url)
                return 'NEO_SES=SESSION_A; NEO_CHK=CHECK_A; analytics=NOT_FOR_GATEWAY'
            def apply(self, url, values):
                pass
        store = Store()
        native = android_reader('webtoon', store, client=site)
        manifest = native.capture_automatic(native.episode_url('7000009'))
        native.retrieve(manifest.episodes[0].pages[0])
        self.assertTrue(store.calls)
        self.assertTrue(all(urlsplit(url).hostname in {'www.webtoons.com', 'm.webtoons.com'} for url in store.calls))
        self.assertNotIn('global.apis.naver.com', {urlsplit(url).hostname for url in store.calls})
        self.assertEqual(len(site.asset_calls()), 1)

    def test_the_gateway_clock_is_bounded_and_refuses_cancellation(self):
        reader, site = adapter()
        reader.transport.cancel_check = lambda: (_ for _ in ()).throw(RunLimit('cancelled'))
        with self.assertRaises(RunLimit):
            reader.inspect_library(category='unlocked')
        self.assertEqual(site.calls, [])
        for clock in (b'PRIVATE_NOT_A_CLOCK', b'', b'9' * 17):
            with self.assertRaises(MetadataError):
                signed_url('myUnlockedTitleProductsV1.json', {}, clock)
