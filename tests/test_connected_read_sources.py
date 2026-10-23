"""Actual native readonly SQLite/thread controls, not peer/account authority."""
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from comic_archive.connected.files import Index, SharedRoot, SourceError, read_source


@unittest.skipUnless(os.name == 'posix', 'Initial actual host boundary is POSIX')
class ConnectedReadSourceTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        base = Path(work.name)
        shared = base / 'selected'
        shared.mkdir()
        self.root = SharedRoot(shared)
        self.addCleanup(self.root.close)
        self.index = Index(base / 'private/index.sqlite', self.root)
        self.addCleanup(self.index.close)

    def test_actual_worker_owns_a_distinct_readonly_connection_and_original_remains_usable(self):
        source = read_source(self.index)
        self.addCleanup(source.close)
        observed, failures = [], []
        def run():
            copy = None
            try:
                copy = source.open()
                observed.append(copy.share_id)
                with self.assertRaises(sqlite3.OperationalError):
                    copy.db.execute('DELETE FROM books')
            except BaseException as error:
                failures.append(error)
            finally:
                if copy is not None:
                    copy.close()
                    copy.root.close()
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]
        self.assertEqual(observed, [self.index.share_id])
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM books').fetchone()[0], 0)

    def test_replaced_private_db_cannot_be_adopted_by_an_existing_native_read_source(self):
        source = read_source(self.index)
        self.addCleanup(source.close)
        foreign = Index(self.index.path.with_name('other.sqlite'), self.root)
        different = foreign.share_id
        foreign.close()
        os.replace(self.index.path.with_name('other.sqlite'), self.index.path)
        with self.assertRaises(SourceError):
            source.open()
        self.assertNotEqual(different, self.index.share_id)
