"""Actual PTY/input custody only; no peer/account/consent is reconstructed."""

import asyncio
import os
import unittest

from comic_archive.connected.files import SourceError
from comic_archive.connected.terminal import TerminalInput


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "openpty"), "Actual POSIX terminal required"
)
class ConnectedTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_terminal_input_and_descriptor_retirement_preserve_original_mode(
        self,
    ):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = os.get_blocking(slave)
        terminal = TerminalInput.open(slave)
        self.addCleanup(terminal.close)
        owned = terminal.fd
        # Its own opening of the terminal: the borrowed one, and whatever shares it, still waits as before.
        self.assertEqual(os.get_blocking(slave), before)
        self.assertFalse(os.get_blocking(owned))
        self.assertEqual(os.fstat(owned).st_rdev, os.fstat(slave).st_rdev)
        os.write(master, b"status\n")
        self.assertEqual(await asyncio.wait_for(terminal.readline(), 1), "status")
        terminal.close()
        self.assertIsNone(terminal.fd)
        self.assertEqual(os.get_blocking(slave), before)
        with self.assertRaises(OSError):
            os.fstat(owned)
        self.assertTrue(os.isatty(slave))  # Borrowed original remains open.

    async def test_nonterminal_refusal_and_actual_input_queue_overflow_do_not_execute_queued_commands(
        self,
    ):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        with self.assertRaises(SourceError):
            TerminalInput.open(read)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        terminal = TerminalInput.open(slave)
        self.addCleanup(terminal.close)
        # The real terminal/loop, not a manufactured ready flag or input object.
        os.write(master, b"status\nstatus\nstatus\nstatus\n")
        for _ in range(100):
            if terminal._lines.full():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(terminal._lines.full())
        os.write(master, b"approve synthetic\n")
        for _ in range(100):
            if terminal._lines.qsize() == 1:
                break
            await asyncio.sleep(0.01)
        # Typed-ahead lines are all dropped, and said so, but input carries on.
        with self.assertRaises(SourceError) as caught:
            await asyncio.wait_for(terminal.readline(), 1)
        self.assertEqual(caught.exception.code, "busy")
        self.assertIsNotNone(terminal.fd)
        os.write(master, b"status\n")
        self.assertEqual(await asyncio.wait_for(terminal.readline(), 1), "status")

    async def test_a_line_that_is_not_text_or_is_too_long_is_refused_alone_and_input_goes_on(self):
        import tty
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tty.setraw(slave)  # A terminal that hands lines over uncut and unedited.
        terminal = TerminalInput.open(slave)
        self.addCleanup(terminal.close)
        os.write(master, b"\xff\xfe\n")
        with self.assertRaises(SourceError) as caught:
            await asyncio.wait_for(terminal.readline(), 1)
        self.assertEqual(caught.exception.code, "invalid_command")
        for _ in range(5):
            os.write(master, b"a" * 1000)
            await asyncio.sleep(0.05)
        os.write(master, b"\n")
        with self.assertRaises(SourceError) as caught:
            await asyncio.wait_for(terminal.readline(), 1)
        self.assertEqual(caught.exception.code, "invalid_command")
        self.assertIsNotNone(terminal.fd)
        # In pieces smaller than a terminal's own queue: a Mac's holds 1024 bytes, and a write that does not
        # fit waits for a reader, which is this same loop.
        for _ in range(8):
            os.write(master, b"a" * 512)
            await asyncio.sleep(0.03)
        os.write(master, b"\nstatus\n")
        self.assertEqual(await asyncio.wait_for(terminal.readline(), 1), "a" * 4096)
        self.assertEqual(await asyncio.wait_for(terminal.readline(), 1), "status")
