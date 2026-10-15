import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comic_archive.estimate import EPISODE_BYTES, EPISODES_PER_SERIES, pace, remaining
from comic_archive.inventory import Inventory
from comic_archive.monitoring import library_status


class PaceTests(unittest.TestCase):
    def test_the_hand_over_between_two_batches_is_downloading_and_a_longer_wait_is_not(self):
        # Newest first: two batches ten seconds apart, then one from before a wait for the connection.
        runs = [(1_000, 1_100, 10), (880, 990, 8), (500, 600, 9)]
        self.assertEqual(pace(runs), {'episodes': 27, 'seconds': 100 + 110 + 10 + 100})

    def test_only_the_most_recent_five_minutes_of_downloading_set_the_pace(self):
        # A minute a batch, as at the fast speed: the hour of slower batches before them no longer counts.
        runs = [(10_000 - index * 60, 10_060 - index * 60, 20) for index in range(5)]
        runs += [(9_700 - index * 600, 10_300 - index * 600, 5) for index in range(1, 7)]
        self.assertEqual(pace(runs), {'episodes': 100, 'seconds': 300})
        # One long batch, as at the slow speed, is taken whole: nothing finer is recorded.
        self.assertEqual(pace([(20_000, 21_800, 15), (18_000, 19_800, 2)]), {'episodes': 15, 'seconds': 1_800})

    def test_a_run_that_ends_before_it_starts_or_overlaps_a_newer_one_is_left_out(self):
        runs = [(5_000, 5_100, 4), (5_050, 5_090, 9), (4_000, 3_000, 9), (4_800, 4_900, 6)]
        self.assertEqual(pace(runs), {'episodes': 10, 'seconds': 200})
        self.assertEqual(pace([]), {'episodes': 0, 'seconds': 0})


class RemainingTests(unittest.TestCase):
    RECENT = {'episodes': 40, 'seconds': 3600}

    def test_known_waiting_episodes_at_the_recent_pace(self):
        value = remaining({'queued': 395, 'failed': 5, 'archived': 1000, 'unsupported': 70}, 30, 0, self.RECENT)
        self.assertEqual((value['waiting_episodes'], value['unchecked_series'], value['seconds']), (400, 0, 36_000))

    def test_a_series_not_checked_yet_is_taken_to_hold_what_a_series_usually_holds(self):
        # The few series checked first say little about the rest: four that hold 476 episodes between them
        # would put 913 more at over a hundred thousand.
        value = remaining({'queued': 291, 'archived': 185}, 917, 913, self.RECENT)
        self.assertEqual(value['seconds'], (291 + 913 * EPISODES_PER_SERIES) * 90)
        self.assertEqual((value['waiting_episodes'], value['unchecked_series']), (291, 913))
        # Before any series has been checked there is an estimate as well.
        self.assertEqual(remaining({}, 780, 780, self.RECENT)['seconds'], 780 * EPISODES_PER_SERIES * 90)

    def test_no_time_is_given_while_too_little_has_been_saved_to_know_the_pace(self):
        for recent in (None, {'episodes': 2, 'seconds': 3600}, {'episodes': 40, 'seconds': 299}, {'episodes': '40', 'seconds': 3600}):
            self.assertIsNone(remaining({'queued': 400}, 30, 0, recent)['seconds'], recent)
        self.assertEqual(remaining({'queued': 400}, 30, 0, {'episodes': 3, 'seconds': 300})['seconds'], 40_000)

    def test_counts_that_are_not_counts_are_read_as_nothing(self):
        value = remaining({'queued': -5, 'failed': None, 'archived': '7'}, 10, 99, self.RECENT, '5')
        self.assertEqual(value, {'waiting_episodes': 0, 'unchecked_series': 10, 'seconds': 10 * EPISODES_PER_SERIES * 90,
                                 'saved_bytes': 0, 'left_bytes': 10 * EPISODES_PER_SERIES * EPISODE_BYTES})


class SpaceTests(unittest.TestCase):
    def test_what_is_left_takes_a_usual_episodes_room_whatever_the_first_saved_episodes_took(self):
        value = remaining({'queued': 395, 'failed': 5, 'archived': 100}, 30, 0, None, 1_200_000_000)
        self.assertEqual((value['saved_bytes'], value['left_bytes'], value['seconds']), (1_200_000_000, 400 * EPISODE_BYTES, None))
        # A library of 917 series with four checked, which takes about 246 GB when it is all saved: the estimate may
        # be up to twice that, never under it, and not the 1.8 TB the four checked series would suggest.
        value = remaining({'queued': 291, 'archived': 185}, 917, 913, None, 3_040_000_000)
        self.assertEqual(value['left_bytes'], (291 + 913 * 24) * 20_000_000)
        self.assertTrue(246e9 <= value['saved_bytes'] + value['left_bytes'] <= 2 * 246e9)

    def test_the_library_adds_up_the_files_of_its_saved_episodes_and_the_status_adds_the_room_free_on_its_disk(self):
        with tempfile.TemporaryDirectory() as root:
            library = Path(root) / 'Library'
            with Inventory(library) as inventory:
                inventory.observe('42', 'Fixture', [{'id': str(key), 'title': 'E', 'order': key, 'access_state': 'tapas_free'}
                                                    for key in (1, 2, 3, 4)], 'fixture')
                (library / 'Fixture').mkdir()
                for key, size in ((1, 700), (2, 1300)):
                    path = library / 'Fixture' / f'{key}.cbz'
                    path.write_bytes(b'x' * size); path.with_suffix('.json').write_text('{}')
                    inventory.mark(str(key), 'archived', path=path)
                self.assertEqual(inventory.saved_bytes(), 2000)
                # A file that was saved and is gone again is not room taken; an episode without a size on record adds none.
                inventory.mark('2', 'queued')
                inventory.db.execute("UPDATE episodes SET status='archived' WHERE id='3'")
                self.assertEqual(inventory.saved_bytes(), 700)
                inventory.db.commit()
            with patch('comic_archive.monitoring.shutil.disk_usage') as usage:
                usage.return_value.free = 123_456
                estimate = library_status(library)['estimate']
                self.assertEqual(usage.call_args.args, (library,))
                # Episodes 2 and 4 are waiting; the one series has not been checked.
                self.assertEqual((estimate['saved_bytes'], estimate['left_bytes'], estimate['free_bytes']),
                                 (700, (2 + EPISODES_PER_SERIES) * EPISODE_BYTES, 123_456))
                usage.side_effect = OSError('PRIVATE')
                self.assertIsNone(library_status(library)['estimate']['free_bytes'])


class RecentRunsTests(unittest.TestCase):
    def test_finished_runs_come_newest_first_with_the_episodes_each_saved(self):
        with tempfile.TemporaryDirectory() as root, Inventory(Path(root) / 'Library') as inventory:
            for summary in ({'archived_episode_ids': ['1', '2', '3']}, {'status': 'paused'}, {'archived_episode_ids': 'PRIVATE'}):
                inventory.finish_run(inventory.start_run(), 'finished', summary)
            inventory.start_run()  # Still running: not counted.
            inventory.db.execute("UPDATE runs SET summary='{bad' WHERE summary LIKE '%paused%'")
            runs = list(inventory.recent_runs())
            self.assertEqual([saved for _, _, saved in runs], [0, 0, 3])
            self.assertTrue(all(started <= finished for started, finished, _ in runs))
            self.assertEqual(runs, sorted(runs, reverse=True))
            self.assertEqual(inventory.status()['recent_pace']['episodes'], 3)


if __name__ == '__main__':
    unittest.main()
