"""Actual UDP/ICE retirement, not an admitted application peer or file grant."""

import asyncio
import unittest

from aiortc import RTCIceCandidate, RTCIceGatherer, RTCIceTransport

from comic_archive.connected.rtc import _IceRetirement


class SilentSocket(asyncio.DatagramProtocol):
    def __init__(self):
        self.closed = asyncio.get_running_loop().create_future()
        self.received = 0

    def datagram_received(self, data, addr):
        self.received += 1  # Genuine STUN bytes arrive; no fake successful reply.

    def connection_lost(self, error):
        self.closed.set_result(error)


class ConnectedIceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transports, self.errors = [], []
        self.loop = asyncio.get_running_loop()
        self.previous = self.loop.get_exception_handler()

        def failed(loop, context):
            self.errors.append(context)
            if self.previous is None:
                loop.default_exception_handler(context)
            else:
                self.previous(loop, context)

        self.loop.set_exception_handler(failed)
        self.socket, self.sink = await self.loop.create_datagram_endpoint(
            SilentSocket, local_addr=("127.0.0.1", 0)
        )
        self.address = self.socket.get_extra_info("sockname")

    async def asyncTearDown(self):
        try:
            for transport in self.transports:
                await asyncio.wait_for(transport.stop(), 5)
            self.socket.close()
            self.assertIsNone(await asyncio.wait_for(self.sink.closed, 2))
            self.assertEqual(self.errors, [])
        finally:
            self.loop.set_exception_handler(self.previous)

    async def native(self):
        gatherer = RTCIceGatherer(iceServers=[])
        transport = RTCIceTransport(gatherer)
        self.transports.append(transport)
        owner = _IceRetirement.own(transport)
        await asyncio.wait_for(gatherer.gather(), 5)
        return gatherer, transport, owner

    async def pending(self, owner):
        end = self.loop.time() + 2
        while not (self.sink.received > 0 and
                   any(protocol.transactions for protocol in owner.connection._protocols)):
            self.assertLess(self.loop.time(), end)
            await asyncio.sleep(0.01)
        self.assertTrue(any(pair.task and not pair.task.done()
                            for pair in owner.connection._check_list))

    def silent_candidate(self):
        return RTCIceCandidate(component=1, foundation="owned-silent", priority=1,
            ip=self.address[0], port=self.address[1], protocol="udp", type="host")

    async def assert_retired(self, transport, owner):
        protocols = tuple(owner.connection._protocols)
        tasks = {pair.task for pair in owner.connection._check_list if pair.task is not None}
        await asyncio.wait_for(transport.stop(), 5)
        self.assertTrue(owner.joined)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertTrue(all(not protocol.transactions and protocol.transport.is_closing()
                            for protocol in protocols))
        received = self.sink.received
        await asyncio.sleep(0.6)  # Cross the real initial 0.5s retry timer.
        self.assertEqual(self.sink.received, received)
        self.assertEqual(self.errors, [])

    async def test_close_joins_checks_during_native_negotiation(self):
        gatherer, transport, owner = await self.native()
        await transport.addRemoteCandidate(self.silent_candidate())
        # Parameters are negotiation DATA, never an accepted peer or read grant.
        starting = asyncio.create_task(transport.start(gatherer.getLocalParameters()))
        try:
            await self.pending(owner)
            await self.assert_retired(transport, owner)
            await asyncio.wait_for(starting, 2)
            self.assertTrue(starting.done())
        finally:
            if not starting.done():
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)

    async def test_close_joins_late_check_after_real_ice_completed(self):
        first, left, owner = await self.native()
        second, right, _ = await self.native()
        owner.connection.ice_controlling = True
        for candidate in first.getLocalCandidates():
            await right.addRemoteCandidate(candidate)
        for candidate in second.getLocalCandidates():
            await left.addRemoteCandidate(candidate)
        await asyncio.wait_for(asyncio.gather(left.start(second.getLocalParameters()),
            right.start(first.getLocalParameters())), 8)
        self.assertEqual((left.state, right.state), ("completed", "completed"))
        await left.addRemoteCandidate(self.silent_candidate())
        pairs = [pair for pair in owner.connection._check_list
                 if pair.remote_addr == self.address]
        self.assertTrue(pairs)
        for pair in pairs:
            owner.connection.check_start_task(pair)
        await self.pending(owner)
        await self.assert_retired(left, owner)


if __name__ == "__main__":
    unittest.main()
