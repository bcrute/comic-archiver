"""Framing/real-file controls; authorization callbacks are explicit test doubles."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from comic_archive.connected.files import SharedRoot, SourceError
from comic_archive.connected.wire import (
    PAYLOAD_MAX,
    WINDOW_MAX,
    ReadRange,
    decode_frame,
    encode_frame,
)

VECTOR = Path(__file__).parent / "fixtures/connected-library/frame-v1.json"


class ConnectedFrameTests(unittest.TestCase):
    def test_hand_authored_cross_language_data_vector_and_malformed_headers(self):
        value = json.loads(VECTOR.read_text())
        expected = bytes.fromhex(value["frame_hex"])
        self.assertEqual(
            encode_frame(
                value["read_id"], 0, 0, bytes.fromhex(value["payload_hex"]), True
            ),
            expected,
        )
        self.assertEqual(decode_frame(expected), (value["read_id"], 0, 0, b"abc", True))
        for offset in [0, 4, 5, 14, 24, 39]:
            changed = bytearray(expected)
            changed[offset] ^= 255
            with self.subTest(offset=offset), self.assertRaises(SourceError):
                decode_frame(bytes(changed))
        for changed in [expected[:39], expected[:-1], expected + b"extra"]:
            with self.assertRaises(SourceError):
                decode_frame(changed)


@unittest.skipUnless(os.name == "posix", "Initial host file boundary is POSIX only")
class ConnectedRangeTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.raw = b"synthetic unchanged source" * 1800
        (self.base / "original.cbz").write_bytes(self.raw)
        self.root = SharedRoot(self.base)
        self.addCleanup(self.root.close)

    def range(self, offset=0, length=None, authorize=lambda: True, clock=lambda: 0):
        source = self.root.open("original.cbz")
        result = ReadRange(source, offset, length or len(self.raw), authorize, clock)
        self.addCleanup(result.close)
        return result

    def test_actual_pinned_range_respects_credit_size_offsets_and_terminal_custody(
        self,
    ):
        calls = []
        stream = self.range(
            offset=3,
            length=len(self.raw) - 9,
            authorize=lambda: not calls.append("checked"),
        )
        self.assertIsNone(stream.next_frame())
        stream.add_credit(len(self.raw))
        received = bytearray()
        while not stream.closed:
            frame = stream.next_frame()
            self.assertLessEqual(len(frame), 16384)
            read_id, offset, sequence, payload, final = decode_frame(frame)
            self.assertEqual(read_id, stream.read_id)
            self.assertEqual(offset, 3 + len(received))
            self.assertEqual(sequence, len(received) // PAYLOAD_MAX)
            received.extend(payload)
            self.assertEqual(final, len(received) == len(self.raw) - 9)
        self.assertEqual(received, self.raw[3:-6])
        self.assertGreaterEqual(len(calls), 2 + 2 * stream.sequence)
        self.assertIsNone(stream.source.fd)
        with self.assertRaises(SourceError):
            stream.next_frame()
        self.assertEqual((self.base / "original.cbz").read_bytes(), self.raw)

    def test_revoke_and_monotonic_idle_end_close_the_actual_file(self):
        allowed = [True]
        stream = self.range(authorize=lambda: allowed[0])
        stream.add_credit(2)
        allowed[0] = False
        with self.assertRaises(SourceError) as error:
            stream.next_frame()
        self.assertEqual(error.exception.code, "revoked")
        self.assertIsNone(stream.source.fd)
        now = [0]
        idle = self.range(clock=lambda: now[0])
        now[0] = 30
        with self.assertRaises(SourceError):
            idle.add_credit(1)
        self.assertIsNone(idle.source.fd)
        now[0] = 0
        fixed_end = self.range(clock=lambda: now[0])
        for value in [20, 40]:
            now[0] = value
            fixed_end.add_credit(1)
        now[0] = 60
        with self.assertRaises(SourceError):
            fixed_end.next_frame()
        self.assertIsNone(fixed_end.source.fd)
        # Callback is a fake policy solely to test rechecking; it is no actual
        # peer/share/account consent, lease, transport or successful reader proof.

    def test_overcredit_invalid_range_and_non_boolean_policy_do_not_keep_handles(self):
        stream = self.range()
        stream.add_credit(WINDOW_MAX)
        with self.assertRaises(SourceError):
            stream.add_credit(1)
        self.assertIsNone(stream.source.fd)
        for offset, length in [
            (-1, 1),
            (True, 1),
            (0, 4 * 1024**2 + 1),
            (len(self.raw), 1),
        ]:
            source = self.root.open("original.cbz")
            with self.subTest(offset=offset), self.assertRaises(SourceError):
                ReadRange(source, offset, length, lambda: True)
            self.assertIsNone(source.fd)
        source = self.root.open("original.cbz")
        with self.assertRaises(SourceError):
            ReadRange(source, 0, 1, lambda: {"approved": False})
        self.assertIsNone(source.fd)
