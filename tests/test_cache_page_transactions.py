from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.archive import Store
from comic_archive.models import Episode, Page
from test_archive import png


class PageTransactionTests(unittest.TestCase):
    def test_completed_group_commits_once_and_survives_a_later_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'
            pages = tuple(Page(i, f'https://cdn.tapas.io/pc/{i}', False) for i in range(1, 4))
            episode = Episode('81', '42', 'Episode', 1, pages, 'tapas_free')
            store = Store(root, min_free_bytes=0)
            statements = []
            store.db.set_trace_callback(statements.append)
            try:
                with self.assertRaisesRegex(ValueError, 'Later operation failed'):
                    with store.page_transaction():
                        for page in pages:
                            store.remember_page('tapas', episode, page, png(page.order))
                        raise ValueError('Later operation failed')
                self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
                self.assertFalse(store.db.in_transaction)
            finally:
                store.close()
            store = Store(root, min_free_bytes=0)
            try:
                self.assertEqual([store.cached_page('tapas', episode, p) for p in pages],
                                 [png(i) for i in range(1, 4)])
                self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                store.close()

    def test_commit_failure_restores_the_normal_page_commit_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'Library', min_free_bytes=0)
            page = Page(1, 'https://cdn.tapas.io/pc/1', False)
            episode = Episode('81', '42', 'Episode', 1, (page,), 'tapas_free')
            connection = store.db
            class FailingCommit:
                def commit(self):
                    raise sqlite3.OperationalError('Storage failed')
                def __getattr__(self, name):
                    return getattr(connection, name)
            store.db = FailingCommit()
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    with store.page_transaction():
                        store.remember_page('tapas', episode, page, png(1))
                self.assertFalse(store._batching_pages)
                store.db = connection
                connection.rollback()
                store.remember_page('tapas', episode, page, png(1))
                self.assertFalse(connection.in_transaction)
            finally:
                store.db = connection
                store.close()

    def test_archived_pages_upgrade_is_one_transaction_and_an_older_partial_one_is_finished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'
            Store(root, min_free_bytes=0).close()
            path, record = root / '.archive-cache/index.sqlite3', ('h', 'Series/Episode.cbz', '0001.png', 1)
            def tables():
                with closing(sqlite3.connect(path)) as db:
                    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, db.execute('SELECT * FROM archived_pages').fetchall()
            with closing(sqlite3.connect(path)) as db:
                db.execute('DROP TABLE archived_pages')
                db.execute('CREATE TABLE archived_pages (hash TEXT PRIMARY KEY,path TEXT,member TEXT,size INTEGER)')
                db.execute('INSERT INTO archived_pages VALUES (?,?,?,?)', record); db.commit()
            connect = sqlite3.connect
            class Stopping:
                """Stops the upgrade after its rename, as a crash would."""
                def __init__(self, db): self.db = db
                def __getattr__(self, name): return getattr(self.db, name)
                def __enter__(self): return self.db.__enter__()
                def __exit__(self, *error): return self.db.__exit__(*error)
                def execute(self, sql, *args):
                    if sql.startswith('CREATE TABLE archived_pages ('): raise sqlite3.OperationalError('simulated crash')
                    return self.db.execute(sql, *args)
            with patch('comic_archive.archive.sqlite3.connect', lambda p: Stopping(connect(p))), self.assertRaises(sqlite3.OperationalError):
                Store(root, min_free_bytes=0)
            self.assertNotIn('archived_pages_single', tables()[0]); self.assertEqual(tables()[1], [record])
            with closing(sqlite3.connect(path)) as db:
                db.execute('ALTER TABLE archived_pages RENAME TO archived_pages_single'); db.commit()  # An older version's stop.
            Store(root, min_free_bytes=0).close()
            self.assertNotIn('archived_pages_single', tables()[0]); self.assertEqual(tables()[1], [record])
