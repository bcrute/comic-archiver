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
        os.write(master, b"status\nstatus\nstatus\nstatus\napprove synthetic\n")
        for _ in range(100):
            if terminal.fd is None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNone(terminal.fd)
        with self.assertRaises(SourceError):
            await asyncio.wait_for(terminal.readline(), 1)
        self.assertIsNone(await terminal.readline())
