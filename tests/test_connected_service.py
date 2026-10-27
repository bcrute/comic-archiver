"""Fleet-only negative HTTP boundary controls with actual TCP/private host state.

The deliberate refusing HTTP responders grant NO Account/Socket/Lease/RTC/file
success. Positive pairing needs the actual web reader SQL/account/service helper.
"""

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

AVAILABLE = os.name == "posix" and importlib.util.find_spec("jwcrypto") is not None
if AVAILABLE:
    from comic_archive.connected.files import Index, SharedRoot, SourceError
    from comic_archive.connected.pairing import PairingOwner
    from comic_archive.connected.service import HostService
    from comic_archive.connected.state import HostState


@unittest.skipUnless(
    AVAILABLE, "POSIX/connected extra required; skip is no qualification"
)
class ConnectedServiceRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = tempfile.TemporaryDirectory()
        base = Path(self.work.name)
        shared = base / "shared"
        shared.mkdir()
        self.root = SharedRoot(shared)
        self.index = Index(base / "index/private.sqlite", self.root)
        state = HostState.open(base / "host", roots=[self.root], create=True)
        self.owner = PairingOwner(state, {self.index.share_id: self.index})
        self.draft = self.owner.start()
        self.services, self.servers, self.handlers = [], [], []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.handlers:
            done, pending = await asyncio.wait(self.handlers, timeout=3)
            self.assertFalse(pending)
            for task in done:
                task.result()
        self.owner.close()
        self.index.close()
        self.root.close()
        self.work.cleanup()

    async def refusing_origin(self, response):
        async def handle(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                writer.write(response(raw))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        def accept(reader, writer):
            self.handlers.append(asyncio.create_task(handle(reader, writer)))

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.servers.append(server)
        return "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])

    async def test_real_http_redirect_is_not_followed_and_server_cookies_never_persist(
        self,
    ):
        destination = []

        def foreign(raw):
            destination.append(raw)
            return b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

        target = await self.refusing_origin(foreign)
        requests = []

        def refusal(raw):
            requests.append(raw)
            if len(requests) == 1:
                return b"HTTP/1.1 503 Unavailable\r\nSet-Cookie: ink_session=untrusted-test-data; Path=/\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            return (
                "HTTP/1.1 307 Temporary Redirect\r\nLocation: "
                + target
                + "/do-not-follow\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()

        service = HostService(self.owner, await self.refusing_origin(refusal))
        self.services.append(service)
        for _ in range(2):
            with self.assertRaises(SourceError):
                await service.register(self.draft)  # Same actual native draft.
        self.assertEqual(destination, [])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(b"cookie:" not in raw.lower() for raw in requests))
        self.assertTrue(self.owner._live(self.draft))
        self.assertEqual(self.owner._record(self.draft.pairing_id)["status"], "pending")

    async def test_actual_oversized_response_refuses_without_approving_or_revoking_local_state(
        self,
    ):
        body = b"x" * 9000

        def oversized(_raw):
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 9000\r\nConnection: close\r\n\r\n"
                + body
            )

        service = HostService(self.owner, await self.refusing_origin(oversized))
        self.services.append(service)
        with self.assertRaises(SourceError) as caught:
            await service.register(self.draft)
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertTrue(self.owner._live(self.draft))
        self.assertEqual(self.owner._record(self.draft.pairing_id)["status"], "pending")
