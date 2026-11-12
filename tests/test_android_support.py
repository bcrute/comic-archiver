"""The phone's log and its Copy details and Save log for support. Private fixtures only."""
import logging
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import android_support, support_details, support_log
from comic_archive.dashboard_model import State
from test_android_cards import OpenedLibrary
from test_app_bridge import app_bridge

SYSTEM = 'Android 15 (API 35)'


class PhoneLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(self.detach)

    def detach(self):
        handler = android_support._kept['handler']
        for log in android_support._logs():
            if handler is not None:
                log.removeHandler(handler)
            log.propagate = True
        if handler is not None:
            handler.close()
        android_support._kept['handler'] = None

    def test_the_apps_and_the_downloads_messages_go_to_one_private_log(self):
        with patch.object(support_log, 'catch_unexpected') as caught:
            self.assertTrue(android_support.configure(self.root / 'log'))
        caught.assert_called_once_with(android_support.APP_LOG)
        logging.getLogger(android_support.APP_LOG).info('the app said this')
        logging.getLogger(support_log.DOWNLOAD_LOG).info('the download said this')
        logging.getLogger(support_log.DOWNLOAD_LOG).debug('too fine to keep')
        text = (self.root / 'log' / android_support.LOG_NAME).read_text()
        self.assertRegex(text, r'\d{4}-\d\d-\d\d .* INFO the app said this\n.* INFO the download said this\n$')
        self.assertNotIn('too fine', text)
        self.assertEqual((self.root / 'log' / android_support.LOG_NAME).stat().st_mode & 0o777, 0o600)
        # Starting it again adds no second copy of each line.
        with patch.object(support_log, 'catch_unexpected'):
            self.assertTrue(android_support.configure(self.root / 'log'))
        logging.getLogger(android_support.APP_LOG).info('once')
        self.assertEqual((self.root / 'log' / android_support.LOG_NAME).read_text().count('once'), 1)

    def test_a_log_that_cannot_be_kept_or_is_reached_through_a_link_is_not_written(self):
        (self.root / 'elsewhere').mkdir()
        (self.root / 'linked').symlink_to(self.root / 'elsewhere')
        with patch.object(support_log, 'catch_unexpected') as caught:
            self.assertFalse(android_support.configure(self.root / 'linked'))
            (self.root / 'log').mkdir()
            (self.root / 'log' / android_support.LOG_NAME).symlink_to(self.root / 'elsewhere' / 'target')
            self.assertFalse(android_support.configure(self.root / 'log'))
        caught.assert_not_called()
        logging.getLogger(android_support.APP_LOG).info('nowhere')
        self.assertEqual(list((self.root / 'elsewhere').iterdir()), [])

    def test_the_details_name_the_phones_system_and_say_which_sites_are_not_open(self):
        state = State()
        state.status = {'session_configured': True, 'episodes_by_status': {'archived': 3}}
        state.account = {'status': 'running', 'phase': 'download', 'speed': 'medium'}
        text = android_support.details('0.9.0-beta1', SYSTEM, [('Tapas', state), ('Webtoon', None)])
        self.assertEqual(text.splitlines(), [
            'Comic Archiver 0.9.0-beta1 on Android 15 (API 35)', '', '== Tapas', 'Sign-in: signed in',
            'Download: running / download', 'Speed: medium', 'Episodes: archived 3', '', '== Webtoon', 'Not opened yet'])
        # The desktop still names its own system.
        with patch.object(support_details, 'system', return_value='Linux 6'):
            self.assertEqual(support_details.text('1', []), 'Comic Archiver 1 on Linux 6')

    def test_a_saved_log_holds_the_details_each_open_sites_records_and_the_phones_log_without_its_folders(self):
        logs, out, base = self.root / 'files' / 'log', self.root / 'files' / 'out', self.root / 'files' / 'library'
        logs.mkdir(parents=True)
        (logs / 'phone.log.1').write_text('older line\n')
        (logs / 'phone.log').write_text(f'a message naming {base}/Webtoon/Library/x and {self.root}/files/other\n')
        (logs / 'desktop.log').write_text('not the phone log\n')
        now = datetime(2026, 11, 11, 9, 30, 5)
        asked = []

        def records(name, session, library):
            asked.append((name, session, library))
            return f'=== {name}: records in {library}'
        with patch.object(support_log, 'site_records', side_effect=records):
            path = android_support.save(out, logs, 'the details', [('Webtoon', base / 'Webtoon' / '.comic-session',
                                                                   base / 'Webtoon' / 'Library')],
                                        folders=[base], home=self.root / 'files', now=now)
        self.assertEqual(path, out / 'Comic Archiver log 2026-11-11 09.30.05.txt')
        self.assertEqual(asked, [('Webtoon', base / 'Webtoon' / '.comic-session', base / 'Webtoon' / 'Library')])
        self.assertEqual(path.read_text().split('\n\n'), [
            'Saved 2026-11-11T09:30:05; the times below are this phone’s own.', 'the details',
            '=== Webtoon: records in <library>/Library',
            '=== The app’s log\nolder line\na message naming <library>/Library/x and ~/other\n'])


class SupportBridgeTests(OpenedLibrary):
    def setUp(self):
        super().setUp()
        self.addCleanup(PhoneLogTests.detach, self)

    def test_the_log_is_started_in_the_folder_the_app_names(self):
        with patch.object(support_log, 'catch_unexpected'):
            started = self.call('support_start', log_root=str(self.root / 'log'))
        self.assertEqual(started['result'], {'logging': True})
        logging.getLogger(support_log.DOWNLOAD_LOG).info('a step')
        self.assertIn('INFO a step', (self.root / 'log' / 'phone.log').read_text())
        self.assertFalse(self.call('support_start', log_root='log')['ok'])
        self.assertFalse(self.call('support_start', log_root=str(self.root / 'log'), more=1)['ok'])

    def test_a_failure_nothing_expected_is_noted_in_the_log_without_its_own_text(self):
        with patch.object(support_log, 'catch_unexpected'):
            self.call('support_start', log_root=str(self.root / 'log'))
        with patch.dict(app_bridge.HANDLERS, {'all_sites': lambda args: (_ for _ in ()).throw(ValueError('PRIVATE'))}):
            failed = self.call('all_sites', base_root=str(self.root))
        self.assertEqual((failed['ok'], failed['code']), (False, 'job_failed'))
        text = (self.root / 'log' / 'phone.log').read_text()
        self.assertIn('WARNING all_sites did not finish: ValueError', text)
        self.assertNotIn('PRIVATE', text)

    def test_details_list_every_site_with_what_the_open_ones_cards_last_read(self):
        asked = {'base_root': str(self.root), 'version': '0.9.0-beta1', 'system': SYSTEM}
        before = self.call('support_details', **asked)['result']['text'].splitlines()
        self.assertEqual(before[:4], ['Comic Archiver 0.9.0-beta1 on Android 15 (API 35)', '', '== Tapas', 'Not opened yet'])
        self.assertTrue(self.call('site_cards', base_root=str(self.root))['ok'])
        text = self.call('support_details', **asked)['result']['text']
        self.assertIn('== Tapas\nSign-in: not signed in\nDownload: ', text)
        self.assertIn('== Webtoon\nNot opened yet', text)
        self.assertNotIn(str(self.root), text)
        for wrong in ({'version': 'a version\nwith a line'}, {'system': 'x' * 61}, {'system': '/data/user'}, {'more': 1}):
            self.assertFalse(self.call('support_details', **(asked | wrong))['ok'], wrong)

    def test_save_writes_one_file_in_the_folder_named_and_answers_its_name_only(self):
        with patch.object(support_log, 'catch_unexpected'):
            self.call('support_start', log_root=str(self.root / 'log'))
        logging.getLogger(android_support.APP_LOG).info('opened %s', self.root)
        asked = {'base_root': str(self.root), 'version': '0.9.0-beta1', 'system': SYSTEM,
                 'log_root': str(self.root / 'log'), 'out_root': str(self.root / 'out')}
        saved = self.call('support_save', **asked)
        self.assertTrue(saved['ok'], saved)
        self.assertEqual(set(saved['result']), {'name'})
        self.assertRegex(saved['result']['name'], r'^Comic Archiver log \d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d\.txt$')
        text = (self.root / 'out' / saved['result']['name']).read_text()
        self.assertIn('Comic Archiver 0.9.0-beta1 on Android 15 (API 35)', text)
        self.assertIn('=== Tapas: episodes not saved', text)
        self.assertIn('INFO opened <library>', text)
        self.assertNotIn(str(self.root), text)
        with patch.object(android_support, 'save', side_effect=OSError('PRIVATE')):
            refused = self.call('support_save', **asked)
        self.assertEqual((refused['ok'], refused['code']), (False, 'refused'))
        self.assertNotIn('PRIVATE', refused['message'])
        self.assertFalse(self.call('support_save', **(asked | {'out_root': 'out'}))['ok'])


if __name__ == '__main__':
    unittest.main()
