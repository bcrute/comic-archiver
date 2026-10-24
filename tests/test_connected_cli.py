"""Actual CLI refusal before native state acquisition; no fabricated consent."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from comic_archive.connected.files import Index, SharedRoot


class ConnectedCliTests(unittest.TestCase):
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
                    sys.executable, "-m", "comic_archive.connected", "serve",
                    "--state", str(root / "host"), "--origin", "http://127.0.0.1:1",
                    "--library", str(shared), str(root / "private/index.sqlite"),
                    "--init", "--interactive",
                ]
                result = subprocess.run(
                    args, stdin=subprocess.DEVNULL if input_pipe else slave,
                    stdout=slave if input_pipe else subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=10, check=False,
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
