import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from comic_archive.importer import archive_series
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Mock()
        self.adapter.inspect_series.return_value = {
            "complete": True, "authenticated": True, "source_series_id": "42", "title": "S",
            "episode_count": 3, "episodes": [
                {"id": "81", "order":1,"source_url": "https://tapas.io/episode/81", "access_state": "tapas_free"},
                {"id": "82", "order":2,"source_url": "https://tapas.io/episode/82", "access_state": "unavailable_or_unverified"},
                {"id": "83", "order":3,"source_url": "https://tapas.io/episode/83", "access_state": "tapas_unlocked"}]}
        series = Series("s", "S", platform_id="42")
        def capture(url):
            episode_id = url.rsplit("/", 1)[1]
            order=next(e['order'] for e in self.adapter.inspect_series.return_value['episodes'] if e['id']==episode_id)
            return Manifest(series, (Episode(episode_id, "s", "E", order, (Page(1, "/local"),), "tapas_free", url),))
        self.adapter.capture_automatic.side_effect = capture

    @patch("comic_archive.importer.Store")
    def test_only_accessible_episodes_enter_fresh_capture_and_storage(self, store):
        result = archive_series(self.adapter, "42", Path("Library"))
        self.assertEqual(result["imported_episode_ids"], ["81", "83"])
        self.assertEqual(result["skipped_unavailable"], 1)
        self.assertEqual([call.args[0] for call in self.adapter.capture_automatic.call_args_list],
                         ["https://tapas.io/episode/81", "https://tapas.io/episode/83"])
        self.assertEqual(store.return_value.archive.call_count, 2)
        store.return_value.close.assert_called_once()

    @patch("comic_archive.importer.Store")
    def test_limit_stops_without_loading_remaining_readers(self, store):
        result = archive_series(self.adapter, "42", limit=1)
        self.assertEqual(result["imported_episode_ids"], ["81"])
        self.adapter.capture_automatic.assert_called_once_with("https://tapas.io/episode/81")

    @patch("comic_archive.importer.Store")
    def test_incomplete_enumeration_and_invalid_limits_stop_before_storage(self, store):
        for limit in (0, -1, True, "1"):
            with self.assertRaises(ArchiveError):
                archive_series(self.adapter, "42", limit=limit)
        self.adapter.inspect_series.return_value["complete"] = False
        with self.assertRaises(ArchiveError):
            archive_series(self.adapter, "42")
        store.assert_not_called()
        self.adapter.capture_automatic.assert_not_called()

    @patch("comic_archive.importer.Store")
    def test_new_access_denial_stops_and_closes_store_for_resume(self, store):
        self.adapter.capture_automatic.side_effect = ArchiveError("Access revoked")
        with self.assertRaises(ArchiveError):
            archive_series(self.adapter, "42")
        store.return_value.archive.assert_not_called()
        store.return_value.close.assert_called_once()

    @patch('comic_archive.importer.Store')
    def test_unavailable_zero_scene_does_not_block_ordered_accessible_episodes(self,store):
        self.adapter.inspect_series.return_value['episodes'][1]['order']=None
        result=archive_series(self.adapter,'42')
        self.assertEqual(result['imported_episode_ids'],['81','83'])
        self.assertEqual(result['held_ordering_episode_ids'],[])
        self.assertEqual(result['skipped_unavailable'],1)
        self.assertFalse(result['ordering_verified'])
        self.assertTrue(result['accessible_ordering_verified'])
        self.adapter.inspect_series.assert_called_once_with('42',strict_order=False)

    @patch('comic_archive.importer.Store')
    def test_accessible_unknown_or_duplicate_order_is_held_without_reader_requests(self,store):
        episodes=self.adapter.inspect_series.return_value['episodes']
        episodes[0]['order']=None
        result=archive_series(self.adapter,'42')
        self.assertEqual(result['held_ordering_episode_ids'],['81'])
        self.assertFalse(result['ordering_verified'])
        self.adapter.capture_automatic.assert_called_once_with('https://tapas.io/episode/83')
        self.adapter.capture_automatic.reset_mock()
        episodes[0]['order']=3
        result=archive_series(self.adapter,'42')
        self.assertEqual(result['held_ordering_episode_ids'],['81','83'])
        self.adapter.capture_automatic.assert_not_called()

    @patch('comic_archive.importer.Store')
    def test_order_change_in_fresh_reader_stops_before_asset_storage(self,store):
        original=self.adapter.capture_automatic.side_effect
        def changed(url):
            value=original(url)
            e=value.episodes[0]
            return Manifest(value.series,(Episode(e.id,e.series_id,e.title,99,e.pages,e.access,e.source_url),))
        self.adapter.capture_automatic.side_effect=changed
        with self.assertRaises(ArchiveError):
            archive_series(self.adapter,'42')
        store.return_value.archive.assert_not_called()


    @patch('comic_archive.importer.Store')
    def test_reader_for_another_series_or_episode_stops_before_storage(self, store):
        original = self.adapter.capture_automatic.side_effect
        def episode(key, order, series_id='s'):
            return Episode(key, series_id, 'E', order, (Page(1, '/local'),), 'tapas_free')
        for label, make in (
                ('another series', lambda url: Manifest(Series('o', 'Other', platform_id='99'), (episode('81', 1, 'o'),))),
                ('another episode', lambda url: Manifest(Series('s', 'S', platform_id='42'), (episode('999', 1),))),
                ('several episodes', lambda url: Manifest(Series('s', 'S', platform_id='42'),
                                                          (episode('81', 1), episode('84', 4))))):
            store.reset_mock()
            self.adapter.capture_automatic.side_effect = make
            with self.subTest(label), self.assertRaises(ArchiveError):
                archive_series(self.adapter, "42")
            store.return_value.archive.assert_not_called()
            store.return_value.close.assert_called_once()
        self.adapter.capture_automatic.side_effect = original
        self.assertEqual(archive_series(self.adapter, "42")["imported_episode_ids"], ["81", "83"])


if __name__ == "__main__":
    unittest.main()
