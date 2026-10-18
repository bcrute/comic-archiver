import os
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE
from comic_archive import osdep


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.new, self.old = Path(folder.name) / 'status.new', Path(folder.name) / 'status'
        self.new.write_text('new'); self.old.write_text('old')

    def test_on_windows_a_file_a_reader_holds_open_is_replaced_once_the_reader_lets_go(self):
        real = os.replace; refusals = [PermissionError(13, 'in use'), PermissionError(13, 'in use')]
        def held_open(source, target):
            if refusals: raise refusals.pop()
            return real(source, target)
        with patch.object(osdep, 'WINDOWS', True), patch.object(os, 'replace', held_open), patch.object(osdep.time, 'sleep'):
            osdep.replace_file(self.new, self.old)
        self.assertEqual(self.old.read_text(), 'new'); self.assertFalse(self.new.exists())

    def test_on_windows_a_file_that_stays_held_is_reported_and_other_failures_are_not_waited_on(self):
        with patch.object(osdep, 'WINDOWS', True), patch.object(osdep, 'REPLACE_WAIT_SECONDS', .05):
            with patch.object(os, 'replace', side_effect=PermissionError(13, 'in use')) as tried:
                with self.assertRaises(PermissionError): osdep.replace_file(self.new, self.old)
            self.assertGreater(tried.call_count, 1)
            with patch.object(os, 'replace', side_effect=FileNotFoundError(2, 'gone')) as tried:
                with self.assertRaises(FileNotFoundError): osdep.replace_file(self.new, self.old)
            tried.assert_called_once()
        self.assertEqual(self.old.read_text(), 'old')

    @unittest.skipIf(osdep.WINDOWS, 'on Windows a refusal is waited on')
    def test_off_windows_a_refusal_is_a_folder_that_cannot_be_written_and_is_reported_at_once(self):
        with patch.object(os, 'replace', side_effect=PermissionError(13, 'read-only')) as tried:
            with self.assertRaises(PermissionError): osdep.replace_file(self.new, self.old)
        tried.assert_called_once()


class EnvironmentTests(unittest.TestCase):
    def test_a_python_the_app_starts_reads_text_as_utf8_and_takes_nothing_from_the_persons_own_python(self):
        environ = {'PYTHONPATH': 'C:\\mine', 'PYTHONHOME': 'C:\\Python39', 'PYTHONUTF8': '0', 'PATH': 'kept'}
        osdep.python_environment(environ)
        self.assertEqual(environ, {'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONSAFEPATH': '1', 'PATH': 'kept'})


class FileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_path_that_names_its_own_start_is_told_from_one_inside_a_folder(self):
        for inside in ('Series/one.cbz', 'one.cbz', '.hidden/x'):
            self.assertFalse(osdep.anchored(inside), inside)
        self.assertTrue(osdep.anchored('/etc/passwd'))
        # As Windows reads them: the first two are not absolute paths there, and each leaves the folder it is joined to.
        for outside in ('/Windows/system.ini', 'C:system.ini', 'C:/Windows', '//server/share/x'):
            self.assertTrue(osdep.anchored(PureWindowsPath(outside)), outside)
        self.assertFalse(osdep.anchored(PureWindowsPath('Series/one.cbz')))

    def test_private_files_use_unix_permissions_but_do_not_need_windows_fchmod(self):
        path = self.folder / 'private'; path.touch()
        with path.open('wb') as stream:
            with patch.object(osdep, 'WINDOWS', False), patch.object(os, 'fchmod', create=True) as restrict:
                osdep.private_file(stream.fileno())
                restrict.assert_called_once_with(stream.fileno(), 0o600)
            with patch.object(osdep, 'WINDOWS', True), patch.dict(os.__dict__):
                os.__dict__.pop('fchmod', None)
                osdep.private_file(stream.fileno())
                from comic_archive.session import store_session
                from comic_archive.locking import LibraryLock
                with LibraryLock(self.folder / 'Library'):
                    store_session({'cookies':[], 'origins':[]}, self.folder / 'session')
                self.assertTrue((self.folder / 'session/state.json').is_file())

    def test_a_finished_file_is_flushed_through_a_handle_the_system_accepts(self):
        path = self.folder / 'episode.cbz'; path.write_bytes(b'saved')
        with patch.object(osdep.os, 'fsync', wraps=os.fsync) as flushed:
            osdep.sync_file(path)  # The real call: Windows refuses to flush through a handle that only reads.
        flushed.assert_called_once()
        self.assertEqual(path.read_bytes(), b'saved')

    def test_two_processes_given_the_same_log_each_add_to_its_end(self):
        log = self.folder / 'background.log'
        first, second = osdep.open_log(log), osdep.open_log(log)
        self.addCleanup(os.close, first); self.addCleanup(os.close, second)
        for descriptor, text in ((first, b'worker started\n'), (second, b'controller started\n'), (first, b'worker busy\n')):
            os.write(descriptor, text)
        self.assertEqual(log.read_bytes(), b'worker started\ncontroller started\nworker busy\n')
        if not osdep.WINDOWS:
            self.assertEqual(log.stat().st_mode & 0o777, PRIVATE_FILE)

    def test_the_room_a_file_takes_is_what_its_disk_gives_it_and_its_length_where_the_disk_does_not_say(self):
        path = self.folder / 'page.bin'; path.write_bytes(b'x' * 5000)
        if hasattr(os.stat_result, 'st_blocks'):
            self.assertEqual(osdep.disk_bytes(path), path.stat().st_blocks * 512)
        class Windows:
            st_size = 5000
        with patch.object(os, 'stat', return_value=Windows()):
            self.assertEqual(osdep.disk_bytes(path), 5000)


class WindowsMarkTests(unittest.TestCase):
    """Windows' own calls, stood in for on either operating system."""

    def kernel(self):
        kernel = Mock()
        wrapper = patch('ctypes.WinDLL', create=True, return_value=kernel); wrapper.start(); self.addCleanup(wrapper.stop)
        return kernel

    def test_on_windows_a_private_folder_is_marked_hidden_keeping_its_other_marks(self):
        kernel = self.kernel()
        folder = Path('C:/Users/me/Comic Archiver/.comic-session')
        with patch.object(osdep, 'WINDOWS', True):
            kernel.GetFileAttributesW.return_value = 0x10 | 0x20  # A folder, marked for backup.
            osdep.hide(folder)
            kernel.SetFileAttributesW.assert_called_once_with(str(folder), 0x10 | 0x20 | 0x2)
            # Already hidden, or not there to be asked about: nothing is changed.
            for marks in (0x12, 0xFFFFFFFF):
                kernel.SetFileAttributesW.reset_mock(); kernel.GetFileAttributesW.return_value = marks
                osdep.hide(folder)
                kernel.SetFileAttributesW.assert_not_called()
            # A folder that cannot be marked is only shown.
            kernel.GetFileAttributesW.side_effect = OSError('denied')
            osdep.hide(folder)
        kernel.reset_mock(side_effect=True)
        with patch.object(osdep, 'WINDOWS', False):
            osdep.hide(folder)  # Elsewhere a leading dot hides it already.
        kernel.GetFileAttributesW.assert_not_called(); kernel.SetFileAttributesW.assert_not_called()

    def test_on_windows_the_computer_is_kept_awake_for_a_download_and_let_sleep_again_with_the_screen_free_to_turn_off(self):
        kernel = self.kernel()
        with patch.object(osdep, 'WINDOWS', True):
            osdep.keep_awake(True)
            osdep.keep_awake(False)
            self.assertEqual([call.args for call in kernel.SetThreadExecutionState.call_args_list], [(0x80000001,), (0x80000000,)])
            kernel.SetThreadExecutionState.side_effect = OSError('refused')
            osdep.keep_awake(True)  # Asking is all the app can do.
        kernel.reset_mock(side_effect=True)
        with patch.object(osdep, 'WINDOWS', False):
            osdep.keep_awake(True)
        kernel.SetThreadExecutionState.assert_not_called()


if __name__ == '__main__':
    unittest.main()
