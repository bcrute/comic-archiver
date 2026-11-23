"""The worker stops when the private directories it opened are unavailable."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from comic_archive.models import ArchiveError
from comic_archive.worker import run_worker


class WorkerQueueCustodyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.session = Path(work.name) / '.comic-session'
        self.queue = self.session / 'worker'
        self.requests = self.queue / 'requests'
        for folder in (self.session, self.queue, self.requests):
            folder.mkdir(mode=0o700)

    def test_an_ordinary_queue_directory_replacement_stops_the_worker(self):
        folders = (self.requests, self.queue) if os.name == 'posix' else (self.requests,)
        for folder in folders:
            with self.subTest(folder=folder):
                replaced = folder.with_name('previous-' + folder.name)

                def disconnect(*_):
                    folder.rename(replaced)
                    folder.mkdir(mode=0o700)

                with patch('comic_archive.worker.controller_ticks', return_value=(('download', disconnect),)), \
                        patch('comic_archive.worker.execute') as execute, patch('builtins.print'), \
                        patch('comic_archive.worker.time.sleep', side_effect=AssertionError('Worker did not stop')):
                    run_worker(self.session, adapter_factory=Mock)
                execute.assert_not_called()
                self.assertEqual(list(folder.iterdir()), [])
                folder.rmdir()
                replaced.rename(folder)

    @unittest.skipUnless(os.name == 'posix', 'POSIX queue permission bits')
    def test_writable_session_is_refused_before_startup(self):
        self.session.chmod(0o770)
        factory = Mock()
        with self.assertRaisesRegex(ArchiveError, 'writable only by this user'):
            run_worker(self.session, adapter_factory=factory)
        factory.assert_not_called()
        self.assertFalse((self.queue / '.runtime').exists())
