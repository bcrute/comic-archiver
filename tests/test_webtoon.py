"""Webtoon's reader, against made-up pages built like Webtoon's own (no real page text or pictures)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_archive import png
from comic_archive import sites, worker
from comic_archive.adapters.webtoon import CANVAS, WebtoonAdapter, split_episode, split_series
from comic_archive.archive import verify
from comic_archive.errors import MetadataError
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_watched_series
from comic_archive.models import ArchiveError
from comic_archive.transport import Transport

HOME = 'https://www.webtoons.com'
IMAGES = 'https://webtoon-phinf.pstatic.net/20200101_1'
LIST_API = 'https://m.webtoons.com/api/v1/{kind}/{title}/episodes?pageSize=100'


def viewer_link(genre, slug, title_no, number):
    return f'/en/{genre}/{slug}/episode-{number}/viewer?title_no={title_no}&episode_no={number}'


def listed(genre, slug, title_no, numbers, after):
    return json.dumps({'result': {'episodeList': [
        {'episodeNo': n, 'episodeTitle': f' Episode {n} ', 'viewerLink': viewer_link(genre, slug, title_no, n),
         'exposureDateMillis': 1577836800000, 'thumbnail': '/thumb.jpg'} for n in numbers], 'nextCursor': after}}).encode()


def viewer_page(genre, slug, title_no, number, images, series_title='Sample Series'):
    link = HOME + viewer_link(genre, slug, title_no, number).replace('&', '&amp;')
    pictures = ''.join(f'<img src="https://webtoons-static.pstatic.net/image/bg_transparency.png" data-url="{url}" '
                       'class="_images" alt="image">' for url in images)
    return f'''<!DOCTYPE html><html lang="en"><head><link rel="canonical" href="{link}">
<meta property="og:title" content="Not the episode title"></head><body>
<div class="subj_info"><a href="{HOME}/en/{genre}/{slug}/list?title_no={title_no}" class="subj" title="{series_title}">{series_title}</a>
<span class="tx">&gt;</span><h1 class="subj_episode" title="Episode {number}">Episode {number}</h1></div>
<div class="viewer_img _img_viewer_area" id="_imageList">{pictures}</div>
<a href="{HOME}/en/{genre}/other/list?title_no=999" class="subj" title="Another series">Another series</a>
</body></html>'''.encode()


def list_page(genre, slug, title_no, heading='h1'):
    return f'''<!DOCTYPE html><html><head>
<meta property="og:url" content="{HOME}/en/{genre}/{slug}/list?title_no={title_no}">
<meta property="og:image" content="https://swebtoon-phinf.pstatic.net/20200101_2/cover.jpg?type=crop540_540">
</head><body><div class="info"><{heading} class="subj">Sample  Series</{heading}>
<div class="author_area"><a href="https://www.webtoons.com/p/community/en/u/x" class="author">First Author</a>
, Second Author <button type="button" class="ico_info2 _btnAuthorInfo">author info</button></div></div>
<div class="detail_body"><p class="summary">A story made up for tests &amp; nothing else.</p></div></body></html>'''.encode()


class Response:
    def __init__(self, data, code=200):
        self.status_code, self.data, self.headers = code, data, {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_bytes(self):
        yield self.data


class Site:
    """A stand-in for Webtoon's servers: each address answers what it was given; any other is a test failure."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def stream(self, method, url, headers):
        self.calls.append((url, headers))
        if url not in self.pages:
            raise AssertionError(f'unexpected request: {url}')
        return Response(self.pages[url])

    def close(self):
        pass


class SignedIn(WebtoonAdapter):
    # How Webtoon names a signed-in account is learnt with an account's owner; the job needs one name.
    def account_identity(self):
        return '1'


def reader(pages, adapter=WebtoonAdapter):
    client = Site(pages)
    return adapter(transport=Transport(client=client, site=sites.named('webtoon'))), client


def series_pages():
    """A two-page episode list with one episode missing from Webtoon's numbering, and its episode pages."""
    pages = {LIST_API.format(kind='webtoon', title=7): listed('fantasy', 'sample-series', 7, [1, 2], 2),
             LIST_API.format(kind='webtoon', title=7) + '&cursor=2': listed('fantasy', 'sample-series', 7, [4], 0),
             f'{HOME}/en/fantasy/sample-series/list?title_no=7': list_page('fantasy', 'sample-series', 7),
             'https://swebtoon-phinf.pstatic.net/20200101_2/cover.jpg': png(200)}
    for number in (1, 2, 4):
        images = [f'{IMAGES}/{number}-{part}.png?type=q90' for part in (1, 2)]
        pages[HOME + viewer_link('fantasy', 'sample-series', 7, number)] = viewer_page('fantasy', 'sample-series', 7, number, images)
        for part in (1, 2):
            pages[f'{IMAGES}/{number}-{part}.png'] = png(10 * number + part)
    return pages


@patch('comic_archive.transport.time.sleep')
class WebtoonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    @patch('builtins.print')
    def test_a_series_is_saved_episode_by_episode_with_each_pictures_original_file(self, output, sleep):
        adapter, client = reader(series_pages(), SignedIn)
        with Inventory(self.root) as inventory:
            inventory.observe('7', 'Sample Series', [], 'fixture')
        with adapter:
            result = archive_watched_series(adapter, '7', self.root)
        self.assertEqual(sorted(result['archived_episode_ids']), ['7000001', '7000002', '7000004'])
        self.assertEqual(verify(self.root), [])
        saved = sorted(self.root.glob('**/*.cbz'))
        self.assertEqual(len(saved), 3)
        for path, number in zip(saved, (1, 2, 4), strict=True):
            with zipfile.ZipFile(path) as archive:
                pictures = [archive.read(name) for name in sorted(archive.namelist()) if name.endswith('.png')]
            self.assertEqual(pictures, [png(10 * number + 1), png(10 * number + 2)])
        series = json.loads(next(self.root.glob('**/series.json')).read_text())
        self.assertEqual((series['title'], series['episode_count']), ('Sample Series', 3))
        images = [headers for url, headers in client.calls if url.startswith(IMAGES)]
        self.assertEqual(len(images), 6)
        # The pictures need Webtoon's address as referer; nothing else about the reader is sent.
        self.assertEqual({tuple(sorted(headers.items())) for headers in images}, {(('Referer', 'https://www.webtoons.com/'),)})

    def test_series_details_come_from_the_series_own_page(self, sleep):
        adapter, _ = reader(series_pages())
        details = adapter.series_metadata('7')
        self.assertEqual((details.id, details.title, details.creators, details.description, details.platform_id),
                         ('sample-series', 'Sample Series', ('First Author', 'Second Author'),
                          'A story made up for tests & nothing else.', '7'))
        self.assertEqual((details.source_url, details.cover_url),
                         (f'{HOME}/en/fantasy/sample-series/list?title_no=7', 'https://swebtoon-phinf.pstatic.net/20200101_2/cover.jpg'))
        self.assertEqual(adapter.retrieve_cover(details), png(200))
        listing = reader(series_pages(), SignedIn)[0].inspect_series('7')
        self.assertEqual([(e['id'], e['order'], e['title'], e['access_state']) for e in listing['episodes']],
                         [('7000001', 1, 'Episode 1', 'webtoon_free'), ('7000002', 2, 'Episode 2', 'webtoon_free'),
                          ('7000004', 4, 'Episode 4', 'webtoon_free')])

    def test_without_an_account_no_episode_is_claimed_readable_and_nothing_is_captured(self, sleep):
        # Access marks are earned from evidence: a reader that cannot check an account reports
        # the list it saw without asserting any episode may be read, and captures nothing.
        adapter, client = reader(series_pages())
        listing = adapter.inspect_series('7')
        self.assertFalse(listing['authenticated'])
        self.assertEqual(listing['accessible_episode_count'], 0)
        self.assertEqual({e['access_state'] for e in listing['episodes']}, {None})
        self.assertEqual({e['authorization_reason'] for e in listing['episodes']}, {'webtoon_access_unverified'})
        from comic_archive.errors import AuthenticationError
        with self.assertRaises(AuthenticationError):
            adapter.capture_automatic(adapter.episode_url('7000001'))
        # No episode page or picture is asked for: capture without an account reads nothing.
        self.assertFalse([url for url, _ in client.calls if '/viewer' in url or url.startswith(IMAGES)])

    def test_a_canvas_series_has_its_own_numbers_addresses_and_library_name(self, sleep):
        key = str(CANVAS + 5)
        pages = {LIST_API.format(kind='canvas', title=5): listed('canvas', 'small-story', 5, [1], 0),
                 f'{HOME}/en/canvas/small-story/list?title_no=5': list_page('canvas', 'small-story', 5, heading='h3'),
                 HOME + viewer_link('canvas', 'small-story', 5, 1): viewer_page('canvas', 'small-story', 5, 1, [f'{IMAGES}/c.jpg?type=q90'])}
        adapter, _ = reader(pages, SignedIn)
        self.assertEqual(adapter.inspect_series(key)['title'], 'Sample Series')
        self.assertEqual(adapter.series_metadata(key).id, 'canvas-small-story')
        manifest = adapter.capture_automatic(adapter.episode_url(key + '000001'))
        self.assertEqual((manifest.series.id, manifest.series.platform_id, manifest.episodes[0].id),
                         ('canvas-small-story', key, key + '000001'))
        self.assertEqual(split_series(key), (5, True))
        self.assertEqual(split_episode(key + '000001'), (key, 1))
        for wrong in (str(CANVAS), str(2 * CANVAS), '0', '07'):
            with self.subTest(wrong=wrong), self.assertRaises(ArchiveError):
                split_series(wrong)
        for wrong in (str(CANVAS) + '000001', '7000000', '7'):
            with self.subTest(wrong=wrong), self.assertRaises(ArchiveError):
                split_episode(wrong)

    def test_an_episode_list_that_is_wrong_stops_before_any_page_is_read(self, sleep):
        api = LIST_API.format(kind='webtoon', title=7)
        broken = {
            'does not move on': {api: listed('fantasy', 's', 7, [1], 5), api + '&cursor=5': listed('fantasy', 's', 7, [2], 5)},
            'twice': {api: listed('fantasy', 's', 7, [1, 1], 0)},
            'another series': {api: listed('fantasy', 's', 8, [1], 0)},
            'elsewhere': {api: listed('fantasy', 's', 7, [1], 0).replace(b'"/en/', b'"https://example.com/en/')},
            'not a viewer': {api: listed('fantasy', 's', 7, [1], 0).replace(b'/viewer?', b'/list?')},
            'other shape': {api: b'{"result": {"episodeList": {}, "nextCursor": 0}}'},
        }
        for name, pages in broken.items():
            adapter, client = reader(pages)
            with self.subTest(name), self.assertRaises(MetadataError):
                adapter.inspect_series('7')
            # A list that does not move on is not asked again and again.
            self.assertTrue(len(client.calls) <= 2 and all(url.startswith('https://m.webtoons.com/api/') for url, _ in client.calls))

    def test_an_episode_page_showing_anything_but_the_episode_asked_for_is_refused(self, sleep):
        url = HOME + viewer_link('fantasy', 'sample-series', 7, 1)
        good = viewer_page('fantasy', 'sample-series', 7, 1, [f'{IMAGES}/a.png'])
        wrong = {
            'another episode': good.replace(b'episode_no=1"', b'episode_no=2"', 1),
            'another series': good.replace(b'list?title_no=7', b'list?title_no=8', 1),
            'no pictures': good.replace(b'class="_images"', b'class="other"'),
            'pictures elsewhere': good.replace(IMAGES.encode(), b'https://example.com'),
            'no title': good.replace(b'class="subj_episode"', b'class="other"'),
        }
        for name, page in wrong.items():
            adapter, _ = reader({url: page}, SignedIn)
            with self.subTest(name), self.assertRaises(ArchiveError):
                adapter.capture_automatic(url)
        adapter, client = reader({}, SignedIn)
        for address in (url.replace('www.webtoons.com', 'example.com'), url.replace('/viewer', '/list'), url.split('&')[0]):
            with self.subTest(address), self.assertRaises(ArchiveError):
                adapter.capture_automatic(address)
        self.assertEqual(client.calls, [])

    def test_the_downloader_reads_webtoon_with_its_reader_at_its_normal_pace(self, sleep):
        self.assertIs(worker.reader(sites.named('webtoon')), WebtoonAdapter)
        adapter, _ = reader({})
        with patch('comic_archive.fast_import.FastImportAdapter') as fast, patch('comic_archive.cloud.upload_pending'), \
                patch('comic_archive.jobs.archive_discovered', return_value={'state': 'finished'}) as plain:
            worker._execute(adapter, {'operation': 'archive_discovered', 'args': {'speed': 'fast', 'limit': 1}})
        fast.assert_not_called()
        self.assertIs(plain.call_args.args[0], adapter)


if __name__ == '__main__':
    unittest.main()
