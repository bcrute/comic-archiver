"""Real synthetic files/index only. No paired peer/account/runtime is fabricated."""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from comic_archive.connected.files import Index, SharedRoot, SourceError


@unittest.skipUnless(os.name == "posix", "Initial host file boundary is POSIX only")
class ConnectedLibraryTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.library = self.base / "shared"
        (self.library / "series").mkdir(parents=True)
        self.root = SharedRoot(self.library)
        self.addCleanup(self.root.close)
        self.index = Index(self.base / "private" / "index.sqlite", self.root)
        self.addCleanup(self.index.close)

    def book(self, name="series/issue.cbz", data=b"complete synthetic page bytes"):
        target = self.library / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("0001.png", data)
        meta = dict(
            schema_version=2,
            export_id=str(uuid.uuid4()),
            title="Safe <script> title",
            format="cbz",
            content_type="comic",
            page_count=1,
            episode_number=4,
            content_hashes=[
                dict(
                    file="0001.png",
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            ],
            source_url="https://private.invalid/do-not-fetch",
            private_canary="NEVER_PROJECT_THIS",
        )
        target.with_suffix(".json").write_text(json.dumps(meta))
        return target

    def test_completed_bytes_and_wire_projection_are_exact_and_source_neutral(self):
        target = self.book()
        before = target.read_bytes()
        record = self.index.register_completed("series/issue.cbz", "Series")
        self.assertEqual(record["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(record["byte_length"], len(before))
        self.assertEqual(record["title"], "Safe <script> title")
        self.assertEqual(
            set(record),
            {
                "book_id",
                "content_version",
                "series_id",
                "title",
                "series_title",
                "chapter_order",
                "byte_length",
                "format",
                "sha256",
            },
        )
        projected = json.dumps(self.index.snapshot())
        self.assertNotIn(str(self.library), projected)
        self.assertNotIn("issue.cbz", projected)
        self.assertNotIn("NEVER_PROJECT_THIS", projected)
        self.assertNotIn("private.invalid", projected)
        original = self.index.open_original(
            record["book_id"], record["content_version"]
        )
        self.addCleanup(original.close)
        self.assertEqual(original.chunk(0, len(before)), before)
        self.assertEqual(target.read_bytes(), before)
        revision = self.index.snapshot()[0]
        self.assertEqual(
            self.index.register_completed("series/issue.cbz", "Series"), record
        )
        self.assertEqual(self.index.snapshot()[0], revision)

    def test_reopen_preserves_random_ids_without_accepting_a_different_root(self):
        self.book()
        record = self.index.register_completed("series/issue.cbz", "Series")
        other = Index(self.index.path, self.root)
        self.addCleanup(other.close)
        self.assertEqual(
            other.describe(record["book_id"], record["content_version"]), record
        )
        folder = self.base / "different"
        folder.mkdir()
        root = SharedRoot(folder)
        self.addCleanup(root.close)
        with self.assertRaises(SourceError):
            Index(self.index.path, root)

    def test_replacement_invalidates_old_pin_and_reindex_changes_only_version(self):
        target = self.book()
        record = self.index.register_completed("series/issue.cbz", "Series")
        source = self.index.open_original(record["book_id"], record["content_version"])
        self.addCleanup(source.close)
        replacement = self.book("series/replacement.cbz", b"new content")
        os.replace(replacement, target)
        os.replace(replacement.with_suffix(".json"), target.with_suffix(".json"))
        with self.assertRaises(SourceError) as caught:
            source.chunk(0, 1)
        self.assertEqual(caught.exception.code, "version_changed")
        revised = self.index.register_completed("series/issue.cbz", "Series")
        self.assertEqual(record["book_id"], revised["book_id"])
        self.assertNotEqual(record["content_version"], revised["content_version"])
        with self.assertRaises(SourceError):
            self.index.open_original(record["book_id"], record["content_version"])

    def test_proven_rename_keeps_identity_but_equal_independent_copy_does_not_merge(
        self,
    ):
        target = self.book()
        record = self.index.register_completed("series/issue.cbz", "Series")
        renamed = target.with_name("renamed.cbz")
        target.rename(renamed)
        target.with_suffix(".json").rename(renamed.with_suffix(".json"))
        moved = self.index.register_completed("series/renamed.cbz", "Series")
        self.assertEqual(record["book_id"], moved["book_id"])
        self.assertEqual(record["content_version"], moved["content_version"])
        copy = renamed.with_name("copy.cbz")
        copy.write_bytes(renamed.read_bytes())
        copy.with_suffix(".json").write_bytes(renamed.with_suffix(".json").read_bytes())
        independent = self.index.register_completed("series/copy.cbz", "Series")
        self.assertNotEqual(moved["book_id"], independent["book_id"])

    def test_traversal_symlink_private_state_and_hardlink_are_refused(self):
        target = self.book()
        for value in [
            "../outside.cbz",
            "/absolute.cbz",
            "series/../issue.cbz",
            "series\\issue.cbz",
            ".archive-state/issue.cbz",
        ]:
            with self.subTest(value=value), self.assertRaises(SourceError):
                self.root.open(value)
        (self.library / "series" / "link.cbz").symlink_to(target)
        with self.assertRaises(SourceError):
            self.root.open("series/link.cbz")
        os.link(target, self.library / "series" / "hard.cbz")
        with self.assertRaises(SourceError):
            self.root.open("series/hard.cbz")
        with self.assertRaises(SourceError):
            Index(self.library / "private" / "index.sqlite", self.root)

    def test_half_published_bad_hash_and_unsupported_format_never_register(self):
        target = self.book()
        metadata = target.with_suffix(".json")
        good = metadata.read_text()
        metadata.unlink()
        with self.assertRaises(SourceError):
            self.index.register_completed("series/issue.cbz", "Series")
        value = json.loads(good)
        value["content_hashes"][0]["sha256"] = "f" * 64
        metadata.write_text(json.dumps(value))
        with self.assertRaises(SourceError):
            self.index.register_completed("series/issue.cbz", "Series")
        value = json.loads(good)
        value["title"] = "\ud800"
        metadata.write_text(json.dumps(value))
        with self.assertRaises(SourceError) as caught:
            self.index.register_completed("series/issue.cbz", "Series")
        self.assertEqual(str(caught.exception), "integrity_failed")
        value["format"] = "epub"
        metadata.write_text(json.dumps(value))
        with self.assertRaises(SourceError):
            self.index.register_completed("series/issue.cbz", "Series")
        self.assertEqual(self.index.snapshot()[1], [])

    def test_central_directory_bomb_is_refused_before_zipfile_allocation(self):
        target = self.book()
        data = bytearray(target.read_bytes())
        tail = data.rfind(b"PK\x05\x06")
        data[tail + 8 : tail + 12] = b"\xff\xff\xff\xff"
        target.write_bytes(data)
        from unittest.mock import patch

        with patch("comic_archive.connected.files.zipfile.ZipFile") as parser:
            with self.assertRaises(SourceError):
                self.index.register_completed("series/issue.cbz", "Series")
            parser.assert_not_called()

    def test_foreign_or_incomplete_index_is_not_adopted(self):
        path = self.index.path.parent / "foreign.sqlite"
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE unrelated(value TEXT)")
            database.execute("INSERT INTO unrelated VALUES('retain')")
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(SourceError):
            Index(path, self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_root_disappearance_is_unavailable_not_a_catalogue_tombstone(self):
        self.book()
        self.index.register_completed("series/issue.cbz", "Series")
        self.library.rename(self.base / "moved")
        with self.assertRaises(SourceError) as caught:
            self.index.snapshot()
        self.assertEqual(caught.exception.code, "host_offline")
        self.assertEqual(
            self.index.db.execute("SELECT count(*) FROM books").fetchone()[0], 1
        )
