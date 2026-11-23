"""Provider document and pagination limits with offline response doubles."""
import json
import unittest
from unittest.mock import Mock, patch

from comic_archive.adapters import tapas_http, tappytoon, webtoon
from comic_archive.adapters.tapas import BrowserReadTransport
from comic_archive.errors import MetadataError, RunLimit
from comic_archive.transport import Transport, metadata_bytes
from test_transport_session import Client, Response


class ProviderMetadataLimitTests(unittest.TestCase):
    def test_transport_enforces_the_document_limit_within_the_stream(self):
        class Document(Response):
            def iter_bytes(self):
                yield b'{}'
                yield b' '
                raise AssertionError('The transport should have stopped reading')

        transport = Transport(client=Client([Document()]))
        with self.assertRaisesRegex(MetadataError, 'supported size'):
            metadata_bytes(transport, 'https://tapas.io/series/42', 2)
        transport = Mock()
        transport.get.side_effect = RunLimit('Job deadline')
        with self.assertRaisesRegex(RunLimit, 'Job deadline'):
            metadata_bytes(transport, 'https://tapas.io/series/42', 2)

    def test_tapas_checks_the_page_size_before_accepting_rows(self):
        transport = Mock()
        transport.get.return_value = json.dumps({'data': {'pagination': {'total': 2},
                                                           'episodes': [{'id': 1}, {'id': 2}]}}).encode()
        with patch.object(tapas_http, 'LIST_PAGE_SIZE', 1), self.assertRaises(MetadataError):
            tapas_http.TapasHTTPAdapter(transport=transport).enumerate_episodes(42)
        self.assertEqual(transport.get.call_args.kwargs['max_bytes'], tapas_http.MAX_JSON_BYTES)

    def test_tappytoon_counts_bytes_across_pages(self):
        transport = Mock(last_response={'link': f'<{tappytoon.API}/comics?page=2>; rel="next"'})
        transport.get.return_value = b'[{"id": 1}]'
        reader = tappytoon.TappytoonAdapter(transport=transport)
        reader._key = 'FIXTURE'
        size = len(transport.get.return_value)
        with patch.object(tappytoon, 'MAX_LIST_BYTES', size + 1), self.assertRaisesRegex(MetadataError, 'supported size'):
            reader.listed('/comics', {})
        self.assertEqual([call.kwargs['max_bytes'] for call in transport.get.call_args_list], [size + 1, 1])

    @patch('comic_archive.adapters.tapas.Transport')
    def test_browser_metadata_uses_the_smaller_caller_limit(self, assets):
        page = Mock(url='https://tapas.io/')
        page.evaluate.return_value = {'status': 200, 'text': None}
        transport = BrowserReadTransport(page, Mock(), metadata_interval=0.25)
        with self.assertRaisesRegex(MetadataError, 'supported response size'):
            transport.get('https://tapas.io/series/42', max_bytes=12)
        _, arguments = page.evaluate.call_args.args
        self.assertEqual(arguments['limit'], 12)

    def test_webtoon_checks_total_rows_before_adding_viewers(self):
        transport = Mock()
        transport.get.side_effect = [
            json.dumps({'result': {'episodeList': [{'episodeNo': n, 'episodeTitle': 'Episode', 'viewerLink': '/'}],
                                   'nextCursor': cursor}}).encode()
            for n, cursor in ((1, 100), (2, 0))
        ]
        reader = webtoon.WebtoonAdapter(transport=transport)
        with patch.object(webtoon, 'MAX_LIST_ROWS', 1), self.assertRaisesRegex(MetadataError, 'longer than'):
            reader.episode_list('42')
        self.assertEqual(reader.viewers, {})
        self.assertEqual(transport.get.call_count, 2)
