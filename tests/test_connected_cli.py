"""Actual CLI refusal before native state acquisition; no fabricated consent."""

import os
import json
import selectors
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path

from comic_archive.connected.files import Index, SharedRoot


class ConnectedCliTests(unittest.TestCase):
    def test_default_registration_stays_fixed_and_opt_in_discovers_finished_downloads(self):
        fixture = Path(__file__).parent / "fixtures/local-reading-v1/payload"
        for watching in (False, True):
            with self.subTest(watching=watching), tempfile.TemporaryDirectory() as work:
                base = Path(work)
                library = base / "Library"
                library.mkdir()
                root = SharedRoot(library)
                try:
                    index = Index(base / "private/index.sqlite", root)
                    try:
                        args = [sys.executable, "-m", "comic_archive.connected", "serve",
                                "--state", str(base / "host"), "--origin", "http://127.0.0.1:1",
                                "--library", str(library), str(index.path), "--init"]
                        if watching:
                            args.append("--watch-archiver")
                        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        try:
                            with selectors.DefaultSelector() as ready:
                                ready.register(process.stdout, selectors.EVENT_READ)
                                self.assertTrue(ready.select(timeout=15))
                            line = process.stdout.readline()
                            self.assertTrue(line, "Actual host must emit startup before discovery")
                            status = json.loads(line)
                            self.assertEqual(status["status"], "starting")
                            self.assertEqual(status["watching_archiver"], watching)
                            self.assertEqual(status["pairings"], 0)
                            source = next(p for p in fixture.iterdir() if p.is_dir() and not p.name.startswith("."))
                            shutil.copytree(source, library / source.name)
                            if watching:
                                until = time.monotonic() + 10
                                while not index.snapshot()[1] and time.monotonic() < until:
                                    self.assertIsNone(process.poll())
                                    time.sleep(0.05)
                                self.assertEqual(len(index.snapshot()[1]), 1)
                                self.assertEqual(index.snapshot()[1][0]["series_title"], "Reader fixture")
                            else:
                                time.sleep(0.2)
                                self.assertEqual(index.snapshot()[1], [])
                            process.terminate()
                            output, error = process.communicate(timeout=12)
                            self.assertEqual(process.returncode, 0, error.decode())
                            self.assertEqual(error, b"")
                            self.assertNotIn(str(library).encode(), line + output)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate(timeout=10)
                    finally:
                        index.close()
                finally:
                    root.close()

    def refuses_before_state(self, *, input_pipe):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            shared = root / "selected"
            shared.mkdir()
            library = SharedRoot(shared)
            try:
                index = Index(root / "private/index.sqlite", library)
                index.close()
            finally:
                library.close()
            before = (root / "private/index.sqlite").read_bytes()
            master, slave = os.openpty()
            try:
                args = [
                    sys.executable,
                    "-m",
                    "comic_archive.connected",
                    "serve",
                    "--state",
                    str(root / "host"),
                    "--origin",
                    "http://127.0.0.1:1",
                    "--library",
                    str(shared),
                    str(root / "private/index.sqlite"),
                    "--init",
                    "--interactive",
                ]
                result = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL if input_pipe else slave,
                    stdout=slave if input_pipe else subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"Connected host unavailable", result.stderr)
                self.assertNotIn(str(root).encode(), result.stderr)
                self.assertFalse((root / "host").exists())
                self.assertEqual((root / "private/index.sqlite").read_bytes(), before)
            finally:
                os.close(slave)
                os.close(master)

    def test_real_input_tty_required_before_opening_state(self):
        self.refuses_before_state(input_pipe=True)

    def test_real_output_tty_required_before_opening_state(self):
        self.refuses_before_state(input_pipe=False)
