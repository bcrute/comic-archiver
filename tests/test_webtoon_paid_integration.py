"""Exercise mobile unlocks through the production worker and publication seams."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from comic_archive import sites
from comic_archive.android_readers import reader as android_reader
from comic_archive.archive import verify
from comic_archive.cancellation import request_stop
from comic_archive.errors import AuthenticationError, MetadataError
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_job, task_status
from comic_archive.publication import import_metadata
from comic_archive.transport import Transport
from comic_archive.webtoon_gateway import BASE, CLOCK, PAGE_SIZE, MobileAPI
from comic_archive.worker import execute, run_worker
from test_webtoon import LIST_API, Response, listed
from test_webtoon_paid import MobileSite, adapter, envelope, grant, title


class CookieStore:
    def headerFor(self, url):
        return 'NEO_SES=SESSION_A; NEO_CHK=CHECK_A'

    def apply(self, url, values):
        pass


@contextmanager
def workspace():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temporary:
        os.chdir(temporary)
        try:
            session = Path(temporary) / '.comic-session'
            session.mkdir()
            (session / 'site').write_text('webtoon')
            yield session, Path(temporary) / 'Library'
        finally:
            os.chdir(previous)


def seed(root):
    with Inventory(root) as inventory:
        inventory.observe('7', 'Sample Series', [{'id': '7000009', 'title': 'Episode 9',
            'order': 9, 'access_state': 'webtoon_unlocked'}], 'synthetic fixture')


def native_reader(site):
    return android_reader('webtoon', CookieStore(), client=site)


@patch('comic_archive.transport.Transport._wait', lambda self, seconds: self._check_budget())
@patch('builtins.print')
class PaidWorkerIntegrationTests(unittest.TestCase):
    def assert_archive(self, root, pictures):
        self.assertEqual(verify(root, decode_images=True), [])
        archive = next(root.rglob('*.cbz'))
        with zipfile.ZipFile(archive) as packet:
            self.assertEqual(packet.namelist(), ['0001.png', '0002.png', '0003.png'])
            self.assertEqual([packet.read(name) for name in packet.namelist()], pictures)
        metadata = json.loads(archive.with_suffix('.json').read_text())
        self.assertEqual(metadata['schema_version'], 2)
        self.assertNotIn('access_state', metadata)
        self.assertNotIn('source_episode_id', metadata)
        metadata = import_metadata(archive.with_suffix('.json'))
        self.assertEqual(metadata['access_state'], 'webtoon_unlocked')
        self.assertEqual(metadata['source_episode_id'], '7000009')
        return archive

    def test_desktop_and_native_worker_save_standby_unlocks_at_every_supported_speed(self, output):
        for native in (False, True):
            for speed in ('normal', 'slow', 'medium', 'fast'):
                with self.subTest(native=native, speed=speed), workspace() as (_, root):
                    site = MobileSite()
                    reader = native_reader(site) if native else adapter(site)[0]
                    seed(root)
                    result = execute(reader, {'operation': 'archive_discovered', 'args': {
                        'limit': 1, 'retry_episode_id': '7000009', 'speed': speed}})
                    self.assertEqual(result['archived_episode_ids'], ['7000009'])
                    self.assertTrue(result['reader_probe_succeeded'])
                    self.assertEqual(result['downloaded_pages'], 3)
                    self.assertEqual(len(site.asset_calls()), 3)
                    self.assert_archive(root, site.pictures)
                    self.assertEqual(reader.download_profile, 'normal')
                    self.assertEqual(reader.interval, 1.0)

    def test_permanent_zero_price_products_save_through_both_worker_seams(self, output):
        for native in (False, True):
            with self.subTest(native=native), workspace() as (_, root):
                site = MobileSite()
                site.rights = [grant(policy='FREE_POLICY')]
                site.manifest_changes['product'] = True
                reader = native_reader(site) if native else adapter(site)[0]
                seed(root)
                result = execute(reader, {'operation': 'archive_discovered', 'args': {'limit': 1}})
                self.assertEqual(result['archived_episode_ids'], ['7000009'])
                self.assertEqual(verify(root, decode_images=True), [])
                archive = next(root.rglob('*.cbz'))
                with zipfile.ZipFile(archive) as packet:
                    self.assertEqual([packet.read(name) for name in packet.namelist()], site.pictures)
                metadata = json.loads(archive.with_suffix('.json').read_text())
                self.assertNotIn('access_state', metadata)
                with Inventory(root, read_only=True) as inventory:
                    self.assertEqual(inventory.db.execute("SELECT access_state FROM episodes WHERE id='7000009'").fetchone()[0], 'webtoon_free')

    def test_actual_queue_publishes_a_successful_paid_job_response_on_both_reader_seams(self, output):
        for native in (False, True):
            with self.subTest(native=native), workspace() as (session, root):
                site = MobileSite()
                reader = native_reader(site) if native else adapter(site)[0]
                seed(root)
                (session / 'worker/requests').mkdir(parents=True)
                task = submit_job(session, 'archive_discovered', {'limit': 1, 'retry_episode_id': '7000009'})
                with patch('comic_archive.worker.time.sleep', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        run_worker(session, adapter_factory=lambda: reader)
                result = task_status(session, task['task_id'])
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['result']['archived_episode_ids'], ['7000009'])
                self.assertFalse((session / 'worker/requests' / (task['task_id'] + '.json')).exists())
                self.assert_archive(root, site.pictures)

    def test_worker_discovers_a_paid_only_series_and_saves_it_without_a_seed_report(self, output):
        with workspace() as (_, root):
            reader, site = adapter()
            site.rights = [grant()]
            site.pages[LIST_API.format(kind='webtoon', title=7)] = listed('fantasy', 'sample-series', 7, [], 0)
            result = execute(reader, {'operation': 'sync', 'args': {'limit': 1}})
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            self.assertEqual([row['category'] for row in result['refresh']['reading_lists']], ['subscribed', 'unlocked'])
            self.assert_archive(root, site.pictures)

    def test_restart_renews_delivery_queries_and_reuses_only_the_completed_pages(self, output):
        with workspace() as (_, root):
            reader, site = adapter()
            seed(root)
            first = execute(reader, {'operation': 'archive_discovered', 'args': {'limit': 1, 'max_pages': 1}})
            self.assertEqual(first['status'], 'limited')
            self.assertEqual(first['archived_episode_ids'], [])
            self.assertFalse(list(root.rglob('*.cbz')))
            resumed, fresh = adapter()
            fresh.version = 'two'
            result = execute(resumed, {'operation': 'archive_discovered', 'args': {'limit': 1}})
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            self.assertEqual(len(fresh.asset_calls()), 2)
            self.assertFalse(any('notice.png' in url for url, _ in fresh.asset_calls()))
            self.assert_archive(root, site.pictures)

    def test_revocation_after_restart_never_publishes_a_partial_cache(self, output):
        with workspace() as (_, root):
            reader, site = adapter()
            seed(root)
            execute(reader, {'operation': 'archive_discovered', 'args': {'limit': 1, 'max_pages': 1}})
            resumed, fresh = adapter()
            fresh.rights[0]['rightInfo']['hasRight'] = False
            fresh.denied_episodes.add(9)
            result = execute(resumed, {'operation': 'archive_discovered', 'args': {'limit': 1}})
            self.assertEqual(result['archived_episode_ids'], [])
            self.assertEqual(result['unavailable_episode_ids'], ['7000009'])
            self.assertEqual(len(fresh.content_calls()), 1)
            self.assertEqual(fresh.asset_calls(), [])
            self.assertFalse(list(root.rglob('*.cbz')))

    def test_account_switch_during_a_page_prevents_publication(self, output):
        with workspace() as (_, root):
            reader, site = adapter()
            seed(root)
            original = site.stream
            def stream(method, url, headers):
                response = original(method, url, headers)
                if '/paid/' in urlsplit(url).path:
                    reader.transport.cookies = (('.webtoons.com', 'NEO_SES=SESSION_B'),)
                return response
            site.stream = stream
            with self.assertRaises(AuthenticationError):
                execute(reader, {'operation': 'archive_discovered', 'args': {'limit': 1}})
            self.assertEqual(len(site.asset_calls()), 1)
            self.assertFalse(list(root.rglob('*.cbz')))

    def test_pause_during_transfer_retains_completed_pages_for_a_fresh_worker(self, output):
        with workspace() as (_, root):
            reader, site = adapter()
            seed(root)
            original = site.stream
            def stream(method, url, headers):
                response = original(method, url, headers)
                if urlsplit(url).path == '/paid/1.png':
                    progress = json.loads((root / '.archive-state/progress.json').read_text())
                    request_stop(root, progress['run_id'])
                return response
            site.stream = stream
            result = execute(reader, {'operation': 'archive_discovered', 'args': {'limit': 1}})
            self.assertEqual(result['status'], 'cancelled')
            self.assertFalse(list(root.rglob('*.cbz')))
            resumed, fresh = adapter()
            result = execute(resumed, {'operation': 'archive_discovered', 'args': {'limit': 1}})
            self.assertEqual(result['archived_episode_ids'], ['7000009'])
            self.assertEqual(len(fresh.asset_calls()), 2)
            self.assert_archive(root, site.pictures)

    def test_catalogue_service_flags_do_not_override_complete_reader(self, output):
        for change in ({'serviceStatus': 'REMOVED'}, {'serviceStatus': 'UNKNOWN'},
                       {'titleServiceStatus': 'STANDBY'}, {'titleServiceStatus': 'REMOVED'}):
            reader, site = adapter()
            site.manifest_changes = change
            with self.subTest(change=change):
                episode = reader.capture_automatic(reader.episode_url('7000009')).episodes[0]
                self.assertEqual(len(episode.pages), 3)
            self.assertEqual(site.asset_calls(), [])

class PaidProtocolBoundaryTests(unittest.TestCase):
    def test_exact_full_title_page_requires_an_empty_boundary_page(self):
        reader, site = adapter()
        site.titles = [title(i) for i in range(1, PAGE_SIZE + 1)]
        self.assertTrue(reader.inspect_library(category='unlocked')['boundary_observed'])
        requests = [parse_qs(urlsplit(url).query) for url, _ in site.calls if 'myUnlockedTitleProducts' in url]
        self.assertEqual([item['offset'] for item in requests], [['0'], [str(PAGE_SIZE)]])

    def test_episode_unlock_pagination_finds_rights_beyond_the_first_page(self):
        reader, site = adapter()
        site.rights = [grant(i) for i in range(1, PAGE_SIZE + 2)]
        episode = reader.capture_automatic(reader.episode_url(f'7{PAGE_SIZE + 1:06d}')).episodes[0]
        self.assertEqual(episode.order, PAGE_SIZE + 1)
        queries = [parse_qs(urlsplit(url).query) for url, _ in site.calls if 'myUnlockedEpisodeProducts' in url]
        self.assertIn([str(PAGE_SIZE)], [q['offset'] for q in queries])

    def test_unbounded_or_malformed_lists_never_claim_a_complete_account_inventory(self):
        for rows in (None, [None], [{'titleNo': True}], [title()] * (PAGE_SIZE + 1)):
            site = MobileSite()
            original = site.stream
            def stream(method, url, headers):
                if 'myUnlockedTitleProducts' in url:
                    return Response(envelope({'unlockedTitleProducts': rows}))
                return original(method, url, headers)
            site.stream = stream
            reader, _ = adapter(site)
            checkpoints = []
            with self.subTest(rows=rows), self.assertRaises(MetadataError):
                reader.inspect_library(category='unlocked', on_page=checkpoints.append)
            self.assertFalse(any(p['complete'] for p in checkpoints))
            self.assertEqual(site.asset_calls(), [])

    def test_page_bound_does_not_convert_incomplete_discovery_to_success(self):
        reader, site = adapter()
        site.titles = [title(i) for i in range(1, PAGE_SIZE + 2)]
        with patch('comic_archive.webtoon_gateway.MAX_PAGES', 1), self.assertRaises(MetadataError):
            reader.inspect_library(category='unlocked')
        self.assertIsNone(reader._mobile_titles_snapshot)

    def test_refresh_rejects_changes_to_cut_identity_dimensions_and_size(self):
        for field in ('cutId', 'width', 'height', 'fileSize'):
            reader, site = adapter()
            episode = reader.capture_automatic(reader.episode_url('7000009')).episodes[0]
            images = deepcopy(json.loads(site.stream('GET', next(url for url, _ in site.content_calls()),
                {'Cookie': 'NEO_SES=SESSION_A; NEO_CHK=CHECK_A'}).data)['message']['result']['episodeInfo']['imageInfo'])
            images[1][field] += 1
            site.manifest_changes['imageInfo'] = images
            with self.subTest(field=field), self.assertRaises(MetadataError):
                reader.refresh(episode)
            self.assertEqual(site.asset_calls(), [])

    def test_gateway_redirects_and_malformed_envelopes_do_not_fetch_outside_content(self):
        for status, body in ((302, b'REDIRECT'), (200, b'NOT_JSON'), (200, b'[]'),
                             (401, b'LOGIN'), (403, b'FORBIDDEN'),
                             (200, envelope([])),
                             (200, b'{"message":{"type":"response","service":"other","result":{}}}')):
            site = MobileSite()
            original = site.stream
            def stream(method, url, headers):
                if url.startswith(BASE):
                    return Response(body, status)
                return original(method, url, headers)
            site.stream = stream
            reader, _ = adapter(site)
            with self.subTest(status=status, body=body), self.assertRaises(ArchiveError):
                reader.inspect_library(category='unlocked')
            self.assertEqual(site.asset_calls(), [])

    def test_malformed_entitlements_do_not_authorize_a_manifest(self):
        for change in ({'titleNo': 8}, {'contentType': 'UNKNOWN'}, {'episodeTitleName': ''}):
            reader, site = adapter()
            site.rights = [grant() | change]
            with self.subTest(change=change), self.assertRaises(MetadataError):
                reader.episode_url('7000009')
            self.assertEqual(site.content_calls(), [])
            self.assertEqual(site.asset_calls(), [])

    def test_bad_title_metadata_never_reaches_page_retrieval(self):
        variants = [({'titleNo': 8}, {}), ({'authorList': [None]}, {}),
                    ({'authorList': [{'authorName': ''}]}, {}),
                    ({'posterThumbnailUrl': 'https://example.com/cover.png'}, {}),
                    ({}, {'titleHomePcWebUrl': 'https://example.com/list?title_no=7'}),
                    ({}, {'titleHomePcWebUrl': 'https://www.webtoons.com/en/canvas/sample/list?title_no=7'})]
        for title_change, meta_change in variants:
            reader, site = adapter()
            original = site.stream
            def stream(method, url, headers):
                response = original(method, url, headers)
                if 'titleHomeMainV3' in url:
                    result = json.loads(response.data)['message']['result']
                    result['title'].update(title_change)
                    result['episodeMeta'].update(meta_change)
                    return Response(envelope(result))
                return response
            site.stream = stream
            with self.subTest(title=title_change, meta=meta_change), self.assertRaises(MetadataError):
                reader.episode_url('7000009')
            self.assertEqual(site.asset_calls(), [])

    def test_website_404_fallback_requires_account_unlocks(self):
        for owned in (True, False):
            reader, site = adapter()
            site.rights = [grant()]
            if not owned:
                site.titles = []
            original = site.stream
            def stream(method, url, headers):
                if url.startswith('https://m.webtoons.com/api/v1/webtoon/7/episodes'):
                    return Response(b'UNAVAILABLE_WEBSITE_LIST', 404)
                return original(method, url, headers)
            site.stream = stream
            if owned:
                self.assertEqual(reader.inspect_series('7')['episodes'][0]['access_state'], 'webtoon_unlocked')
                self.assertEqual(reader.capture_automatic(reader.episode_url('7000009')).episodes[0].id, '7000009')
            else:
                with self.assertRaises(ArchiveError) as caught:
                    reader.inspect_series('7')
                self.assertEqual(caught.exception.source_http_status, 404)
                self.assertEqual(site.content_calls(), [])
            self.assertEqual(site.asset_calls(), [])

    def test_only_the_session_pair_is_rebuilt_for_every_gateway_retry(self):
        headers_seen = []
        current = ['NEO_SES=FIRST; analytics=PRIVATE']
        class Client:
            def stream(self, method, url, headers):
                if url == CLOCK:
                    return Response(b'1700000000000')
                headers_seen.append(dict(headers))
                if len(headers_seen) == 1:
                    current[0] = 'NEO_SES=SECOND; NEO_CHK=CHECK; analytics=PRIVATE'
                    return Response(b'RETRY', 503)
                return Response(envelope({'unlockedTitleProducts': []}))
        transport = Transport(site=sites.named('webtoon'), client=Client(), cookie_source=lambda url: current[0])
        api = MobileAPI(transport, lambda: 'synthetic account')
        self.assertEqual(list(api.listed('myUnlockedTitleProductsV1.json', 'unlockedTitleProducts')), [])
        self.assertEqual([h['Cookie'] for h in headers_seen], ['NEO_SES=FIRST', 'NEO_SES=SECOND; NEO_CHK=CHECK'])
