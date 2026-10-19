"""What the library page lists and says for a page of the saved-file catalog, decided without a window."""
from datetime import datetime
import unittest

from comic_archive import library_model as model

LOCATIONS = [{'id': 'device', 'label': 'This device', 'files': 120, 'episodes': 60, 'connected': True},
             {'id': 'b2', 'label': 'Backblaze B2', 'files': 20, 'episodes': 10, 'connected': True}]
CATALOG = {'file_count': 140, 'episode_count': 70, 'locations': LOCATIONS}


def item(**changes):
    return {'id': 'a' * 64, 'series': 'Series', 'title': 'Episode 1', 'filename': 'Episode 001.cbz', 'kind': 'cbz',
            'size': 2048, 'saved_at': '2026-10-01T10:00:00+00:00', 'location': 'device'} | changes


class FormatTests(unittest.TestCase):
    def test_sizes_are_counted_in_1024s_as_windows_shows_them(self):
        for value, said in ((500, '500 B'), (1536, '1.5 KB'), (12_345_678, '11.8 MB'), (3 * 1024 ** 3, '3.0 GB'),
                            (1024 ** 5, '1024.0 TB'), (-1, '—'), (None, '—'), (float('nan'), '—'), ('5', '—')):
            self.assertEqual(model.size(value), said)

    def test_a_saved_time_is_shown_in_this_computers_time_zone_and_kept_exactly_for_its_tooltip(self):
        local = datetime.fromisoformat('2026-10-01T10:00:00+00:00').astimezone()
        self.assertEqual(model.saved_time('2026-10-01T10:00:00+00:00'),
                         (f'{local.day} {local:%b %Y, %H:%M}', '2026-10-01T10:00:00+00:00'))
        for text in ('2026-10-01T10:00:00', 'yesterday', None, 5):
            self.assertIsNone(model.saved_time(text))

    def test_page_numbers_and_their_range_follow_the_catalogs_offset_and_limit(self):
        self.assertEqual(model.pages({'offset': 100, 'limit': 50, 'total': 250}), (3, 5))
        self.assertEqual(model.pages({}), (1, 1))
        self.assertEqual(model.offset_for(3, 50), 100)
        self.assertEqual(model.offset_for(0, 50), 0)
        self.assertEqual(model.range_text({'offset': 100, 'total': 250, 'files': [{}, {}]}), 'Showing 101–102 of 250 files.')
        self.assertEqual(model.range_text({'total': 0, 'file_count': 5}), 'No saved files match these filters.')
        self.assertEqual(model.range_text({}), '')

    def test_the_notice_says_whether_anything_was_found_or_saved_at_all(self):
        self.assertEqual(model.notice({'total': 2}), 'Saved files loaded.')
        self.assertEqual(model.notice({'total': 0, 'file_count': 5}), 'Try another location or clear the search.')
        self.assertEqual(model.notice({}), 'No files have been saved yet. Use Dashboard to start.')
        self.assertEqual(model.title('Webtoon'), 'Your saved Webtoon library')


class PlaceTests(unittest.TestCase):
    def test_all_storage_comes_first_where_there_is_cloud_storage(self):
        places = model.places(CATALOG, False, 'b2')
        self.assertEqual([(p['id'], p['current']) for p in places], [('all', False), ('device', False), ('b2', True)])
        self.assertEqual(places[0], {'id': 'all', 'label': 'All storage', 'current': False, 'counts': '140 files · 70 episodes'})

    def test_without_cloud_storage_only_this_computer_is_offered(self):
        places = model.places(CATALOG, True, 'device')
        self.assertEqual(places, [{'id': 'device', 'label': 'This device', 'current': True, 'counts': '120 files · 60 episodes'}])

    def test_a_cloud_storage_that_is_not_connected_and_empty_says_so(self):
        catalog = dict(CATALOG, locations=[LOCATIONS[0], dict(LOCATIONS[1], files=0, connected=False)])
        self.assertEqual(model.places(catalog, False, 'all')[2]['counts'], '0 files · 10 episodes · Not connected')
        self.assertFalse(model.has_cloud(catalog, False))
        self.assertTrue(model.has_cloud(CATALOG, False))
        self.assertFalse(model.has_cloud(CATALOG, True))

    def test_the_summary_counts_this_computer_or_every_place(self):
        self.assertEqual(model.summary(CATALOG, True), '60 saved episodes across 120 files on this computer.')
        self.assertEqual(model.summary(CATALOG, False), '70 saved episodes across 140 files and cloud copies.')
        self.assertEqual(model.summary({'file_count': 3, 'episode_count': 1}, False), '1 saved episodes across 3 files.')


class RowTests(unittest.TestCase):
    def rows(self, *items):
        return model.rows(dict(CATALOG, files=list(items)), False)

    def test_comics_and_novels_on_this_computer_can_be_opened(self):
        comic, novel = self.rows(item(), item(id='b' * 64, kind='epub'))
        self.assertEqual((comic['open'], novel['open']), ('a' * 64, 'b' * 64))
        self.assertEqual((comic['type'], novel['type'], comic['size'], comic['storage'], comic['action']),
                         ('Comic', 'Novel', '2.0 KB', 'This device', ''))

    def test_cloud_copies_data_files_and_unexpected_ids_are_not_offered_to_open(self):
        cloud, data, odd, short = self.rows(item(location='b2'), item(kind='episode_metadata'), item(id='A' * 64), item(id=None))
        self.assertEqual([row['open'] for row in (cloud, data, odd, short)], [None] * 4)
        self.assertEqual((cloud['storage'], cloud['action']), ('Backblaze B2', 'Cloud copy'))
        self.assertEqual(data['type'], 'Data file')

    def test_missing_details_show_as_dashes_and_unknown_storage_is_named_so(self):
        row, = self.rows({'location': 'elsewhere', 'kind': 'zip'})
        self.assertEqual((row['series'], row['title'], row['filename'], row['type'], row['size'], row['saved'], row['exact']),
                         ('', '—', '', '—', '—', '—', ''))
        self.assertEqual((row['storage'], row['file_time'], row['open'], row['action']), ('Unknown storage', False, None, 'Cloud copy'))

    def test_a_time_taken_from_the_file_itself_is_marked_as_such(self):
        known, guessed = self.rows(item(), item(date_source='file_modified'))
        self.assertFalse(known['file_time'])
        self.assertTrue(guessed['file_time'])
        self.assertEqual(guessed['exact'], '2026-10-01T10:00:00+00:00')
        unknown, = self.rows(item(date_source='file_modified', saved_at='never'))
        self.assertFalse(unknown['file_time'])


if __name__ == '__main__':
    unittest.main()
