"""The log a person saves for support: everything the app did, in one file, with nothing private in it."""
from datetime import datetime, timezone
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import comic_archive
from comic_archive import osdep, support_log
from comic_archive.inventory import Inventory
from comic_archive.workspace import Workspace


class SupportLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_each_line_the_downloader_writes_begins_with_the_time(self):
        out = io.StringIO()
        stamped = support_log.Stamped(out, clock=lambda: (2026, 11, 4, 10, 45, 14, 2, 308, 0))
        self.assertEqual(stamped.write('Starting task one\nhalf'), 22)
        stamped.write(' a line\n')
        stamped.write('')
        stamped.write('two\nthree\n')
        self.assertEqual(out.getvalue().splitlines(), ['2026-11-04 10:45:14 Starting task one',
                                                       '2026-11-04 10:45:14 half a line',
                                                       '2026-11-04 10:45:14 two', '2026-11-04 10:45:14 three'])
        self.assertEqual(stamped.getvalue(), out.getvalue())  # Anything else is the stream's own.

    def test_the_downloaders_process_stamps_its_output_logs_its_download_and_writes_any_character(self):
        log = self.home / 'background.log'
        code = ('import logging; from comic_archive import support_log; support_log.downloader_output("9.9.9"); '
                'print("Starting task", flush=True); '
                'logging.getLogger("comic_archive.download").info("download: begun (Go)"); '
                'logging.getLogger("comic_archive.download").debug("not this"); '
                'print("caf\\u00e9 \\u2019", flush=True); import sys; sys.stderr.write("Traceback\\n")')
        environment = {**os.environ, 'PYTHONPATH': str(Path(comic_archive.__file__).parents[1]), 'PYTHONIOENCODING': 'cp1252'}
        with open(log, 'wb') as output:
            subprocess.run([sys.executable, '-c', code], stdout=output, stderr=subprocess.STDOUT, env=environment,
                           check=True, timeout=60)
        lines = log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 5, lines)
        for line in lines:
            self.assertRegex(line, r'^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} ')
        self.assertRegex(lines[0], r' Downloader of Comic Archiver 9\.9\.9 starting on \S.*, Python 3\.[0-9]+\.[0-9]+\.$')
        self.assertEqual([line[20:] for line in lines[1:]],
                         ['Starting task', 'INFO download: begun (Go)', 'café ’', 'Traceback'])

    def test_the_downloaders_log_is_started_afresh_past_its_size_keeping_the_last_two(self):
        log = self.home / 'background.log'
        log.write_text('small')
        support_log.trim(log, limit=10)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['background.log'])
        for text in ('first log!!', 'second log!', 'third log!!'):
            log.write_text(text)
            support_log.trim(log, limit=10)
        self.assertEqual({p.name: p.read_text() for p in self.home.iterdir()},
                         {'background.log.1': 'third log!!', 'background.log.2': 'second log!'})
        support_log.trim(self.home / 'missing.log')  # Nothing yet: nothing to do.
        log.write_text('fourth log!')
        with patch('os.replace', side_effect=PermissionError(13, 'in use')):
            support_log.trim(log, limit=10)  # It grows on; the next start tries again.
        self.assertEqual(log.read_text(), 'fourth log!')

    def test_folders_are_left_out_however_they_are_spelled(self):
        library = self.home / 'My Comics'
        text = (f'opened {library}/tapas/Library\nin {self.home}/.local\n'
                f"windows {str(library).replace('/', chr(92))}{chr(92)}x")
        scrubbed = support_log.scrub(text, [library], home=self.home)
        self.assertEqual(scrubbed, 'opened <library>/tapas/Library\nin ~/.local\nwindows <library>\\x')
        with patch.object(support_log, 'WINDOWS', True):
            self.assertEqual(support_log.scrub('C:\\USERS\\SOMEONE\\AppData', [], home='C:\\Users\\Someone'), '~\\AppData')
        # A folder as short as a drive would match everywhere: it is left alone.
        self.assertEqual(support_log.scrub('C:\\x', ['C:\\'], home='/'), 'C:\\x')
        # A Mac's folders are the same in any case, as on Windows; elsewhere another case is another folder.
        with patch.object(support_log, 'WINDOWS', False), patch.object(support_log, 'sys', Mock(platform='darwin')):
            self.assertEqual(support_log.scrub('/users/SOMEONE/Library', [], home='/Users/Someone'), '~/Library')
        with patch.object(support_log, 'WINDOWS', False), patch.object(support_log, 'sys', Mock(platform='linux')):
            self.assertEqual(support_log.scrub('/HOME/someone/x', [], home='/home/someone'), '/HOME/someone/x')
            # The end of the home folder's path written under another start still names the person.
            self.assertEqual(support_log.scrub('at /Users/someone/Library, not /Users/someone-else or /Users/someone2',
                                               [], home='/Volumes/Data/Users/someone'),
                             'at ~/Library, not /Users/someone-else or /Users/someone2')
            self.assertEqual(support_log.scrub('/x/y', [], home='/x'), '/x/y')

    def site(self):
        workspace = Workspace('default', self.home)
        with Inventory(workspace.library) as inventory:
            inventory.observe('42', 'Private Series', [
                {'id': '81', 'title': 'Private Episode', 'order': 1, 'access_state': 'tapas_free'},
                {'id': '82', 'title': 'Saved', 'order': 2, 'access_state': 'tapas_free'}], 'fixture')
            inventory.mark('81', 'unsupported', 'content_unsupported')
            inventory.mark('82', 'archived')
        return workspace

    def test_a_sites_records_list_its_episodes_not_saved_by_number_its_steps_and_its_downloaders_log(self):
        workspace = self.site()
        worker = workspace.session / 'worker'
        worker.mkdir(parents=True)
        (worker / 'background.log.2').write_text('oldest\n')
        (worker / 'background.log.1').write_text(
            '[1:2:1104/104514.760892:ERROR:ssl_client_socket_impl.cc:915] handshake failed; returned -1, SSL error code 1\n'
            'older\n')
        (worker / 'background.log').write_text('newest\n')
        text = support_log.site_records('Tapas', workspace.session, workspace.library)
        lines = text.splitlines()
        self.assertEqual(lines[:2], ['=== Tapas: episodes not saved', 'series 42 episode 81: unsupported content_unsupported, '
                                     f"tries 0, access tapas_free, checked {lines[1].rpartition(' ')[2]}"])
        self.assertIn('=== Tapas: the download’s last steps, oldest first\nNo download yet', text)
        self.assertTrue(text.endswith('=== Tapas: the downloader’s log\noldest\nolder\nnewest\n'
                                      '(1 routine messages from the browser engine left out)'), text)
        self.assertNotIn('Private', text)
        self.assertNotIn('episode 82', text)

    def test_a_downloaders_log_reached_through_a_link_is_left_out(self):
        # A library folder from elsewhere could link its log to the sign-in saved beside it.
        workspace = self.site()
        worker = workspace.session / 'worker'; worker.mkdir(parents=True)
        secret = workspace.session / 'state.json'; secret.write_text('{"cookies": "SECRET"}')
        (worker / 'background.log.1').write_text('older\n')
        (worker / 'background.log').symlink_to(secret)
        text = support_log.site_records('Tapas', workspace.session, workspace.library)
        self.assertTrue(text.endswith('=== Tapas: the downloader’s log\nolder'), text)
        elsewhere = self.home / 'elsewhere'; elsewhere.mkdir()
        (elsewhere / 'background.log').write_text('ELSEWHERE\n')
        worker.rename(workspace.session / 'old-worker'); worker.symlink_to(elsewhere)
        text = support_log.site_records('Tapas', workspace.session, workspace.library)
        self.assertTrue(text.endswith('=== Tapas: the downloader’s log\n'), text)
        self.assertNotIn('ELSEWHERE', text)

    def test_a_record_that_cannot_be_read_is_said_and_the_rest_still_comes(self):
        workspace = self.site()
        with patch('comic_archive.download_control.history', side_effect=RuntimeError('PRIVATE')):
            text = support_log.site_records('Tapas', workspace.session, workspace.library)
        self.assertRegex(text, r'last steps, oldest first\nCould not be read: RuntimeError at [a-z_]+\.py:[0-9]+')
        self.assertIn('series 42 episode 81', text)
        self.assertNotIn('PRIVATE', text)
        empty = Workspace('other', self.home / 'other')
        self.assertTrue(support_log.site_records('Webtoon', empty.session, empty.library).startswith(
            '=== Webtoon: episodes not saved\nNone recorded yet'))

    def test_a_downloads_steps_say_where_it_stands_and_what_each_step_did(self):
        state = {'status': 'held', 'code': 'archive_incomplete', 'stage': 'verify', 'updated_at': '2026-11-04T10:00:00+00:00',
                 'wait_until': None, 'idle_waits': 0, 'network_waits': 2, 'source_probes': 0, 'failed_retries': 1,
                 'episodes_waiting': 0, 'audit': {'missing_recorded_accessible_episode_count': 50}}
        steps = [{'task_id': 'job-' + 'a' * 24, 'kind': 'download', 'reserved_at': '2026-11-04T09:00:00+00:00',
                  'published_at': '2026-11-04T09:00:01+00:00', 'finished_at': '2026-11-04T09:10:00+00:00', 'outcome': 'ok',
                  'pages': 30, 'bytes': 9000, 'episodes': 2, 'series_checked': None, 'progressed': 1},
                 {'task_id': 'job-' + 'b' * 24, 'kind': 'audit', 'reserved_at': '2026-11-04T09:11:00+00:00',
                  'published_at': None, 'finished_at': None, 'outcome': None, 'pages': None, 'bytes': None,
                  'episodes': None, 'series_checked': None, 'progressed': None}]
        with patch('comic_archive.download_control.history', return_value=(state, steps)):
            lines = support_log._steps(Path('s'), Path('l'))
        self.assertEqual(lines, [
            'Now: held/archive_incomplete at verify since 2026-11-04T10:00:00+00:00',
            'Tries: idle_waits 0, network_waits 2, source_probes 0, failed_retries 1, episodes waiting 0',
            'Last check: missing_recorded_accessible_episode_count 50',
            f"2026-11-04T09:00:00+00:00 job-{'a' * 24} download: ok at 2026-11-04T09:10:00+00:00; episodes 2, pages 30, "
            'bytes 9000; progressed',
            f"2026-11-04T09:11:00+00:00 job-{'b' * 24} audit: not sent yet"])

    def test_the_app_log_is_read_oldest_first_with_the_last_crash(self):
        for name, text in (('desktop.log.2', 'two\n'), ('desktop.log.1', 'one\n'), ('desktop.log', 'now\n')):
            (self.home / name).write_text(text)
        self.assertEqual(support_log.app_log(self.home), 'two\none\nnow\n')
        (self.home / 'crash.log').write_text('Fatal Python error: Segmentation fault\n')
        self.assertTrue(support_log.app_log(self.home).endswith(
            'now\n\n=== Where the app was when it last stopped suddenly\nFatal Python error: Segmentation fault\n'))
        (self.home / 'desktop.log').write_text('x' * 50 + '\nlast line\n')
        with patch.object(support_log, 'APP_LOG_BYTES', 20):
            self.assertTrue(support_log.app_log(self.home).startswith('last line\n'))

    def test_one_file_holds_it_all_named_for_when_it_was_saved_and_never_too_large(self):
        when = datetime(2026, 11, 4, 10, 45, 14, tzinfo=timezone.utc)
        text = support_log.bundle('Comic Archiver 0.3.0 on Linux', ['=== Tapas: x'], 'app', [self.home / 'lib'], now=when)
        self.assertEqual(text, 'Saved 2026-11-04T10:45:14+00:00; the times below are this computer’s own.\n\n'
                               'Comic Archiver 0.3.0 on Linux\n\n=== Tapas: x\n\n=== The app’s log\napp')
        # Too much to send: each part gives up its start, never its end, where the newest lines are.
        with patch.object(support_log, 'LIMIT', 400):
            text = support_log.bundle('details', ['=== Tapas: x\n' + 'old line\n' * 50 + 'newest site'],
                                      'a\n' * 300 + 'newest app', [], now=when)
        self.assertLessEqual(len(text), 400)
        self.assertGreater(len(text), 380)
        self.assertTrue(text.startswith('Saved 2026-11-04T10:45:14+00:00;') and text.endswith('\na\nnewest app'))
        self.assertIn(f'\n\ndetails\n\n=== Tapas: x\n{support_log.SHORTENED}\nold line\n', text)
        self.assertIn(f'old line\nnewest site\n\n=== The app’s log\n{support_log.SHORTENED}\na\n', text)
        with patch.object(support_log, 'LIMIT', 60):  # No room for more than the names: still never over.
            self.assertLessEqual(len(support_log.bundle('d' * 100, ['=== Tapas: x\ny'], 'app', [], now=when)), 60)
        folder = self.home / 'Downloads'
        first = support_log.save(folder, 'é', now=when)
        second = support_log.save(folder, 'again', now=when)
        self.assertEqual((first.name, second.name), ('Comic Archiver log 2026-11-04 10.45.14.txt',
                                                     'Comic Archiver log 2026-11-04 10.45.14 (2).txt'))
        self.assertEqual(first.read_text(encoding='utf-8'), 'é')

    def test_an_error_nothing_caught_is_logged_by_class_and_places_never_its_text(self):
        before, thread_before = Mock(), Mock()
        with patch.object(support_log, '_hooked', False), patch('sys.excepthook', before), \
                patch('threading.excepthook', thread_before):
            support_log.catch_unexpected('comic_archive.test')
            support_log.catch_unexpected('comic_archive.test')  # Once only, however often the log is set up.
            hook, thread_hook = sys.excepthook, threading.excepthook
            try:
                raise ValueError('PRIVATE_COOKIE')
            except ValueError as error:
                caught = error
            with self.assertLogs('comic_archive.test', 'CRITICAL') as logs:
                hook(ValueError, caught, caught.__traceback__)
                thread_hook(Mock(exc_type=ValueError, exc_traceback=caught.__traceback__))
                hook(KeyboardInterrupt, KeyboardInterrupt(), None)  # Stopped on purpose: nothing to log.
        self.assertEqual(len(logs.records), 2)
        self.assertRegex(logs.records[0].getMessage(),
                         r'^ValueError that nothing caught\n  at test_support_log\.py:[0-9]+ in test_an_error')
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertEqual(before.call_count, 2)  # Python still reports it as it did.
        thread_before.assert_called_once()

    def test_a_crash_of_the_app_itself_is_written_to_a_small_file_of_its_own(self):
        crash = self.home / 'crash.log'
        crash.write_text('x' * (support_log.CRASH_BYTES + 1))
        with patch.object(support_log, '_crashes', None), patch('faulthandler.enable') as enable:
            support_log.catch_crashes(crash)
            opened = support_log._crashes
            self.addCleanup(opened.close)
            support_log.catch_crashes(crash)
        enable.assert_called_once_with(opened, all_threads=True)
        self.assertEqual(crash.stat().st_size, 0)  # A large one from before starts afresh.
        link = self.home / 'link.log'
        link.symlink_to(self.home / 'elsewhere')
        with patch.object(support_log, '_crashes', None), patch('faulthandler.enable') as enable:
            support_log.catch_crashes(link)
        enable.assert_not_called()

    def test_a_place_says_the_kind_of_drive_its_file_system_and_the_room_left(self):
        with patch.object(support_log, 'drive', return_value=('removable', 'exfat')), \
                patch('shutil.disk_usage', return_value=Mock(free=12_345_000_000)):
            self.assertEqual(support_log.place(self.home), 'removable drive, exfat, 12.3 GB free')
        with patch.object(support_log, 'drive', return_value=('unknown', 'unknown')), \
                patch('shutil.disk_usage', side_effect=OSError):
            self.assertEqual(support_log.place(self.home), 'unknown drive, unknown, room left unknown')


class DriveTests(unittest.TestCase):
    def setUp(self):
        # These fixtures describe Linux's mount table; native Mac volumes have their own adapter tests.
        platform = patch.object(osdep.sys, 'platform', 'linux')
        platform.start()
        self.addCleanup(platform.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.mounts = self.root / 'mounts'
        self.blocks = self.root / 'class/block'
        devices = self.root / 'devices'
        for disk, removable, bus in (('sda', '0', 'pci'), ('sdb', '0', 'pci/usb1'), ('sdc', '1', 'pci')):
            folder = devices / bus / disk
            (folder / f'{disk}1').mkdir(parents=True)
            (folder / 'removable').write_text(removable + '\n')
            self.blocks.mkdir(parents=True, exist_ok=True)
            (self.blocks / f'{disk}1').symlink_to(folder / f'{disk}1')

    def drive(self, path, mounts):
        self.mounts.write_text(''.join(f'{line}\n' for line in mounts))
        return osdep.drive(path, self.mounts, self.blocks)

    def test_the_drive_a_folder_is_on_is_the_deepest_mount_holding_it(self):
        home = Path('/home/someone')
        mounts = ['/dev/sda1 / ext4 rw 0 0', f'tmpfs {Path("/tmp").resolve()} tmpfs rw 0 0',
                  f'/dev/sdb1 {self.root}/media/USB\\040Drive exfat rw 0 0',
                  f'//server/share {self.root}/media/nas cifs rw 0 0', f'/dev/sdc1 {self.root}/media/card vfat rw 0 0']
        self.assertEqual(self.drive(home, mounts), ('local', 'ext4'))
        self.assertEqual(self.drive(self.root / 'media/USB Drive/Comics', mounts), ('removable', 'exfat'))
        self.assertEqual(self.drive(self.root / 'media/nas/Comics', mounts), ('network', 'cifs'))
        self.assertEqual(self.drive(self.root / 'media/card', mounts), ('removable', 'vfat'))
        self.assertEqual(self.drive('/tmp/x', mounts), ('local', 'tmpfs'))
        self.assertEqual(self.drive(home, []), ('unknown', 'unknown'))
        self.assertEqual(self.drive(home, ['/dev/mapper/gone / btrfs rw 0 0']), ('local', 'btrfs'))
        self.assertEqual(osdep.drive(home, self.root / 'no-mounts', self.blocks), ('unknown', 'unknown'))

    def test_on_windows_the_drive_is_asked_of_windows_and_an_answer_it_cannot_give_is_unknown(self):
        with patch.object(osdep, 'WINDOWS', True), patch.object(osdep, '_windows_drive', return_value=('network', 'NTFS')):
            self.assertEqual(osdep.drive('Z:\\Comics'), ('network', 'NTFS'))
        with patch.object(osdep, 'WINDOWS', True), patch.object(osdep, '_windows_drive', side_effect=AttributeError):
            self.assertEqual(osdep.drive('C:\\Comics'), ('unknown', 'unknown'))


class LoggingSetupTests(unittest.TestCase):
    def test_the_download_log_says_nothing_until_a_process_gives_it_somewhere_to_write(self):
        log = logging.getLogger(support_log.DOWNLOAD_LOG)
        from comic_archive import download_control
        self.assertIs(download_control.LOG, log)
        self.assertTrue(any(isinstance(handler, logging.NullHandler) for handler in log.handlers))


if __name__ == '__main__':
    unittest.main()
