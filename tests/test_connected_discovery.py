"""Real public files, producer, private indices and owned threads; no peer grant."""

import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from comic_archive.connected.discovery import CatalogWatcher, CompletedScan
from comic_archive.connected.files import Index, SharedRoot, SourceError, read_source
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter

FIXTURE = Path(__file__).parent / "fixtures/local-reading-v1"


class DiscoveryFixture:
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.library = self.base / "Library"
        shutil.copytree(FIXTURE / "payload", self.library)
        self.root = SharedRoot(self.library)
        self.addCleanup(self.root.close)
        self.index = Index(self.base / "private/index.sqlite", self.root)
        self.addCleanup(self.index.close)
        self.scan = CompletedScan(self.index)
        self.stop = threading.Event()
        manifest = json.loads((FIXTURE / "manifest.json").read_text())
        self.comic = next(item["archive"] for item in manifest["records"] if item["kind"] == "cbz")

    def publish(self):
        series = Series("fixture-future", "Future download", ("Author",))
        episode = Episode("future-1", series.id, "New chapter", 1,
                          (Page(1, "https://synthetic.invalid/page", False),), "reader_confirmed")
        with closing(PublicationStore(self.library)) as publisher:
            folder = publisher.archive(Manifest(series, (episode,)), MemoryAdapter(),
                                       progress=lambda _: None)
        return next(folder.glob("*.cbz"))

    def facts(self):
        return {p.relative_to(self.library).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.library.rglob("*") if p.is_file()}


@unittest.skipUnless(os.name == "posix", "POSIX connected host")
class CompletedScanTests(DiscoveryFixture, unittest.TestCase):
    def test_held_first_producer_discovery_preserves_all_bytes_and_cache(self):
        before = self.facts()
        self.assertEqual(self.scan.run(self.stop), 1)
        snapshot = self.index.snapshot()
        book = snapshot[1][0]
        self.assertEqual(book["series_title"], "Reader fixture")
        self.assertEqual(book["format"], "cbz")
        self.assertEqual(len(snapshot[1]), 1)  # Held EPUB stays local-reader-only.
        self.assertEqual(before, self.facts())
        with patch.object(self.index, "register_completed", wraps=self.index.register_completed) as register:
            self.assertEqual(self.scan.run(self.stop), 0)
            register.assert_not_called()
        self.assertEqual(self.index.snapshot(), snapshot)
        for value in (str(self.library), self.comic, ".archive-state", "source_url"):
            self.assertNotIn(value, json.dumps(snapshot))

    def test_actual_new_publication_waits_for_valid_complete_sidecar(self):
        self.scan.run(self.stop)
        original = self.index.snapshot()[1][0]
        archive = self.publish()
        sidecar = archive.with_suffix(".json")
        good = sidecar.read_bytes()
        sidecar.unlink()
        self.assertEqual(self.scan.run(self.stop), 0)
        self.assertEqual(self.index.snapshot()[1], [original])
        bad = json.loads(good)
        bad["content_hashes"][0]["sha256"] = "f" * 64
        sidecar.write_text(json.dumps(bad))
        self.assertEqual(self.scan.run(self.stop), 0)
        sidecar.write_bytes(good)
        before = self.facts()
        self.assertEqual(self.scan.run(self.stop), 1)
        books = self.index.snapshot()[1]
        self.assertEqual(len(books), 2)
        self.assertIn(original, books)
        new = next(book for book in books if book != original)
        self.assertEqual((new["title"], new["series_title"]), ("New chapter", "Future download"))
        self.assertEqual(new["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(self.facts(), before)

    def test_replacement_and_series_changes_invalidate_cache_preserve_identity(self):
        self.scan.run(self.stop)
        old_revision, (old,) = self.index.snapshot()
        archive = self.library / self.comic
        with zipfile.ZipFile(archive, "a") as comic:
            comic.comment = b"changed original bytes, same completed pages"
        self.assertEqual(self.scan.run(self.stop), 1)
        revision, (new,) = self.index.snapshot()
        self.assertNotEqual(old_revision, revision)
        self.assertEqual(old["book_id"], new["book_id"])
        self.assertNotEqual(old["content_version"], new["content_version"])
        series = archive.parent / "series.json"
        metadata = json.loads(series.read_bytes())
        metadata["title"] = "Updated public series title"
        series.write_text(json.dumps(metadata))
        self.assertEqual(self.scan.run(self.stop), 1)
        final = self.index.snapshot()[1][0]
        self.assertEqual(final["book_id"], new["book_id"])
        self.assertEqual(final["content_version"], new["content_version"])
        self.assertEqual(final["series_title"], metadata["title"])
        archive.unlink()
        self.assertEqual(self.scan.run(self.stop), 0)
        self.assertEqual(self.index.snapshot()[1], [final])
        with self.assertRaises(SourceError):
            self.index.open_original(final["book_id"], final["content_version"])

    def test_hidden_links_pipes_nested_and_unfinished_entries_are_not_adopted(self):
        source = (self.library / self.comic).parent
        shutil.copytree(source, self.library / ".private-copy")
        outside = self.base / "outside"
        shutil.copytree(source, outside)
        (self.library / "linked-series").symlink_to(outside, target_is_directory=True)
        archive = self.library / self.comic
        (source / "linked.cbz").symlink_to(outside / archive.name)
        os.link(outside / archive.name, source / "hardlinked.cbz")
        shutil.copyfile(archive.with_suffix(".json"), source / "hardlinked.json")
        os.mkfifo(source / "pipe.cbz")
        shutil.copyfile(archive, source / "unfinished.cbz.tmp")
        shutil.copyfile(archive, source / "no-sidecar.cbz")
        shutil.copytree(outside, source / "nested")
        self.assertEqual(self.scan.run(self.stop), 1)
        self.assertEqual(len(self.index.snapshot()[1]), 1)
        self.assertEqual(self.index.snapshot()[1][0]["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_enumeration_bound_and_root_replacement_fail_closed(self):
        with patch("comic_archive.connected.discovery.ENTRIES_MAX", 1):
            with self.assertRaises(SourceError) as caught:
                self.scan.run(self.stop)
        self.assertEqual(caught.exception.code, "resource_limit")
        self.library.rename(self.base / "removed")
        self.library.mkdir()
        with self.assertRaises(SourceError) as caught:
            self.scan.run(self.stop)
        self.assertEqual(caught.exception.code, "host_offline")

    def test_bad_deflate_is_a_refused_book_without_ending_discovery(self):
        path = self.library / self.comic
        with zipfile.ZipFile(path) as archive:
            pages = [(item.filename, archive.read(item)) for item in archive.infolist()]
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in pages:
                archive.writestr(name, data)
        raw = bytearray(path.read_bytes())
        name_size, extra_size = struct.unpack_from("<HH", raw, 26)
        raw[30 + name_size + extra_size] = 0x07  # Reserved DEFLATE block type.
        path.write_bytes(raw)
        with self.assertRaises(SourceError) as caught:
            self.index.register_completed(self.comic, "Reader fixture")
        self.assertEqual(caught.exception.code, "integrity_failed")
        self.assertEqual(self.scan.run(self.stop), 0)
        self.assertEqual(self.index.snapshot()[1], [])

    def test_existing_only_index_open_never_initializes_missing_or_empty_files(self):
        missing = self.index.path.parent / "missing.sqlite"
        with self.assertRaises(SourceError):
            Index(missing, self.root, create=False)
        self.assertFalse(missing.exists())
        missing.touch(mode=0o600)
        with self.assertRaises(SourceError):
            Index(missing, self.root, create=False)
        self.assertEqual(missing.read_bytes(), b"")

    def test_existing_actual_read_only_connection_sees_future_catalog_revision(self):
        self.scan.run(self.stop)
        with closing(read_source(self.index)) as selected:
            reader = selected.open()
            try:
                before = reader.snapshot()
                self.publish()
                self.scan.run(self.stop)
                after = reader.snapshot()
                self.assertNotEqual(before[0], after[0])
                self.assertEqual(len(after[1]), 2)
                self.assertIn(before[1][0], after[1])
                with self.assertRaises(SourceError) as caught:
                    reader.register_completed(self.comic, "Cannot grant write access")
                self.assertEqual(caught.exception.code, "auth_required")
            finally:
                reader.close()
                reader.root.close()


@unittest.skipUnless(os.name == "posix", "POSIX connected host")
class CatalogWatcherTests(DiscoveryFixture, unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        until = asyncio.get_running_loop().time() + 8
        while not predicate():
            self.assertLess(asyncio.get_running_loop().time(), until)
            await asyncio.sleep(0.01)

    async def test_actual_live_producer_is_discovered_and_worker_really_joins(self):
        watcher = CatalogWatcher({self.index.share_id: self.index})
        with patch("comic_archive.connected.discovery.POLL_SECONDS", 0.02):
            watcher.start()
            try:
                await self.wait_for(lambda: len(self.index.snapshot()[1]) == 1 or watcher.error)
                self.assertIsNone(watcher.error)
                old = self.index.snapshot()[1][0]
                archive = self.publish()
                await self.wait_for(lambda: len(self.index.snapshot()[1]) == 2 or watcher.error)
                self.assertIsNone(watcher.error)
                self.assertIn(old, self.index.snapshot()[1])
                self.assertTrue(watcher.thread.is_alive())
                self.assertEqual(self.index.snapshot()[1][0]["format"], "cbz")
                self.assertTrue(archive.is_file())
            finally:
                await watcher.close()
        self.assertTrue(watcher.joined)
        self.assertFalse(watcher.thread.is_alive())
        for root, index in watcher.owned:
            self.assertIsNone(root.fd)
            with self.assertRaises(sqlite3.ProgrammingError):
                index.db.execute("SELECT 1")

    async def test_replaced_index_is_not_adopted_or_modified(self):
        watcher = CatalogWatcher({self.index.share_id: self.index})
        other_path = self.index.path.parent / "replacement.sqlite"
        with closing(Index(other_path, self.root)) as replacement:
            self.assertNotEqual(replacement.share_id, self.index.share_id)
        self.index.path.rename(self.index.path.with_suffix(".retained"))
        other_path.rename(self.index.path)
        before = self.index.path.read_bytes()
        watcher.start()
        with self.assertRaises(SourceError):
            await watcher.monitor()
        await watcher.close()
        self.assertEqual(self.index.path.read_bytes(), before)
        self.assertTrue(watcher.joined)

    async def test_missing_index_is_not_recreated_by_worker(self):
        watcher = CatalogWatcher({self.index.share_id: self.index})
        self.index.path.unlink()
        watcher.start()
        with self.assertRaises(SourceError):
            await watcher.monitor()
        await watcher.close()
        self.assertFalse(self.index.path.exists())

    async def test_unknown_join_retains_real_io_owner_until_sqlite_lock_releases(self):
        watcher = CatalogWatcher({self.index.share_id: self.index})
        entered = threading.Event()
        register = Index.register_completed

        def observed(index, *args):
            entered.set()
            return register(index, *args)

        self.index.db.execute("BEGIN IMMEDIATE")  # Real kernel/SQLite contention.
        with patch.object(Index, "register_completed", observed):
            watcher.start()
            try:
                await self.wait_for(entered.is_set)
                with patch("comic_archive.connected.discovery.JOIN_SECONDS", 0.001):
                    with self.assertRaises(SourceError):
                        await watcher.close()
                self.assertFalse(watcher.joined)
                self.assertTrue(watcher.thread.is_alive())
                self.assertIsNotNone(watcher.owned[0][0].fd)
            finally:
                self.index.db.execute("ROLLBACK")
                await watcher.close()
        self.assertTrue(watcher.joined)
        self.assertFalse(watcher.thread.is_alive())
