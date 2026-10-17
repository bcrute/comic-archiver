import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive import locking
from comic_archive.errors import LibraryBusy
from comic_archive.locking import LibraryLock, wait_for_library
from comic_archive.models import ArchiveError


def in_another_thread(action):
    """Run action in a second thread of this process; return what it raised, or None."""
    outcome = []
    def run():
        try:
            action()
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        raise AssertionError('The helper thread did not finish')
    return outcome[0]


def enter_and_leave(root):
    with LibraryLock(root):
        pass


class LockingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    def test_wait_rejects_a_bad_duration_before_touching_the_library(self):
        for seconds in (-1, float('nan'), float('inf'), True, '5', None):
            with self.subTest(seconds=seconds), self.assertRaises(ArchiveError):
                with wait_for_library(self.root, seconds):
                    pass
        self.assertFalse(self.root.exists())

    def test_a_lock_held_by_another_job_is_refused_when_no_waiting_is_allowed(self):
        with LibraryLock(self.root):
            error = in_another_thread(lambda: wait_for_library(self.root, 0).__enter__())
            self.assertIsInstance(error, LibraryBusy)
        self.assertIsNone(in_another_thread(lambda: enter_and_leave(self.root)))

    def test_leaving_a_lock_that_was_never_entered_does_not_release_the_holders_lock(self):
        with LibraryLock(self.root):
            LibraryLock(self.root).__exit__(None, None, None)
            self.assertIsInstance(in_another_thread(lambda: enter_and_leave(self.root)), LibraryBusy)
        self.assertIsNone(in_another_thread(lambda: enter_and_leave(self.root)))

    def test_the_librarys_private_state_folder_is_hidden_from_the_file_manager(self):
        with patch.object(locking.osdep, 'hide') as hide:
            enter_and_leave(self.root)
        hide.assert_called_once_with(self.root.absolute() / '.archive-state')

    def test_a_failure_while_taking_the_lock_leaves_nothing_held(self):
        seen = []
        def failing(descriptor):
            seen.append(descriptor)
            raise OSError(5, 'disk fault')
        with patch.object(locking.osdep, 'lock', side_effect=failing):
            with self.assertRaises(OSError):
                LibraryLock(self.root).__enter__()
        with self.assertRaises(OSError):
            os.fstat(seen[0])  # The descriptor was closed, not leaked.
        enter_and_leave(self.root)
        self.assertIsNone(in_another_thread(lambda: enter_and_leave(self.root)))


if __name__ == '__main__':
    unittest.main()
