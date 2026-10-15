import unittest
from dataclasses import replace

from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series, canonical_url, identifier


def comic(**changes):
    base = Episode('81', 's', 'Episode', 1, (Page(1, 'https://us-a.tapas.io/pc/1.jpg', False),), 'tapas_free')
    return replace(base, **changes)


class SourceRecordTests(unittest.TestCase):
    def test_ids_are_short_ascii_words_that_cannot_climb_out_of_a_folder(self):
        for value in ('', None, 5, 'x' * 101, 'a b', 'a/b', '../x', 'a.b', 'café', '١٢'):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                identifier(value)
        for value in ('abc', 'A_b-9', 'x' * 100):
            self.assertEqual(identifier(value), value)

    def test_source_links_must_be_plain_https_without_credentials_or_ports(self):
        for value in ('http://tapas.io/x', 'https://user:secret@tapas.io/x', 'https://tapas.io:8443/x',
                      'https:///x', 'tapas.io/x', 7):
            with self.subTest(value=value), self.assertRaises(ArchiveError) as raised:
                canonical_url(value)
            self.assertNotIn('secret', str(raised.exception))
        self.assertIsNone(canonical_url(None))
        self.assertEqual(canonical_url('https://TAPAS.io/series/x?utm=1#top'), 'https://tapas.io/series/x')

    def test_pages_need_a_real_position_and_somewhere_to_read_from(self):
        for order, location in ((0, '/p'), (-1, '/p'), (True, '/p'), ('1', '/p'), (1.0, '/p'), (1, '')):
            with self.subTest(order=order, location=location), self.assertRaises(ArchiveError):
                Page(order, location)
        self.assertEqual(Page(2, '/p').order, 2)

    def test_series_details_are_bounded_text_with_a_title(self):
        Series('s', 'Title', ('Artist',), 'x' * 20000, 'https://tapas.io/series/s', platform_id='42', credits='y' * 20000,
               cover_url='https://us-a.tapas.io/cover.png')
        for changes in ({'title': ''}, {'title': '   '}, {'title': None},
                        {'creators': ['Artist']}, {'creators': ('Artist',) * 21}, {'creators': ('',)},
                        {'creators': ('  ',)}, {'creators': (7,)}, {'creators': ('a' * 201,)},
                        {'description': 'x' * 20001}, {'description': 5}, {'credits': 'x' * 20001}, {'credits': []},
                        {'source_url': 'http://tapas.io/series/s'}, {'cover_url': 'https://user:pw@cdn.example/c.png'},
                        {'platform_id': '../42'}, {'slug': 'a b'}, {'source': 'a/b'}, {'id': ''}):
            with self.subTest(changes=changes), self.assertRaises(ArchiveError):
                Series(**({'id': 's', 'title': 'Title'} | changes))

    def test_episodes_need_a_title_a_positive_order_and_readable_access(self):
        for changes in ({'title': ''}, {'title': ' '}, {'title': None}, {'order': 0}, {'order': True}, {'order': '1'},
                        {'access': 'unavailable_or_unverified'}, {'access': 'tapas_paid'}, {'access': None},
                        {'content_type': 'audio'}, {'text_html': '<p>Text</p>'},
                        {'illustrations': (b'image',)}, {'content_type': 'novel'},
                        {'pages': ()}, {'pages': (Page(2, 'https://us-a.tapas.io/pc/2.jpg', False),)},
                        {'pages': (Page(1, 'https://us-a.tapas.io/pc/1.jpg', False),
                                   Page(1, 'https://us-a.tapas.io/pc/2.jpg', False))},
                        {'source_url': 'http://tapas.io/episode/81'}):
            with self.subTest(changes=changes), self.assertRaises(ArchiveError):
                comic(**changes)
        for access in ('local_owned', 'reader_confirmed', 'tapas_free', 'tapas_unlocked'):
            self.assertEqual(comic(access=access).access, access)

    def test_a_manifest_cannot_mix_series_or_repeat_an_episode_id_or_position(self):
        series = Series('s', 'Title')
        second = comic(id='82', order=2)
        self.assertEqual(len(Manifest(series, (comic(), second)).episodes), 2)
        for episodes in ((comic(), comic(order=2)), (comic(), comic(id='82')),
                         (comic(), replace(second, series_id='other'))):
            with self.subTest(ids=[e.id for e in episodes]), self.assertRaises(ArchiveError):
                Manifest(series, episodes)


if __name__ == '__main__':
    unittest.main()
