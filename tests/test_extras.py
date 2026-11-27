import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from comic_archive import download_selection, extras
from comic_archive.adapters.tapas_comments import comment_page, episode_comments
from comic_archive.errors import AuthenticationError, LibraryMoved, RunLimit
from comic_archive.models import ArchiveError, Episode, Page, Series
from comic_archive.network import NetworkPolicyHold

ALL_OFF = dict.fromkeys(extras.KINDS, False)
ALL_ON = dict.fromkeys(extras.KINDS, True)
TYPES = dict.fromkeys(download_selection.TYPES, True)
MP3 = b'ID3\x04\x00' + b'\x00' * 64


def comment_row(key, name, date, words, likes, replies):
    return (f'<div class="comment-row-wrap js-comment-parent-row" id="comment-row-{key}"><div class="row__body">'
            f'<div class="body__writer"><a class="writer__name" href="/x">{name}</a><p class="writer__date">{date}</p></div>'
            f'<div class="body__comment js-comment-body">{words}</div>'
            f'<a class="info__button" data-cnt="{likes}"><i class="ico"></i><span class="js-like-cnt">{likes}</span></a><hr/>'
            f'<div><a class="body__button js-toggle-reply-btn hidden" data-reply-cnt="{replies}">'
            f'<span class="button__label">View {replies} reply</span></a></div>'
            '<script type="text/template"><textarea class="js-edit-box">{{body}}</textarea></script></div>'
            '<div class="comment-row__reply hidden js-reply-list"></div></div>')


class Source:
    """The job's budget as save_episode sees it."""
    def __init__(self, seen, music=MP3, comments=None):
        self.seen, self.music, self.comments, self.asked = seen, music, comments, []

    def episode_extras(self, key):
        self.asked.append(('seen', key))
        return self.seen

    def retrieve_extra(self, url, max_bytes):
        self.asked.append(('music', url, max_bytes))
        if isinstance(self.music, BaseException):
            raise self.music
        return self.music

    def episode_comments(self, key, limit):
        self.asked.append(('comments', key, limit))
        if isinstance(self.comments, BaseException):
            raise self.comments
        return self.comments


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.series = Series('s', 'Series', source='tapas', platform_id='42')
        self.episode = Episode('81', 's', 'Episode 1', 1, (Page(1, 'https://us-a.tapas.io/pc/1.jpg', False),), 'tapas_free',
                               source_url='https://tapas.io/episode/81')
        self.seen = extras.found('A note\nwith two lines.', extras.counts(views=10, likes=2, comments=1),
                                 (('https://tapas.io/bgm/1.mp3', 'Theme'),), comments=True)

    def test_every_extra_starts_off_and_an_unreadable_choice_saves_nothing(self):
        self.assertEqual(extras.read(self.db), ALL_OFF)
        extras.save(self.db, ALL_ON | {'music': False})
        self.assertEqual(extras.read(self.db), ALL_ON | {'music': False})
        for bad in ('{PRIVATE', json.dumps({'notes': True}), json.dumps(ALL_ON | {'notes': 'yes'}), '[]'):
            with self.subTest(bad=bad):
                self.db.execute("UPDATE settings SET value=? WHERE key='download_extras'", (bad,))
                with self.assertLogs('comic_archive.download', 'WARNING') as logs:
                    self.assertEqual(extras.read(self.db), ALL_OFF)
                self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        for bad in ({'notes': True}, ALL_ON | {'more': True}, ALL_ON | {'stats': 1}, None):
            with self.subTest(bad=bad), self.assertRaises(ArchiveError):
                extras.validate(bad)

    def test_each_site_offers_only_what_it_shows(self):
        self.assertEqual(extras.offered('tapas'), ('notes', 'music', 'stats', 'comments'))
        self.assertEqual(extras.offered('webtoon'), ('notes', 'music', 'stats'))
        self.assertEqual(extras.offered('tappytoon'), ())
        self.assertEqual(extras.offered('elsewhere'), ())
        for kind in extras.KINDS:
            self.assertTrue(extras.label(kind) and extras.help_text(kind))

    def test_episode_types_may_carry_some_extras_and_keep_the_others(self):
        self.db.execute('CREATE TABLE download_paid_series (series_id TEXT PRIMARY KEY)')
        extras.save(self.db, ALL_OFF | {'stats': True})
        download_selection.save(self.db, TYPES | {'free': False, 'notes': True})
        self.assertEqual(download_selection.read(self.db), TYPES | {'free': False})
        self.assertEqual(extras.read(self.db), ALL_OFF | {'stats': True, 'notes': True})
        stored = json.loads(self.db.execute("SELECT value FROM settings WHERE key='download_types'").fetchone()[0])
        self.assertEqual(set(stored), set(download_selection.TYPES))
        # Types alone leave the extras as they were.
        download_selection.save(self.db, TYPES)
        self.assertEqual(extras.read(self.db), ALL_OFF | {'stats': True, 'notes': True})
        for bad in (TYPES | {'other': True}, TYPES | {'notes': 'yes'}, dict.fromkeys(download_selection.TYPES, False) | {'notes': True}):
            with self.subTest(bad=bad), self.assertRaises(ArchiveError):
                download_selection.validate(bad)

    def test_nothing_is_asked_or_written_until_an_extra_is_chosen(self):
        source = Source(self.seen)
        self.assertIsNone(extras.save_episode(self.root, self.db, source, self.series, self.episode))
        self.assertEqual(source.asked, [])
        self.assertFalse((self.root / '.archive-state').exists())
        # A site that shows none is never asked, whatever was chosen.
        extras.save(self.db, ALL_ON)
        other = Series('t', 'Series', source='tappytoon', platform_id='7')
        self.assertIsNone(extras.save_episode(self.root, self.db, source, other, self.episode))
        self.assertEqual(source.asked, [])

    def test_chosen_extras_are_kept_in_the_private_folder_with_the_music(self):
        extras.save(self.db, ALL_ON)
        comments = {'comments': [{'id': '1', 'name': 'A', 'date': 'Mar 09, 2018', 'text': 'Hi', 'likes': 0, 'replies': 0}],
                    'total': 1, 'complete': True}
        source = Source(self.seen, comments=comments)
        with self.assertLogs('comic_archive.download', 'INFO') as logs:
            path = extras.save_episode(self.root, self.db, source, self.series, self.episode)
        self.assertEqual(path, self.root / '.archive-state/extras/tapas/42/81.json')
        value = json.loads(path.read_text())
        self.assertEqual(value['creator_note'], 'A note\nwith two lines.')
        self.assertEqual({key: value['stats'][key] for key in ('views', 'likes', 'comments')},
                         {'views': 10, 'likes': 2, 'comments': 1})
        self.assertEqual(value['music'][0]['file'], '81.music-1.mp3')
        self.assertEqual(value['music'][0]['title'], 'Theme')
        self.assertEqual((path.parent / '81.music-1.mp3').read_bytes(), MP3)
        self.assertEqual((value['comments'], value['comments_total'], value['comments_complete']),
                         (comments['comments'], 1, True))
        self.assertEqual(source.asked, [('seen', '81'), ('music', 'https://tapas.io/bgm/1.mp3', extras.MUSIC_BYTES),
                                        ('comments', '81', extras.MAX_COMMENTS)])
        self.assertIn('saved=creator_note,stats,music,comments', '\n'.join(logs.output))
        self.assertNotIn('two lines', '\n'.join(logs.output))
        # Nothing beside the episode's own files changes.
        self.assertEqual([p.name for p in self.root.iterdir()], ['.archive-state'])

    def test_only_chosen_extras_are_requested_and_earlier_choices_are_kept(self):
        extras.save(self.db, ALL_OFF | {'notes': True})
        source = Source(self.seen)
        value = json.loads(extras.save_episode(self.root, self.db, source, self.series, self.episode).read_text())
        self.assertNotIn('stats', value)
        self.assertNotIn('music', value)
        self.assertNotIn('comments', value)
        self.assertEqual(source.asked, [('seen', '81')])
        extras.save(self.db, ALL_OFF | {'stats': True})
        value = json.loads(extras.save_episode(self.root, self.db, Source(self.seen), self.series, self.episode).read_text())
        self.assertEqual(value['creator_note'], self.seen['note'])
        self.assertEqual(value['stats']['views'], 10)

    def test_unchosen_notes_are_not_saved_when_counts_are_selected(self):
        extras.save(self.db, ALL_OFF | {'stats': True})
        value = json.loads(extras.save_episode(self.root, self.db, Source(self.seen),
                                              self.series, self.episode).read_text())
        self.assertNotIn('creator_note', value)
        self.assertEqual(value['stats']['views'], 10)

    def test_an_extra_that_cannot_be_had_is_left_out_and_the_rest_kept(self):
        extras.save(self.db, ALL_ON)
        for music in (b'<html>not a sound</html>', ArchiveError('gone'), OSError('disk')):
            with self.subTest(music=music):
                source = Source(self.seen, music=music, comments=ArchiveError('Tapas returned unexpected metadata'))
                value = json.loads(extras.save_episode(self.root, self.db, source, self.series, self.episode).read_text())
                self.assertNotIn('music', value)
                self.assertNotIn('comments', value)
                self.assertEqual(value['creator_note'], 'A note\nwith two lines.')
                self.assertEqual(sorted(p.name for p in (self.root / '.archive-state/extras/tapas/42').iterdir()), ['81.json'])
        # Nothing seen, or nothing chosen that was seen, writes nothing.
        for seen in (None, extras.found()):
            with self.subTest(seen=seen):
                (self.root / '.archive-state/extras/tapas/42/81.json').unlink(missing_ok=True)
                self.assertIsNone(extras.save_episode(self.root, self.db, Source(seen), self.series, self.episode))
                self.assertFalse((self.root / '.archive-state/extras/tapas/42/81.json').exists())

    def test_a_lost_sign_in_a_limit_or_a_request_to_slow_down_still_stops_the_job(self):
        extras.save(self.db, ALL_ON)
        slow = ArchiveError('slow down')
        slow.source_http_status = 429
        for error in (RunLimit('limit'), AuthenticationError('signed out'), LibraryMoved(), slow, NetworkPolicyHold('mobile data')):
            for place in ('music', 'comments', 'seen'):
                with self.subTest(error=type(error).__name__, place=place):
                    source = Source(self.seen, music=error if place == 'music' else MP3,
                                    comments=error if place == 'comments' else {'comments': [], 'total': 0, 'complete': True})
                    if place == 'seen':
                        source.episode_extras = Mock(side_effect=error)
                    with self.assertRaises(type(error)):
                        extras.save_episode(self.root, self.db, source, self.series, self.episode)
        # Any other failure, anywhere, loses only the extras.
        source = Source(self.seen)
        source.episode_extras = Mock(side_effect=ArchiveError('odd'))
        self.assertIsNone(extras.save_episode(self.root, self.db, source, self.series, self.episode))

    def test_found_keeps_bounded_https_music_and_plain_words(self):
        seen = extras.found('  \x00one\r\n\r\n\r\n\r\ntwo  ', {'likes': 3},
                            [('http://tapas.io/a.mp3', 'x'), ('https://tapas.io/a.mp3', 'y' * 500), (None, 'z'),
                             *((f'https://tapas.io/{n}.mp3', None) for n in range(10))])
        self.assertEqual(seen['note'], 'one\n\ntwo')
        self.assertEqual(len(seen['music']), extras.MAX_TRACKS)
        self.assertEqual(seen['music'][0], {'url': 'https://tapas.io/a.mp3', 'title': 'y' * extras.NAME_CHARACTERS})
        self.assertEqual(extras.counts(views=-1, likes=True, comments='3'), None)
        self.assertEqual(extras.counts(views=0, likes=5), {'views': 0, 'likes': 5})
        self.assertEqual(extras.text('   ', 10), None)

    def test_creator_note_is_the_words_of_its_element_with_its_line_breaks(self):
        html = ('<div class="js-episode-story-wrap"><p class="other">not this</p>'
                '<p class="text js-episode-story text--nm">First &amp; line<br>second <b>bold</b><br/>third</p>'
                '<p class="text js-episode-story">a second note is not read</p></div>')
        self.assertEqual(extras.ElementText.of(html, 'js-episode-story'), 'First & line\nsecond bold\nthird')
        self.assertIsNone(extras.ElementText.of('<p class="text js-episode-story text--nm"></p>', 'js-episode-story'))
        self.assertIsNone(extras.ElementText.of('<p>none</p>', 'js-episode-story'))

    def test_sound_files_are_named_by_their_first_bytes(self):
        cases = {b'ID3\x03': '.mp3', b'\xff\xfb\x90': '.mp3', b'\x00\x00\x00\x20ftypM4A ': '.m4a', b'OggS\x00': '.ogg',
                 b'fLaC\x00': '.flac', b'RIFF\x00\x00\x00\x00WAVEfmt ': '.wav', b'<html>': None, b'': None,
                 b'\xff\x00': None, b'RIFF\x00\x00\x00\x00AVI ': None}
        for data, extension in cases.items():
            with self.subTest(data=data):
                self.assertEqual(extras.audio_extension(data), extension)

    def test_extras_stay_inside_the_library_folder(self):
        for source, key in (('tapas', '../x'), ('../tapas', '42'), ('nowhere', '42')):
            with self.subTest(source=source, key=key), self.assertRaises(ArchiveError):
                extras.folder(self.root, source, key)


class TapasCommentTests(unittest.TestCase):
    def test_a_comment_page_gives_each_top_level_comment_and_its_counts(self):
        html = (comment_row('11', 'Ann &amp; Co', 'Mar 09, 2018', 'Great<br>page <i>here</i>', 4, 2)
                + comment_row('12', 'Bo', 'Mar 10, 2018', '', 'x', ''))
        self.assertEqual(comment_page(html), [
            {'id': '11', 'name': 'Ann & Co', 'date': 'Mar 09, 2018', 'text': 'Great\npage here', 'likes': 4, 'replies': 2},
            {'id': '12', 'name': 'Bo', 'date': 'Mar 10, 2018', 'text': None, 'likes': None, 'replies': None}])
        self.assertEqual(comment_page(''), [])
        for bad in (None, 'x' * (4 * 1024 * 1024 + 1)):
            with self.subTest(bad=type(bad)), self.assertRaises(ArchiveError):
                comment_page(bad)

    def test_pages_are_read_newest_first_until_tapas_says_there_are_no_more(self):
        pages = {1: (['1', '2'], {'page': 1, 'has_next': True, 'since': 99}),
                 2: (['2', '3'], {'page': 2, 'has_next': False, 'since': 99})}
        asked = []
        def json_get(path):
            asked.append(path)
            page = int(path.split('page=')[1].split('&')[0])
            keys, pagination = pages[page]
            return {'html': ''.join(comment_row(k, 'N', 'D', 'T', 0, 0) for k in keys), 'pagination': pagination,
                    'total_comment_cnt': 5}
        check = Mock()
        listed = episode_comments(json_get, '81', 300, check)
        self.assertEqual([row['id'] for row in listed['comments']], ['1', '2', '3'])
        self.assertEqual((listed['total'], listed['complete']), (5, True))
        self.assertEqual(asked, ['/comment/81?page=1&sort=NEWEST', '/comment/81?page=2&sort=NEWEST&since=99'])
        self.assertEqual(check.call_count, 2)

    def test_no_comment_page_is_passed_over_when_tapas_names_the_following_page_in_its_answer(self):
        # The episode list's answers name the page that follows; the comments' answers may do the same.
        pages = {1: (['1', '2'], {'page': 2, 'has_next': True}), 2: (['3', '4'], {'page': 3, 'has_next': True}),
                 3: (['5'], {'page': 4, 'has_next': False})}
        asked = []
        def json_get(path):
            page = int(path.split('page=')[1].split('&')[0])
            asked.append(page)
            keys, pagination = pages[page]
            return {'html': ''.join(comment_row(k, 'N', 'D', 'T', 0, 0) for k in keys), 'pagination': pagination}
        listed = episode_comments(json_get, '81', 300)
        self.assertEqual([row['id'] for row in listed['comments']], ['1', '2', '3', '4', '5'])
        self.assertEqual((asked, listed['complete']), ([1, 2, 3], True))

    def test_comments_stop_at_the_limit_or_an_answer_without_pages_and_say_so(self):
        def endless(path):
            page = int(path.split('page=')[1].split('&')[0])
            return {'html': ''.join(comment_row(f'{page}{n}', 'N', 'D', 'T', 0, 0) for n in range(10)),
                    'pagination': {'page': page, 'has_next': True}}
        asked = Mock(side_effect=endless)
        listed = episode_comments(asked, '81', 25)
        self.assertEqual((len(listed['comments']), listed['complete'], asked.call_count), (25, False, 3))
        listed = episode_comments(lambda path: {'html': comment_row('1', 'N', 'D', 'T', 0, 0)}, '81', 25)
        self.assertEqual((len(listed['comments']), listed['complete']), (1, False))
        # A page that never moves on is still bounded.
        stuck = Mock(return_value={'html': comment_row('1', 'N', 'D', 'T', 0, 0), 'pagination': {'page': 1, 'has_next': True}})
        listed = episode_comments(stuck, '81', 30)
        self.assertEqual((len(listed['comments']), listed['complete'], stuck.call_count), (1, False, 4))
        for key in ('0', 'x', '1/2', 81):
            with self.subTest(key=key), self.assertRaises(ArchiveError):
                episode_comments(stuck, key, 30)


class SiteCaptureTests(unittest.TestCase):
    def test_webtoon_capture_remembers_the_note_likes_and_music_for_one_episode(self):
        from comic_archive.adapters.webtoon import WebtoonAdapter
        adapter = WebtoonAdapter.__new__(WebtoonAdapter)
        with self.assertLogs('comic_archive.download', 'INFO') as logs:
            adapter._remember_extras('7', {'creatorNote': 'Thanks!', 'likeItCount': 12, 'bgmInfo': [
                {'bgmUrl': '/bgm/one.mp3', 'bgmTitle': 'One', 'secret': 'PRIVATE'}, 'odd', {'title': 'no address'}]})
        seen = adapter.episode_extras('7')
        self.assertEqual(seen['note'], 'Thanks!')
        self.assertEqual(seen['stats'], {'likes': 12})
        self.assertEqual(seen['music'], [{'url': 'https://webtoon-phinf.pstatic.net/bgm/one.mp3', 'title': 'One'}])
        self.assertFalse(seen['comments'])
        self.assertIn('music_fields=bgmTitle,bgmUrl,secret', '\n'.join(logs.output))
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertIsNone(adapter.episode_extras('7'))
        for n in range(20):
            adapter._remember_extras(str(n), {})
        self.assertIsNone(adapter.episode_extras('0'))
        self.assertIsNotNone(adapter.episode_extras('19'))

    def test_webtoon_music_is_read_only_from_its_own_hosts(self):
        from comic_archive.adapters.webtoon import WebtoonAdapter
        adapter = WebtoonAdapter.__new__(WebtoonAdapter)
        adapter.transport = Mock()
        adapter.transport.get.return_value = MP3
        self.assertEqual(adapter.retrieve_extra('https://webtoon-phinf.pstatic.net/bgm/one.mp3', 5), MP3)
        adapter.transport.get.assert_called_once_with('https://webtoon-phinf.pstatic.net/bgm/one.mp3', max_bytes=5)
        for bad in ('https://example.com/one.mp3', 'http://webtoon-phinf.pstatic.net/one.mp3'):
            with self.subTest(bad=bad), self.assertRaises(ArchiveError):
                adapter.retrieve_extra(bad, 5)

    def test_a_webtoon_cover_or_track_over_its_own_size_cap_is_that_files_failure_and_does_not_stop_the_job_as_limited(self):
        from comic_archive.adapters.webtoon import WebtoonAdapter
        from comic_archive.errors import ResponseLimit
        adapter = WebtoonAdapter.__new__(WebtoonAdapter)
        adapter.transport = Mock()
        adapter.transport.get.side_effect = ResponseLimit('over the size asked for')
        series = Series('s', 'S', cover_url='https://webtoon-phinf.pstatic.net/cover.jpg')
        reads = {'cover': lambda budget: adapter.retrieve_cover(series, max_bytes=budget),
                 'music': lambda budget: adapter.retrieve_extra('https://webtoon-phinf.pstatic.net/bgm/one.mp3', budget)}
        for name, read in reads.items():
            with self.subTest(name):
                with self.assertRaisesRegex(ArchiveError, 'supported size') as caught:
                    read(None if name == 'cover' else extras.MUSIC_BYTES)
                self.assertNotIsInstance(caught.exception, RunLimit)
                # Less than the cap was left of the job's budget: the budget is what ran out.
                with self.assertRaises(RunLimit):
                    read(5)

    def test_tapas_music_is_read_only_from_tapas(self):
        from comic_archive.adapters.tapas_http import TapasHTTPAdapter
        adapter = TapasHTTPAdapter(transport=Mock())
        adapter.transport.get.return_value = MP3
        self.assertEqual(adapter.retrieve_extra('https://us-a.tapas.io/bgm/1.mp3', 5), MP3)
        for bad in ('https://example.com/one.mp3', 'http://us-a.tapas.io/one.mp3'):
            with self.subTest(bad=bad), self.assertRaises(ArchiveError):
                adapter.retrieve_extra(bad, 5)

    def test_a_tapas_episode_keeps_its_extras_when_the_next_one_is_read_first(self):
        # Each Tapas capture gets a new reader, and at Fast speed the job reads the next episode before it saves this
        # one's extras. The extras must still be there, on the desktop at either speed and on the phone.
        from unittest.mock import patch
        from types import SimpleNamespace
        from comic_archive.adapters.tapas import TapasAdapter
        from comic_archive.android_browser import AndroidTapasBrowserAdapter
        from comic_archive.fast_import import FastBrowserAdapter

        class Transport:
            def __init__(self, *args, **kwargs):
                self.assets, self.last, self.deadline = SimpleNamespace(client=Mock()), 0.0, None

            def close(self):
                pass

        def bare(kind, **values):
            adapter = kind.__new__(kind)
            adapter.__dict__.update(context=Mock(closed=False), page=Mock(), transport=None, **values)
            return adapter

        with patch('comic_archive.adapters.tapas.BrowserReadTransport', Transport), \
                patch('comic_archive.fast_import.BrowserReadTransport', Transport), \
                patch('comic_archive.android_browser.AndroidBrowserTransport', Transport), patch('httpx.Client'):
            for adapter in (bare(TapasAdapter), bare(FastBrowserAdapter),
                            bare(AndroidTapasBrowserAdapter, fast=True, interval=1.0, network_guard=None)):
                with self.subTest(adapter=type(adapter).__name__):
                    this = adapter.automatic_adapter()
                    this._remember_extras('1', 'note one')
                    following = adapter.automatic_adapter()
                    following._remember_extras('2', 'note two')
                    self.assertIsNot(following, this)
                    self.assertEqual(adapter.episode_extras('1'), 'note one')
                    self.assertEqual(adapter.episode_extras('2'), 'note two')
                    self.assertIsNone(adapter.episode_extras('1'))


class BudgetTests(unittest.TestCase):
    def test_music_counts_toward_the_byte_budget_and_comments_toward_its_checks(self):
        from comic_archive.jobs import DownloadBudget
        adapter = Mock(spec=['retrieve_extra', 'episode_comments', 'episode_extras'])
        adapter.retrieve_extra.return_value = b'x' * 40
        budget = DownloadBudget(adapter, max_bytes=100)
        budget.bytes = 50
        self.assertEqual(budget.retrieve_extra('https://tapas.io/a.mp3', extras.MUSIC_BYTES), b'x' * 40)
        adapter.retrieve_extra.assert_called_once_with('https://tapas.io/a.mp3', 50)
        self.assertEqual(budget.bytes, 90)
        budget.bytes = 100
        with self.assertRaises(RunLimit):
            budget.retrieve_extra('https://tapas.io/a.mp3', extras.MUSIC_BYTES)
        budget.episode_comments('81', 300)
        key, limit, between_pages = adapter.episode_comments.call_args.args
        self.assertEqual((key, limit), ('81', 300))
        between_pages()
        budget.cancellation = Mock()
        budget.cancellation.check.side_effect = RunLimit('paused')
        with self.assertRaises(RunLimit):
            between_pages()
        budget.cancellation = None
        budget.max_seconds, budget.started = 1, 0
        with self.assertRaises(RunLimit):
            between_pages()
        adapter.episode_extras.return_value = {'note': 'n'}
        self.assertEqual(budget.episode_extras('81'), {'note': 'n'})
        self.assertIsNone(DownloadBudget(Mock(spec=[])).episode_extras('81'))


if __name__ == '__main__':
    unittest.main()
