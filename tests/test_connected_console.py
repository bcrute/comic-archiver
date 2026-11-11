"""The host's console and its connection boundary under network failures; no real service or peer."""

import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comic_archive.connected.files import SourceError

READY = all(importlib.util.find_spec(name) is not None for name in ("websockets", "jwcrypto"))


class Lines:
    """Console input: these lines, then the end of input."""

    def __init__(self, *lines):
        self.lines = list(lines)

    async def readline(self):
        if not self.lines:
            return None
        line = self.lines.pop(0)
        if isinstance(line, Exception):
            raise line
        return line


@unittest.skipUnless(READY, "Opt-in connected extra required")
class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def said(self, driver, *lines):
        from comic_archive.connected import __main__ as host

        output, shutdown = [], asyncio.Event()
        with patch.object(host, "emit", output.append):
            await asyncio.wait_for(host.console(driver, Lines(*lines), shutdown), 5)
        self.assertTrue(shutdown.is_set())
        return output[1:]  # After the list of commands.

    async def test_a_network_failure_under_one_command_is_reported_and_the_console_goes_on(self):
        from websockets.exceptions import InvalidHandshake

        for failure in (OSError("synthetic"), asyncio.TimeoutError(), InvalidHandshake("synthetic")):
            driver = Mock()
            driver.controller.pending.return_value = []
            driver.controller.begin = AsyncMock(side_effect=failure)
            driver.status.return_value = []
            driver.controller.owner._indices = {}
            said = await self.said(driver, "pair", "status")
            self.assertEqual(said[0], dict(error="connection_unavailable"), failure)
            self.assertEqual(said[1], dict(connections=[], pending=[], shares=[]))

    async def test_a_refused_line_is_reported_and_the_console_goes_on(self):
        driver = Mock()
        driver.controller.pending.return_value = []
        driver.status.return_value = []
        driver.controller.owner._indices = {}
        said = await self.said(driver, SourceError("invalid_command"), SourceError("busy"), "status")
        self.assertEqual(said[:2], [dict(error="invalid_command"), dict(error="busy")])
        self.assertEqual(len(said), 3)

    async def test_input_that_has_ended_in_an_error_still_ends_the_console(self):
        from comic_archive.connected import __main__ as host

        with patch.object(host, "emit", Mock()), self.assertRaises(SourceError):
            await host.console(Mock(), Lines(SourceError("resource_limit")), asyncio.Event())


@unittest.skipUnless(READY, "Opt-in connected extra required")
class RendezvousBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_connection_that_cannot_be_made_is_the_connection_being_unavailable(self):
        from websockets.exceptions import InvalidHandshake
        from comic_archive.connected import rendezvous

        for failure in (OSError("synthetic"), asyncio.TimeoutError(), InvalidHandshake("synthetic")):
            with patch.object(rendezvous, "PinnedConnect", AsyncMock(side_effect=failure)):
                with self.assertRaises(SourceError) as caught:
                    await rendezvous.HostRendezvous._connect("wss://synthetic.invalid")
            self.assertEqual(caught.exception.code, "connection_unavailable", failure)
        with patch.object(rendezvous, "PinnedConnect", AsyncMock(side_effect=KeyError("synthetic"))):
            with self.assertRaises(KeyError):  # Anything else is not a network failure and is not hidden.
                await rendezvous.HostRendezvous._connect("wss://synthetic.invalid")

    async def test_a_send_the_network_fails_closes_the_connection_and_says_unavailable(self):
        from websockets.exceptions import ConnectionClosedError
        from comic_archive.connected import rendezvous

        for failure, expected in ((ConnectionClosedError(None, None), SourceError), (OSError("synthetic"), SourceError),
                                  (KeyError("synthetic"), KeyError)):
            transport = object.__new__(rendezvous.HostRendezvous)
            transport._send_lock, transport._service_deadline = asyncio.Lock(), 5.0
            transport._current = Mock()
            transport._socket = Mock(send=AsyncMock(side_effect=failure))
            transport.close = AsyncMock()
            with self.assertRaises(expected) as caught:
                await transport._send({"v": 1})
            if expected is SourceError:
                self.assertEqual(caught.exception.code, "connection_unavailable")
            transport.close.assert_awaited_once()
            self.assertEqual(transport._service_deadline, 0)


class CloseBudgetTests(unittest.TestCase):
    def test_every_wait_for_a_close_covers_all_that_it_waits_on(self):
        from comic_archive.connected import limits

        self.assertGreater(limits.PEER_CLEANUP, limits.WORKER_CLOSE + limits.NATIVE_CLOSE)
        self.assertGreaterEqual(limits.PEER_CLOSE, limits.PEER_TASK + limits.PEER_CLEANUP)
        self.assertGreater(limits.RUNNER_CLOSE, limits.PEER_CLOSE + limits.RECEIVE_CLOSE)
        self.assertGreaterEqual(limits.PAIRING_STOP, limits.OPENING + limits.RUNNER_CLOSE)
        self.assertGreater(limits.SLOT_STOP, limits.PAIRING_STOP)
