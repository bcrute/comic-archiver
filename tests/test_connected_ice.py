"""Connection settings DATA and real private-file/empty CLI lifecycle, not consent."""

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from comic_archive.connected.files import Index, SharedRoot, SourceError
from comic_archive.connected.ice import read_servers, servers


class ConnectedIceTests(unittest.TestCase):
    def test_supported_profile_and_refusals(self):
        turn = dict(
            urls="turns:relay.example:5349?transport=tcp",
            username="public-fixture-user",
            credential="public-fixture-password",
        )
        self.assertEqual(servers([]), [])
        self.assertEqual(
            servers([dict(urls="stun:127.0.0.1:3478"), turn]),
            [dict(urls="stun:127.0.0.1:3478"), turn],
        )
        for bad in [
            None,
            {},
            [dict(urls="stuns:server.example")],
            [dict(urls="stun:server.example?transport=tcp")],
            [dict(urls="stun:server.example"), dict(urls="stun:other.example")],
            [turn, turn],
            [turn | dict(urls="turns:relay.example?transport=udp")],
            [turn | dict(credential="")],
            [turn | dict(username="a\nb")],
            [turn | dict(credential="x" * 513)],
            [turn | dict(urls="turn:[::1]:3478")],
            [turn | dict(urls="turn:relay.example:65536")],
            [turn | dict(urls="turn:user:password@relay.example")],
            [dict(urls="stun:bad..example")],
            [dict(urls=["stun:a.example", "stun:b.example"])],
        ]:
            with self.subTest(value=bad), self.assertRaises(SourceError):
                servers(bad)

    @unittest.skipUnless(os.name == "posix", "Actual POSIX private file required")
    def test_real_file_custody_rejects_public_alias_fifo_and_malformed_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("[]")
            path.chmod(0o600)
            self.assertEqual(read_servers(path), [])
            path.chmod(0o644)
            with self.assertRaises(SourceError):
                read_servers(path)
            path.chmod(0o600)
            alias = Path(folder) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                read_servers(alias)
            alias.unlink()
            os.link(path, alias)
            with self.assertRaises(SourceError):
                read_servers(path)
            alias.unlink()
            fifo = Path(folder) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(SourceError):
                read_servers(fifo)
            for raw in [
                "[]" * 5000,
                '[{"urls":"stun:a.example","urls":"stun:b.example"}]',
                "{}",
                "null",
            ]:
                path.write_text(raw)
                with self.assertRaises(SourceError):
                    read_servers(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(SourceError):
                read_servers(path)

    @unittest.skipUnless(os.name == "posix", "Actual POSIX signal/process required")
    def test_real_empty_host_reads_private_settings_and_stops_normally(self):
        work = Path(tempfile.mkdtemp(prefix="connected-cli-settings-"))
        process = None
        try:
            root_path = work / "selected"
            root_path.mkdir()
            root = SharedRoot(root_path)
            index = Index(work / "index/private.sqlite", root)
            index.close()
            root.close()
            settings = work / "settings.json"
            settings.write_text(
                json.dumps(
                    [
                        dict(
                            urls="turn:127.0.0.1:3478",
                            username="public-fixture-user",
                            credential="public-fixture-password",
                        )
                    ]
                )
            )
            settings.chmod(0o600)
            # No approved pair exists, so there is no outbound peer/file authority.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "comic_archive.connected",
                    "serve",
                    "--state",
                    str(work / "host"),
                    "--origin",
                    "http://127.0.0.1:49151",
                    "--library",
                    str(root_path),
                    str(work / "index/private.sqlite"),
                    "--init",
                    "--ice-config",
                    str(settings),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertTrue(
                select.select([process.stdout], [], [], 10)[0],
                "Native CLI did not report startup",
            )
            ready = json.loads(process.stdout.readline())
            self.assertEqual((ready["status"], ready["pairings"]), ("starting", 0))
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0)
            self.assertNotIn(b"public-fixture-password", output + errors)
            self.assertNotIn(b"public-fixture-user", output + errors)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    # Retain state for the actual live process; no normal-stop claim.
                    raise RuntimeError(
                        "Owned CLI cleanup pending; retain private fixture"
                    ) from None
            if process is None or process.poll() is not None:
                shutil.rmtree(work)
