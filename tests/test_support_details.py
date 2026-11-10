"""The details a person copies for support: enough to see why a download fell short, and nothing private."""
import unittest
from unittest import mock

from comic_archive import support_details
from comic_archive.dashboard_model import State


def state(**changes):
    values = dict(loaded=True, site_auth={'status': 'idle'},
                  status={'session_configured': True, 'worker': {'reported_state': 'ready'},
                          'episodes_by_status': {'archived': 900, 'failed': 50, 'unavailable': 12},
                          'unsupported_by_reason': {'novel': 2}, 'unsaved_by_code': {'failed:job_failed': 50},
                          'series_count': 40, 'scan_remaining_series_count': 0, 'series_scan_gap_count': 1,
                          'series_without_scan_checkpoint_count': 0, 'series_scan_gaps': [{'title': 'A Private Series'}],
                          'last_run': {'status': 'completed', 'id': 'run-1'}},
                  account={'status': 'held', 'phase': 'verify', 'code': 'archive_incomplete', 'speed': 'normal',
                           'audit': {'missing_recorded_accessible_episode_count': 50, 'verified_inventory_episode_count': 900}})
    values.update(changes)
    return State(**values)


class SupportDetailsTests(unittest.TestCase):
    def test_a_site_says_why_its_download_stopped_and_how_many_episodes_are_in_each_state(self):
        with mock.patch.object(support_details, 'system', return_value='Windows 10 (build 22631)'):
            text = support_details.text('0.3.0', [('Tapas', state()), ('Webtoon', None)])
        self.assertEqual(text.splitlines(), [
            'Comic Archiver 0.3.0 on Windows 10 (build 22631)',
            '',
            '== Tapas',
            'Sign-in: signed in (sign-in idle)',
            'Download: held / verify / archive_incomplete',
            'Downloader: ready',
            'Speed: normal',
            'Episodes: archived 900, failed 50, unavailable 12',
            'Not downloaded: novel 2',
            'Problems: failed:job_failed 50',
            'Last check: missing_recorded_accessible_episode_count 50, verified_inventory_episode_count 900',
            'Series: known 40, to check 0, with gaps 1, never checked 0',
            'Last step: completed',
            '',
            '== Webtoon',
            'Not opened yet'])

    def test_only_the_apps_own_codes_and_counts_go_in(self):
        private = state(status={'session_configured': False, 'episodes_by_status': {'My Series': 3, 'queued': -1, 'failed': 'x'},
                                'unsaved_by_code': {'failed:Some Title': 1, 'queued:interrupted': 2}},
                        account={'status': 'held', 'code': 'C:\\Users\\someone'}, site_auth={'status': 'error: someone@example.com'},
                        offline=True)
        text = support_details.text('0.3.0', [('Tapas', private)])
        for word in ('My Series', 'Some Title', 'Users', 'someone', 'A Private Series', 'run-1'):
            self.assertNotIn(word, text)
        self.assertIn('Sign-in: not signed in\n', text)
        self.assertIn('Download: held\n', text)
        self.assertIn('Problems: queued:interrupted 2\n', text)
        self.assertNotIn('Episodes:', text)
        self.assertTrue(text.endswith('Status updates: not arriving'))
        self.assertIn('Not opened yet', support_details.text('0.3.0', [('Tapas', State())]))

    def test_the_details_fit_in_one_message(self):
        many = state(status={'episodes_by_status': {f'code_{i}': i for i in range(2000)}})
        text = support_details.text('0.3.0', [('Tapas', many)])
        self.assertLessEqual(len(text), support_details.LIMIT)
        self.assertTrue(text.endswith('\n(shortened)'))
        self.assertLess(len(support_details.text('0.3.0', [('Tapas', state())])), support_details.LIMIT)

    def test_windows_says_its_build_so_windows_11_is_told_from_10(self):
        with mock.patch('platform.system', return_value='Windows'), mock.patch('platform.release', return_value='10'), \
                mock.patch('platform.version', return_value='10.0.22631'):
            self.assertEqual(support_details.system(), 'Windows 10 (build 22631)')
        with mock.patch('platform.system', return_value='Linux'), mock.patch('platform.release', return_value='6.17.0'):
            self.assertEqual(support_details.system(), 'Linux 6.17.0')
        with mock.patch('platform.system', return_value='Darwin'), mock.patch('platform.mac_ver', return_value=('15.8.2', (), '')):
            self.assertEqual(support_details.system(), 'macOS 15.8.2')


if __name__ == '__main__':
    unittest.main()
