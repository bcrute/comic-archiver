"""Observed response shapes through real adapters; transport never contacts a provider."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zipfile

from comic_archive.adapters.tapas_http import TapasHTTPAdapter
from comic_archive.adapters.pagination import EpisodeCountMismatch
from comic_archive.archive import verify
from comic_archive.errors import MetadataError
from comic_archive.publication import PublicationStore
from comic_archive.transport import Transport
from test_archive import png
from test_tappytoon import API, CHAPTERS, KEY_PAGE, Answer, page_data, reader as tappytoon_reader
from test_webtoon import Response
from test_webtoon_paid import MobileSite, adapter as webtoon_reader

FIXTURES = Path(__file__).parent / 'fixtures/provider-replay-v1'


def payload(name):
    return json.loads((FIXTURES / name).read_text())


def observations():
    return payload('metadata-observations.json')['cases']


class RecordedHTTP:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def stream(self, method, url, headers):
        if method != 'GET' or url not in self.responses:
            raise AssertionError('Unexpected replay request')
        self.calls.append(url)
        return Response(json.dumps(self.responses[url]).encode())

    def close(self):
        pass


class RecordedWebtoon(MobileSite):
    """Synthetic identity/clock; captured grant/title/manifest bodies; generated images."""
    def __init__(self):
        super().__init__()
        self.manifest_body = payload('webtoon-paid-manifest.json')
        self.replies = {'episodeInfoWithLogin.json': self.manifest_body,
                        'titleHomeMainV3': payload('webtoon-title.json'),
                        'myUnlockedEpisodeProductsV1.json': payload('webtoon-grant-page.json')}
        images = self.manifest_body['message']['result']['episodeInfo']['imageInfo']
        self.assets = {image['url']: png(index + 1) for index, image in enumerate(images)}
        self.delivered = []

    def stream(self, method, url, headers):
        if url in self.assets:
            if method != 'GET' or headers.get('Cookie') or headers.get('Authorization'):
                raise AssertionError('Replay image request leaked credentials')
            self.delivered.append(url)
            return Response(self.assets[url])
        # Retain the existing synthetic server's request signature, route and
        # credential-boundary checks before replacing its response body.
        response = super().stream(method, url, headers)
        route = urlsplit(url).path.rsplit('/', 1)[-1]
        return Response(json.dumps(self.replies[route]).encode()) if route in self.replies else response


@patch('comic_archive.transport.Transport._wait', lambda self, seconds: self._check_budget())
class ProviderReplayTests(unittest.TestCase):
    def test_observed_tapas_envelope_preserves_sparse_order_access_and_total(self):
        address = 'https://tapas.io/series/42/episodes?page=1&max_limit=20'
        client = RecordedHTTP({address: payload('tapas-episode-list.json')})
        transport = Transport(client=client)
        self.addCleanup(transport.close)
        reader = TapasHTTPAdapter(transport=transport)
        rows = reader.enumerate_episodes(42)
        expected = observations()['tapas-episode-list.json']
        self.assertEqual(len(rows), expected['episode_count'])
        self.assertEqual([row['scene'] for row in rows], expected['scenes'])
        self.assertEqual(dict(Counter(row['access_state'] for row in reader._episode_summaries(rows))),
                         expected['access_counts'])
        self.assertEqual(client.calls, [address])

    def test_changed_tapas_total_is_a_mismatch_not_a_complete_list(self):
        body = payload('tapas-episode-list.json')
        body['data']['pagination']['total'] += 1  # Constructed fault, not a captured response.
        client = RecordedHTTP({'https://tapas.io/series/42/episodes?page=1&max_limit=20': body})
        transport = Transport(client=client)
        self.addCleanup(transport.close)
        with self.assertRaises(EpisodeCountMismatch):
            TapasHTTPAdapter(transport=transport).enumerate_episodes(42)

    def tappytoon(self, first=None):
        metadata = observations()
        following = API + '/comics/67/chapters?limit=100&cursor=fixture-next'
        responses = {KEY_PAGE: page_data(), API + '/comics/67?locale=en': payload('tappytoon-comic.json'),
                     CHAPTERS: Answer(json.dumps(first if first is not None else
                                                payload('tappytoon-chapters-page-1.json')).encode(),
                                      headers=list(metadata['tappytoon-chapters-page-1.json']['response_headers'].items())),
                     following: payload('tappytoon-chapters-page-2.json')}
        reader, client = tappytoon_reader(responses)
        self.addCleanup(reader.transport.close)
        return reader, client, following

    def test_observed_tappytoon_pages_reach_the_real_end_with_all_episode_types(self):
        reader, client, following = self.tappytoon()
        result = reader.inspect_series('67')
        cases = observations()
        first, second = (cases['tappytoon-chapters-page-' + page + '.json'] for page in ('1', '2'))
        self.assertTrue(result['complete'])
        self.assertEqual(result['episode_count'], first['episode_count'] + second['episode_count'])
        self.assertEqual([row['order'] for row in result['episodes']], first['orders'] + second['orders'])
        expected = Counter(first['access_counts']) + Counter(second['access_counts'])
        self.assertEqual(Counter(row['access_state'] for row in result['episodes']), expected)
        self.assertEqual(len({row['id'] for row in result['episodes']}), 120)
        self.assertEqual([url for url, _ in client.calls if '/chapters?' in url], [CHAPTERS, following])

    def test_changed_tappytoon_comic_identity_is_rejected(self):
        first = payload('tappytoon-chapters-page-1.json')
        first[0]['comicId'] = 999  # Constructed identity fault.
        reader, _, _ = self.tappytoon(first)
        with self.assertRaises(MetadataError):
            reader.inspect_series('67')

    def test_observed_webtoon_grant_page_keeps_the_paid_rights_fields(self):
        reader, _ = webtoon_reader(RecordedWebtoon())
        self.addCleanup(reader.transport.close)
        result = reader._mobile.request('myUnlockedEpisodeProductsV1.json', titleNo=7, offset=0, pageSize=20)
        rows = result['unlockedEpisodeProducts']
        self.assertEqual(len(rows), observations()['webtoon-grant-page.json']['grant_page_count'])
        owned = next(row for row in rows if row['titleNo'] == 7 and row['episodeNo'] == 70)
        self.assertTrue(owned['rightInfo']['hasRight'])
        self.assertTrue(owned['rightInfo']['infinite'])
        self.assertEqual(owned['rightInfo']['productPolicy'], 'PAID_POLICY')

    def test_observed_webtoon_paid_manifest_produces_a_complete_ordered_archive(self):
        site = RecordedWebtoon()
        reader, _ = webtoon_reader(site)
        self.addCleanup(reader.transport.close)
        manifest = reader._capture_mobile('7000070')
        expected = observations()['webtoon-paid-manifest.json']
        episode = manifest.episodes[0]
        self.assertEqual(len(episode.pages), expected['image_count'])
        self.assertEqual(expected['observed_service_status'], 'STANDBY')
        self.assertEqual(expected['observed_product_policy'], 'PAID_POLICY')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'
            store = PublicationStore(root)
            try:
                store.archive(manifest, reader, progress=lambda _: None)
            finally:
                store.close()
            self.assertEqual(verify(root, decode_images=True), [])
            archive = next(root.rglob('*.cbz'))
            with zipfile.ZipFile(archive) as packet:
                images = [packet.read(name) for name in packet.namelist()]
            self.assertEqual(images, [site.assets[page.location] for page in episode.pages])
            self.assertCountEqual(site.delivered, site.assets)

    def test_truncated_observed_webtoon_manifest_requests_no_images(self):
        site = RecordedWebtoon()
        body = deepcopy(site.manifest_body)
        body['message']['result']['episodeInfo']['imageInfo'].pop()  # Constructed truncation.
        site.replies['episodeInfoWithLogin.json'] = body
        reader, _ = webtoon_reader(site)
        self.addCleanup(reader.transport.close)
        with self.assertRaises(MetadataError) as refused:
            reader._capture_mobile('7000070')
        self.assertEqual(refused.exception.source_validation_reason, 'image_count')
        self.assertEqual(site.delivered, [])

    def test_observed_fixture_checksums_match_the_provenance(self):
        for name, metadata in observations().items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(),
                                 metadata['sanitized_sha256'])
