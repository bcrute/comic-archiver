"""Actual UDP/ICE retirement, not an admitted application peer or file grant."""

import asyncio
import unittest
from struct import pack

from aiortc import (
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceGatherer,
    RTCIceTransport,
    RTCPeerConnection,
)
from aiortc.rtcsctptransport import (
    SCTP_DATA_FIRST_FRAG,
    WEBRTC_STRING,
    DataChunk,
    crc32c,
    tsn_plus_one,
)

from comic_archive.connected.rpc import CONTROL_MAX
from comic_archive.connected.rtc import _IceRetirement, _SctpReceiveBounds


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
        while not (
            self.sink.received > 0
            and any(protocol.transactions for protocol in owner.connection._protocols)
        ):
            self.assertLess(self.loop.time(), end)
            await asyncio.sleep(0.01)
        self.assertTrue(
            any(
                pair.task and not pair.task.done()
                for pair in owner.connection._check_list
            )
        )

    def silent_candidate(self):
        return RTCIceCandidate(
            component=1,
            foundation="owned-silent",
            priority=1,
            ip=self.address[0],
            port=self.address[1],
            protocol="udp",
            type="host",
        )

    async def assert_retired(self, transport, owner):
        protocols = tuple(owner.connection._protocols)
        tasks = {
            pair.task for pair in owner.connection._check_list if pair.task is not None
        }
        await asyncio.wait_for(transport.stop(), 5)
        self.assertTrue(owner.joined)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertTrue(
            all(
                not protocol.transactions and protocol.transport.is_closing()
                for protocol in protocols
            )
        )
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
        await asyncio.wait_for(
            asyncio.gather(
                left.start(second.getLocalParameters()),
                right.start(first.getLocalParameters()),
            ),
            8,
        )
        self.assertEqual((left.state, right.state), ("completed", "completed"))
        await left.addRemoteCandidate(self.silent_candidate())
        pairs = [
            pair
            for pair in owner.connection._check_list
            if pair.remote_addr == self.address
        ]
        self.assertTrue(pairs)
        for pair in pairs:
            owner.connection.check_start_task(pair)
        await self.pending(owner)
        await self.assert_retired(left, owner)


class ConnectedSctpBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pcs, self.owners, self.errors = [], [], []
        self.loop = asyncio.get_running_loop()
        self.previous = self.loop.get_exception_handler()

        def failed(loop, context):
            self.errors.append(context)
            if self.previous is None:
                loop.default_exception_handler(context)
            else:
                self.previous(loop, context)

        self.loop.set_exception_handler(failed)

    async def asyncTearDown(self):
        try:
            for pc in reversed(self.pcs):
                await asyncio.wait_for(pc.close(), 5)
            for owner in self.owners:
                self.assertTrue(owner.joined)
                self.assertTrue(owner.connection._closed)
                self.assertTrue(
                    all(
                        not protocol.transactions and protocol.transport.is_closing()
                        for protocol in owner.connection._protocols
                    )
                )
            for pc in self.pcs:
                self.assertEqual(pc.connectionState, "closed")
                if pc.sctp is not None:
                    self.assertIsNone(pc.sctp._t1_handle)
                    self.assertIsNone(pc.sctp._t2_handle)
                    self.assertIsNone(pc.sctp._t3_handle)
            await asyncio.sleep(0)
            self.assertEqual(self.errors, [])
        finally:
            self.loop.set_exception_handler(self.previous)

    async def wait_for(self, predicate, maximum=8):
        end = self.loop.time() + maximum
        while not predicate():
            self.assertLess(self.loop.time(), end)
            await asyncio.sleep(0.01)

    async def pair(self, bounded=True):
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pcs.extend((sender, receiver))
        channels, messages = {}, []

        @receiver.on("datachannel")
        def opened(channel):
            channels[channel.label] = channel
            channel.on("message", lambda value: messages.append((channel.label, value)))

        control = sender.createDataChannel("cl1-control")
        data = sender.createDataChannel("cl1-data")
        self.owners.append(_IceRetirement.own(sender.sctp.transport.transport))
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        self.owners.append(_IceRetirement.own(receiver.sctp.transport.transport))
        bounds = (
            _SctpReceiveBounds.own(
                receiver.sctp, lambda: asyncio.create_task(receiver.close())
            )
            if bounded
            else None
        )
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)
        await self.wait_for(
            lambda: (
                sender.connectionState == receiver.connectionState == "connected"
                and control.readyState == data.readyState == "open"
                and set(channels) == {"cl1-control", "cl1-data"}
            )
        )
        return sender, receiver, control, data, channels, messages, bounds

    def chunk(self, sender, stream, size, flags=0, tsn=None):
        chunk = DataChunk()
        chunk.flags = flags
        if tsn is None:
            tsn = sender.sctp._local_tsn
            sender.sctp._local_tsn = tsn_plus_one(tsn)
        chunk.tsn = tsn
        chunk.stream_id = stream.id
        chunk.stream_seq = sender.sctp._outbound_stream_seq.get(stream.id, 0)
        chunk.protocol = WEBRTC_STRING
        chunk.user_data = b"x" * size
        return chunk

    async def send_packet(self, sender, chunks):
        sctp = sender.sctp
        header = pack(
            "!HHL", sctp._local_port, sctp._remote_port, sctp._remote_verification_tag
        )
        body = b"".join(bytes(chunk) for chunk in chunks)
        checksum = crc32c(header + b"\x00\x00\x00\x00" + body)
        await sctp.transport._send_data(header + pack("<L", checksum) + body)

    async def send_body(self, sender, body):
        sctp = sender.sctp
        header = pack(
            "!HHL", sctp._local_port, sctp._remote_port, sctp._remote_verification_tag
        )
        checksum = crc32c(header + b"\x00\x00\x00\x00" + body)
        await sctp.transport._send_data(header + pack("<L", checksum) + body)

    async def fill_bytes(self, sender, control, count):
        first = True
        while count:
            size = min(1200, count)
            await sender.sctp._send_chunk(
                self.chunk(sender, control, size, SCTP_DATA_FIRST_FRAG if first else 0)
            )
            first, count = False, count - size

    @staticmethod
    def actual_reassembly(transport):
        pieces = [
            part
            for stream in transport._inbound_streams.values()
            for part in stream.reassembly
        ]
        return {
            "parts": len(pieces),
            "bytes": sum(len(part.user_data) for part in pieces),
        }

    def assert_released(self, observed):
        self.assertTrue(observed["retired"])
        self.assertEqual(
            tuple(
                observed[name]
                for name in ("streams", "parts", "bytes", "misordered", "duplicates")
            ),
            (0, 0, 0, 0, 0),
        )

    async def test_ordinary_two_channel_messages_and_close(self):
        sender, receiver, control, data, _, messages, bounds = await self.pair()
        before = bounds.sample()
        await sender.sctp.transport._send_data(
            b"\x00" * 16
        )  # Authenticated, bad checksum.
        await self.send_body(
            sender, pack("!BBH", 0, 0, 3)
        )  # Valid checksum, bad length.
        await asyncio.sleep(0.05)
        self.assertEqual(bounds.sample(), before)
        self.assertEqual(receiver.connectionState, "connected")
        control.send("request")
        data.send(b"frame")
        await self.wait_for(lambda: len(messages) == 2)
        self.assertEqual(
            set(messages), {("cl1-control", "request"), ("cl1-data", b"frame")}
        )
        observed = bounds.sample()
        self.assertIsNone(observed["denied"])
        self.assertEqual(
            (observed["streams"], observed["parts"], observed["bytes"]), (2, 0, 0)
        )

    async def test_reassembly_bytes_stop_at_bound_over_real_dtls(self):
        sender, _, control, _, _, _, bounds = await self.pair()
        await self.fill_bytes(sender, control, CONTROL_MAX)
        await self.wait_for(lambda: bounds.sample()["bytes"] == CONTROL_MAX)
        await sender.sctp._send_chunk(self.chunk(sender, control, 1))
        await self.wait_for(lambda: bounds.denied is not None)
        observed = bounds.sample()
        self.assertEqual(observed["denied"], "reassembly_bytes")
        self.assertEqual(observed["peaks"]["bytes"], CONTROL_MAX)
        self.assert_released(observed)

    async def test_unguarded_same_byte_oracle_characterizes_sdk_exposure(self):
        sender, receiver, control, _, _, _, _ = await self.pair(bounded=False)
        await self.fill_bytes(sender, control, CONTROL_MAX + 1)
        await self.wait_for(
            lambda: self.actual_reassembly(receiver.sctp)["bytes"] > CONTROL_MAX
        )
        observed = self.actual_reassembly(receiver.sctp)
        self.assertFalse(False or observed["bytes"] <= CONTROL_MAX)

    async def test_fragment_count_stops_at_bound_over_real_dtls(self):
        sender, _, control, _, _, _, bounds = await self.pair()
        for index in range(bounds.MAX_REASSEMBLY_PARTS + 1):
            await sender.sctp._send_chunk(
                self.chunk(
                    sender, control, 1, SCTP_DATA_FIRST_FRAG if index == 0 else 0
                )
            )
        await self.wait_for(lambda: bounds.denied is not None)
        observed = bounds.sample()
        self.assertEqual(observed["denied"], "parts")
        self.assertEqual(observed["peaks"]["parts"], bounds.MAX_REASSEMBLY_PARTS)
        self.assert_released(observed)

    async def test_misordered_tsn_count_stops_at_bound_over_real_dtls(self):
        sender, _, control, _, _, _, bounds = await self.pair()
        tsn = tsn_plus_one(sender.sctp._local_tsn)  # Keep one genuine gap open.
        for _ in range(bounds.MAX_MISORDERED + 1):
            await sender.sctp._send_chunk(self.chunk(sender, control, 1, tsn=tsn))
            tsn = tsn_plus_one(tsn)
        sender.sctp._local_tsn = tsn
        await self.wait_for(lambda: bounds.denied is not None)
        observed = bounds.sample()
        self.assertEqual(observed["denied"], "misordered")
        self.assertEqual(observed["peaks"]["misordered"], bounds.MAX_MISORDERED)
        self.assert_released(observed)

    async def test_duplicate_chunks_in_one_real_packet_stop_at_bound(self):
        sender, receiver, control, _, _, _, bounds = await self.pair()
        duplicate = receiver.sctp._last_received_tsn
        chunks = [
            self.chunk(sender, control, 1, tsn=duplicate)
            for _ in range(bounds.MAX_DUPLICATES_PER_PACKET + 1)
        ]
        await self.send_packet(sender, chunks)
        await self.wait_for(lambda: bounds.denied is not None)
        observed = bounds.sample()
        self.assertEqual(observed["denied"], "duplicates")
        self.assertEqual(
            observed["peaks"]["duplicates"], bounds.MAX_DUPLICATES_PER_PACKET
        )
        self.assert_released(observed)

    async def test_third_native_data_channel_is_denied_before_stream_allocation(self):
        sender, _, _, _, _, _, bounds = await self.pair()
        sender.createDataChannel("third")
        await self.wait_for(lambda: bounds.denied is not None)
        observed = bounds.sample()
        self.assertEqual(observed["denied"], "streams")
        self.assertEqual(observed["peaks"]["streams"], bounds.MAX_STREAMS)
        self.assert_released(observed)


if __name__ == "__main__":
    unittest.main()
