"""Explicit, source-neutral archive custody; no downloader or remote retrieval."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from comic_archive.connected.__main__ import parser, register
from comic_archive.connected.files import Index, SharedRoot, SourceError, read_source

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.folder = self.base / "library"
        self.folder.mkdir()
        self.root = SharedRoot(self.folder)
        self.addCleanup(self.root.close)
        self.index = Index(self.base / "private/index.sqlite", self.root)
        self.addCleanup(self.index.close)

    def archive(self, name="My issue.cbz", members=None):
        target = self.folder / name
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for key, value in members or [
                ("pages/", b""),
                ("pages/001.png", PNG),
                ("ComicInfo.xml", b"<ComicInfo>https://never-fetch.invalid/</ComicInfo>"),
            ]:
                output.writestr(key, value)
        return target

    def test_exact_original_default_refusal_and_independent_read_only_reopen(self):
        target = self.archive()
        before = target.read_bytes()
        with self.assertRaises(SourceError):
            self.index.register_completed(target.name, "My library")
        record = self.index.register_standalone(target.name, "My library")
        self.assertEqual(record["title"], "My issue")
        self.assertIsNone(record["chapter_order"])
        self.assertEqual(record["sha256"], hashlib.sha256(before).hexdigest())
        revision = self.index.revision
        self.assertEqual(self.index.register_standalone(target.name, "My library"), record)
        self.assertEqual(self.index.revision, revision)
        selected = read_source(self.index)
        self.addCleanup(selected.close)
        reader = selected.open()
        self.addCleanup(reader.root.close)
        self.addCleanup(reader.close)
        self.assertEqual(reader.describe(record["book_id"], record["content_version"]), record)
        for method in (reader.register_completed, reader.register_standalone):
            with self.assertRaises(SourceError) as refused:
                method(target.name, "My library")
            self.assertEqual(refused.exception.code, "auth_required")
        source = reader.open_original(record["book_id"], record["content_version"])
        self.addCleanup(source.close)
        self.assertEqual(source.chunk(0, len(before)), before)
        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn(str(self.folder), json.dumps(self.index.snapshot()))
        self.assertFalse(target.with_suffix(".json").exists())
        # An optional remote composition consumes THESE admitted original bytes
        # through the unchanged JavaScript decoder; no independently rebuilt ZIP.
        if destination := os.environ.get("INK_STANDALONE_ORIGINAL"):
            Path(destination).write_bytes(before)

    def test_malformed_names_metadata_duplicates_and_crc_do_not_change_catalogue(self):
        for members in [
            [("../page.png", PNG)], [("/page.png", PNG)], [("a\\page.png", PNG)],
            [("page.png", PNG), ("PAGE.PNG", PNG)],
            [("x/../page.png", PNG)], [("page.gif", PNG)],
            [("ComicInfo.xml", b"inert but no page")],
            [("page.png", PNG), ("script.js", b"alert(1)")],
            [("page.png", PNG), ("metadata.json", b"x" * 65537)],
            [("page.png", b"")],
        ]:
            self.archive(members=members)
            with self.subTest(names=[name for name, _ in members]), self.assertRaises(SourceError):
                self.index.register_standalone("My issue.cbz", "Series")
            self.assertEqual(self.index.snapshot()[1], [])
        target = self.archive(members=[("page.png", PNG)])
        raw = bytearray(target.read_bytes())
        # Corrupt the central CRC; decompression must check it before committing.
        central = raw.index(b"PK\x01\x02")
        raw[central + 16] ^= 1
        target.write_bytes(raw)
        with self.assertRaises(SourceError):
            self.index.register_standalone(target.name, "Series")
        self.assertEqual(self.index.snapshot()[1], [])

    def test_resource_and_central_bounds_precede_decompression(self):
        target = self.archive()
        raw = bytearray(target.read_bytes())
        tail = raw.rfind(b"PK\x05\x06")
        raw[tail + 8:tail + 12] = b"\xff\xff\xff\xff"
        target.write_bytes(raw)
        with patch("comic_archive.connected.files.zipfile.ZipFile") as opening:
            with self.assertRaises(SourceError):
                self.index.register_standalone(target.name, "Series")
            opening.assert_not_called()
        self.archive()
        with patch("comic_archive.connected.files.EXPANDED_MAX", 1):
            with self.assertRaises(SourceError):
                self.index.register_standalone(target.name, "Series")

    def test_replacement_invalidates_live_pin_and_changes_version_not_identity(self):
        target = self.archive()
        first = self.index.register_standalone(target.name, "Series")
        source = self.index.open_original(first["book_id"], first["content_version"])
        self.addCleanup(source.close)
        replacement = self.archive("replacement.cbz", [("page.png", PNG), ("metadata.json", b"{}")])
        os.replace(replacement, target)
        with self.assertRaises(SourceError):
            source.chunk(0, 1)
        second = self.index.register_standalone(target.name, "Series")
        self.assertEqual(first["book_id"], second["book_id"])
        self.assertNotEqual(first["content_version"], second["content_version"])

    def test_cli_requires_explicit_opt_in_and_does_not_invoke_retrieval(self):
        target = self.archive()
        command = [sys.executable, "-m", "comic_archive.connected", "index", "--root",
                   str(self.folder), "--index", str(self.index.path), "--series", "Series"]
        refused = subprocess.run([*command, target.name], capture_output=True, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, b"")
        output = io.StringIO()
        args = parser().parse_args([*command[3:], "--standalone", target.name])
        with contextlib.redirect_stdout(output):
            register(args)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"share_id", "book_id", "content_version"})
        self.assertNotIn(target.name, output.getvalue())

    def test_genuine_earlier_producer_and_reader_open_both_current_record_types(self):
        fixture = Path(__file__).parent / "fixtures/connected-index-v1/files_4caa2e9.py"
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
                         "1930e938c08e7fc9cd84696f288d806f713d4de17eef5444f154699da717f19c")
        spec = importlib.util.spec_from_file_location("connected_index_4caa2e9", fixture)
        earlier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = earlier
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(earlier)
        target = self.archive("completed.cbz", [("001.png", PNG)])
        import uuid

        target.with_suffix(".json").write_text(json.dumps(dict(
            schema_version=2, format="cbz", content_type="comic", export_id=str(uuid.uuid4()),
            title="Earlier producer", page_count=1,
            content_hashes=[dict(file="001.png", size=len(PNG), sha256=hashlib.sha256(PNG).hexdigest())],
        )))
        old_root = earlier.SharedRoot(self.folder)
        self.addCleanup(old_root.close)
        old = earlier.Index(self.base / "prior/index.sqlite", old_root)
        self.addCleanup(old.close)
        prior = old.register_completed(target.name, "Series")
        current = Index(old.path, self.root)
        self.addCleanup(current.close)
        self.assertEqual(current.describe(prior["book_id"], prior["content_version"]), prior)
        generic = self.archive()
        new = current.register_standalone(generic.name, "Series")
        self.assertEqual(old.describe(new["book_id"], new["content_version"]), new)
        pin = old.open_original(new["book_id"], new["content_version"])
        self.addCleanup(pin.close)
        self.assertEqual(pin.chunk(0, len(generic.read_bytes())), generic.read_bytes())
