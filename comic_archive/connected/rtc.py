"""Actual aiortc/DTLS host peer and bounded read-worker ownership.

Only this factory consumes the actual controller's verified signaling queue and
creates/observes a native connected peer. No constructor accepts a Peer/Lease/
Account JSON description, SDP permission flag or injected positive transport.
"""

import asyncio
import hashlib
import queue
import re
import threading
import time
from importlib.metadata import version

from aioice import Connection
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCIceTransport,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.rtcsctptransport import (
    DataChunk,
    RTCSctpTransport,
    tsn_plus_one,
    uint32_gt,
)
from aiortc.sdp import SessionDescription

from .files import SourceError
from .ice import servers as checked_servers
from .jsoncodec import require
from .read_worker import FileWorker
from .rpc import CONTROL_MAX
from .wire import FRAME_MAX, WINDOW_MAX

_CREATE = object()


class _IceRetirement:
    """Retire real pinned SDK check tasks before their UDP owners close.

    aioice 0.10.2 closes protocols without joining outstanding triggered checks.
    The per-connection adapter only reduces work during retirement. It never
    supplies successful ICE, DTLS, local approval or file-access authority.
    """

    def __init__(self, token, connection):
        require(token is _CREATE and isinstance(connection, Connection))
        self.connection, self.retiring, self.joined = connection, False, False
        self._closing = None
        self._original_close = connection.close
        self._original_start = connection.check_start_task
        self._original_incoming = connection.check_incoming
        self._original_periodic = connection.check_periodic

    @classmethod
    def own(cls, transport):
        require(isinstance(transport, RTCIceTransport))
        require(version("aioice") == "0.10.2", "unsupported_format")
        connection = transport._connection
        require(isinstance(connection, Connection))
        require(getattr(connection, "_ink_retirement", None) is None)
        result = cls(_CREATE, connection)
        connection._ink_retirement = result
        connection.close = result.close
        connection.check_start_task = result.start
        connection.check_incoming = result.incoming
        connection.check_periodic = result.periodic
        return result

    def start(self, pair):
        if not self.retiring:
            self._original_start(pair)

    def incoming(self, *args):
        if not self.retiring:
            self._original_incoming(*args)

    def periodic(self):
        return False if self.retiring else self._original_periodic()

    async def _drain(self):
        # Scheduling is already stopped on THIS connection, including incoming
        # triggered checks. Cancellation unwinds native Transaction.run finally
        # blocks, retiring their real retransmission timers before socket close.
        tasks = {
            pair.task for pair in self.connection._check_list if pair.task is not None
        }
        require(all(isinstance(task, asyncio.Task) for task in tasks))
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3)
            require(not pending, "connection_unavailable")
            for task in tasks:
                if not task.cancelled():
                    require(task.exception() is None, "connection_unavailable")
        protocols = tuple(self.connection._protocols)
        await self._original_close()  # Also cancels/joins actual consent work.
        require(self.connection._closed, "connection_unavailable")
        # The real protocol's connection_lost future works for both ordinary
        # UDP and TurnTransport, which has no is_closing() API.
        require(
            all(
                not protocol.transactions
                and protocol._StunProtocol__closed.done()
                and not protocol._StunProtocol__closed.cancelled()
                and protocol._StunProtocol__closed.result() is True
                for protocol in protocols
            ),
            "connection_unavailable",
        )
        self.joined = True

    async def close(self):
        if self._closing is None:
            self.retiring = True
            self._closing = asyncio.create_task(self._drain())
        await asyncio.shield(self._closing)  # Unknown close remains retained.


class _SctpReceiveBounds:
    """Bound actual pinned-SDK bookkeeping before it retains a DATA chunk.

    This per-transport adapter supplies no peer, permission or successful packet.
    DTLS authentication, SCTP parsing and acknowledgements remain in aiortc.
    """

    MAX_STREAMS = 2
    MAX_REASSEMBLY_PARTS = 128
    MAX_MISORDERED = 128
    MAX_DUPLICATES_PER_PACKET = 64

    def __init__(self, token, transport, deny):
        require(token is _CREATE and isinstance(transport, RTCSctpTransport))
        self.transport, self._deny = transport, deny
        self._original = transport._receive_data_chunk
        self.denied, self.retired, self.peaks = None, False, self._measure()

    @classmethod
    def own(cls, transport, deny):
        require(isinstance(transport, RTCSctpTransport))
        require(version("aiortc") == "1.15.0", "unsupported_format")
        require(
            callable(deny)
            and callable(transport._receive_data_chunk)
            and getattr(transport._receive_data_chunk, "__self__", None) is transport
            and isinstance(transport._inbound_streams, dict)
            and isinstance(transport._sack_misordered, set)
            and isinstance(transport._sack_duplicates, list)
            and getattr(transport, "_ink_receive_bounds", None) is None
        )
        result = cls(_CREATE, transport, deny)
        transport._ink_receive_bounds = result
        transport._receive_data_chunk = result.receive
        return result

    def _measure(self):
        pieces = [
            part
            for stream in self.transport._inbound_streams.values()
            for part in stream.reassembly
        ]
        return dict(
            streams=len(self.transport._inbound_streams),
            parts=len(pieces),
            bytes=sum(len(part.user_data) for part in pieces),
            misordered=len(self.transport._sack_misordered),
            duplicates=len(self.transport._sack_duplicates),
        )

    def sample(self):
        return self._measure() | {
            "denied": self.denied,
            "retired": self.retired,
            "peaks": dict(self.peaks),
        }

    def retire(self):
        if self.retired:
            return
        self._observe()
        self.transport._inbound_streams.clear()
        self.transport._sack_misordered.clear()
        self.transport._sack_duplicates.clear()
        self.transport._sack_needed = False
        self.retired = True

    def _observe(self):
        for name, value in self._measure().items():
            self.peaks[name] = max(self.peaks[name], value)

    async def receive(self, chunk):
        if self.retired:
            return
        require(isinstance(chunk, DataChunk))
        transport = self.transport
        last = transport._last_received_tsn
        duplicate = last is not None and (
            not uint32_gt(chunk.tsn, last) or chunk.tsn in transport._sack_misordered
        )
        streams = transport._inbound_streams
        pieces = [part for stream in streams.values() for part in stream.reassembly]
        projected_misordered = (
            not duplicate
            and last is not None
            and chunk.tsn != tsn_plus_one(last)
            and len(transport._sack_misordered) >= self.MAX_MISORDERED
        )
        reason = None
        if (
            duplicate
            and len(transport._sack_duplicates) >= self.MAX_DUPLICATES_PER_PACKET
        ):
            reason = "duplicates"
        elif projected_misordered:
            reason = "misordered"
        elif (
            not duplicate
            and len(streams) + (chunk.stream_id not in streams) > self.MAX_STREAMS
        ):
            reason = "streams"
        elif not duplicate and not 0 < len(chunk.user_data) <= CONTROL_MAX:
            reason = "chunk_bytes"
        elif not duplicate and len(pieces) >= self.MAX_REASSEMBLY_PARTS:
            reason = "parts"
        elif not duplicate and (
            sum(len(part.user_data) for part in pieces) + len(chunk.user_data)
            > CONTROL_MAX
        ):
            reason = "reassembly_bytes"
        if reason is not None:
            self.denied = reason
            self.retire()
            self._deny()
            return
        await self._original(chunk)
        self._observe()


def data_sdp(text):
    require(isinstance(text, str) and 0 < len(text.encode("utf-8")) <= 65536)
    parsed = SessionDescription.parse(text)
    require(
        len(parsed.media) == 1
        and parsed.media[0].kind == "application"
        and parsed.media[0].port != 0
        and parsed.media[0].profile in {"UDP/DTLS/SCTP", "DTLS/SCTP"}
    )
    dtls = parsed.media[0].dtls
    require(dtls is not None and 1 <= len(dtls.fingerprints) <= 6)
    sha256 = set()
    for fingerprint in dtls.fingerprints:
        size = {"sha-256": 32, "sha-384": 48, "sha-512": 64}.get(
            fingerprint.algorithm.lower()
        )
        require(
            size is not None
            and re.fullmatch(
                r"(?:[0-9A-Fa-f]{2}:){" + str(size - 1) + r"}[0-9A-Fa-f]{2}",
                fingerprint.value,
            )
        )
        if size == 32:
            sha256.add(fingerprint.value.upper())
    require(len(sha256) == 1)
    return next(iter(sha256))  # Describes signed SDP only, not accepted DTLS proof.


def configuration(servers):
    values = [RTCIceServer(**item) for item in checked_servers(servers)]
    return RTCConfiguration(iceServers=values)  # Explicit [], no public STUN fallback.


class _ReadGate:
    def __init__(self, token, peer):
        require(token is _CREATE)
        self.peer, self.retired = peer, threading.Event()
        self._deadline, self._shares, self._lock = 0.0, frozenset(), threading.Lock()

    def refresh(self):
        # Read the actual held controller/socket request-start permission and
        # current local store on the event-loop owner, never from a passed DTO.
        require(self.peer._physical_current(), "revoked")
        controller, pair = self.peer.controller, self.peer.pairing_id
        row = controller.owner._record(pair)
        bound = self.peer._runner.transport.binding
        require(
            row is not None
            and row["status"] == "approved"
            and row["generation"] == bound["generation"]
            and row["reader_keys"] == bound["reader_keys"]
            and row["account_binding"] == bound["account_binding"]
            and row["shares"] == bound["shares"],
            "revoked",
        )
        deadline = controller.service_deadline(pair)
        require(time.monotonic() < deadline, "revoked")
        with self._lock:
            require(not self.retired.is_set(), "revoked")
            self._deadline, self._shares = deadline, frozenset(row["shares"])

    def authorize(self, share):
        # Safe on the actual file thread; local stop sets this event before IO,
        # service expiry uses the actual request's monotonic deadline.
        with self._lock:
            return (
                not self.retired.is_set()
                and time.monotonic() < self._deadline
                and (share is None or share in self._shares)
            )

    def remaining_ms(self):
        with self._lock:
            return (
                max(0, min(10000, int((self._deadline - time.monotonic()) * 1000)))
                if not self.retired.is_set()
                else 0
            )

    def retire(self):
        self.retired.set()


class HostPeer:
    def __init__(self, token, controller, runner, pc):
        require(token is _CREATE and isinstance(pc, RTCPeerConnection))
        self.controller, self._runner, self._pc = controller, runner, pc
        self.pairing_id, self.session_challenge = runner.pairing_id, None
        self.control, self.data, self.worker = None, None, None
        self.task, self._closing, self.joined = None, None, False
        self._ready = asyncio.get_running_loop().create_future()
        self._gate, self._stopped = _ReadGate(_CREATE, self), False
        self._incoming, self._channels = queue.Queue(maxsize=8), set()
        self._remote_sdp, self._local_sdp = None, None
        self._failure, self._native_close, self._cleaning = None, None, False
        self._ice_retirement, self._sctp_bounds = None, None

    @classmethod
    async def open(cls, controller, pairing_id, ice_servers=None):
        from .controller import HostController

        require(isinstance(controller, HostController))
        controller.current()
        runner = controller._runners.get(pairing_id)
        require(
            runner is not None
            and runner.pending is None
            and runner.current()
            and runner.peer is None,
            "revoked",
        )
        peer = cls(
            _CREATE,
            controller,
            runner,
            RTCPeerConnection(
                configuration([] if ice_servers is None else ice_servers)
            ),
        )
        runner.peer = (
            peer  # Actual native handle retained BEFORE first negotiation await.
        )
        peer.task = asyncio.create_task(peer._run())
        try:
            return await asyncio.wait_for(asyncio.shield(peer._ready), 45)
        except BaseException:
            peer.invalidate()
            # Caller cancellation never drops the actual negotiation/cleanup task.
            raise

    def _physical_current(self):
        try:
            return (
                not self._stopped
                and self._runner.current()
                and self._runner.peer is self
                and self._pc.connectionState == "connected"
                and self._pc.signalingState == "stable"
                and self._pc.sctp is not None
                and self._pc.sctp.transport.state == "connected"
                and self._pc.sctp.state == "connected"
                and self.control is not None
                and self.data is not None
                and self.control.readyState == self.data.readyState == "open"
                and self._pc.remoteDescription.sdp == self._remote_sdp
                and self._pc.localDescription.sdp == self._local_sdp
            )
        except Exception:
            return False

    def current(self):
        return self._physical_current() and self._gate.authorize(None)

    def invalidate(self):
        self._stopped = True
        self._gate.retire()
        if self._sctp_bounds is not None:
            self._sctp_bounds.retire()
        if self.worker is not None:
            self.worker.stop()
        # Network retirement is independent of a possibly blocked file read;
        # retain its actual close task without retiring any worker FD early.
        if self._native_close is None:
            self._native_close = asyncio.create_task(self._pc.close())
        if (
            self.task is not None
            and self.task is not asyncio.current_task()
            and not self.task.done()
            and not self._cleaning
            and self.task.cancelling() == 0
        ):
            self.task.cancel()

    def _channel(self, channel):
        try:
            require(
                channel.label in {"cl1-control", "cl1-data"}
                and channel.label not in self._channels
                and channel.ordered
                and channel.maxRetransmits is None
                and channel.maxPacketLifeTime is None
                and not channel.negotiated
                and channel.protocol == ""
            )
            self._channels.add(channel.label)
            if channel.label == "cl1-control":
                self.control = channel
                channel.on("message", self._message)
            else:
                self.data = channel
                channel.on(
                    "message", lambda _: self.invalidate()
                )  # Host never accepts comic bytes.
            channel.on("close", self.invalidate)
        except Exception:
            self.invalidate()

    def _limit_sctp(self):
        transport = self._pc.sctp
        require(transport is not None)
        self._sctp_bounds = _SctpReceiveBounds.own(transport, self.invalidate)

    def _message(self, value):
        try:
            require(
                not self._stopped
                and isinstance(value, str)
                and len(value.encode("utf-8")) <= CONTROL_MAX
            )
            self._incoming.put_nowait(value)
        except Exception:
            self.invalidate()

    async def _run(self):
        try:
            self._pc.on("datachannel", self._channel)
            self._pc.on("connectionstatechange", self._state)
            claims = await asyncio.wait_for(
                self.controller.receive_signal(self.pairing_id), 20
            )
            require(
                claims["kind"] == "offer" and self._runner.current(), "auth_required"
            )
            offer = claims["body"][
                "sdp"
            ]  # Already verified by actual native SignalSession.
            data_sdp(offer)
            bound = self._runner.transport.binding
            require(
                bound is not None
                and not bound["pending"]
                and claims["challenge"] == bound["challenge"]
            )
            self.session_challenge = bound["challenge"]
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer, type="offer")
            )
            self._remote_sdp = self._pc.remoteDescription.sdp
            self._ice_retirement = _IceRetirement.own(self._pc.sctp.transport.transport)
            self._limit_sctp()  # BEFORE local ICE/DTLS/SCTP can admit remote bytes.
            await asyncio.wait_for(
                self._pc.setLocalDescription(await self._pc.createAnswer()), 20
            )
            self._local_sdp = self._pc.localDescription.sdp
            data_sdp(self._local_sdp)
            await self.controller.send_signal(
                self.pairing_id,
                "answer",
                dict(
                    sdp=self._local_sdp,
                    sha256=hashlib.sha256(self._local_sdp.encode("utf-8")).hexdigest(),
                ),
            )
            until = asyncio.get_running_loop().time() + 20
            while not self._physical_current():
                require(
                    not self._stopped
                    and self._runner.current()
                    and asyncio.get_running_loop().time() < until,
                    "connection_unavailable",
                )
                await asyncio.sleep(0.01)
            # Actual connected DTLS has validated the signed remote SDP's cert
            # fingerprints inside aiortc. A connected flag supplied by data never
            # reaches this constructor or satisfies the private native checks.
            while self.controller.service_deadline(self.pairing_id) <= time.monotonic():
                require(
                    self._physical_current()
                    and asyncio.get_running_loop().time() < until,
                    "revoked",
                )
                await asyncio.sleep(0.01)
            self._gate.refresh()
            row = self.controller.owner._record(self.pairing_id)
            indices = {
                share: self.controller.owner._indices[share] for share in row["shares"]
            }
            self.worker = FileWorker.start(self, indices)
            await self.worker.wait_ready()
            require(self.current(), "revoked")
            self._ready.set_result(self)
            while self.current():
                self._gate.refresh()
                try:
                    text = self._incoming.get_nowait()
                except queue.Empty:
                    text = None
                if text is not None:
                    result = await self.worker.call("control", text)
                    self._send(result)
                # Always poll streams, even at full bufferedAmount: idle/fixed
                # deadlines must retire without waiting for another credit.
                result = await self.worker.call("frame", self.data.bufferedAmount)
                if result is not None:
                    self._send(result)
                if text is None and result is None:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self._failure = "cancelled"
            raise
        except SourceError as error:
            self._failure = error.code
        except Exception:
            self._failure = "connection_unavailable"
        finally:
            self.invalidate()
            if not self._ready.done():
                self._ready.set_exception(
                    SourceError(self._failure or "connection_unavailable")
                )
            await self._cleanup()

    def _state(self):
        if self._pc.connectionState in {"failed", "closed", "disconnected"}:
            self.invalidate()

    def _send(self, result):
        if result is None:
            return
        self._gate.refresh()  # Actual owner/lease/native SDP again AFTER worker IO.
        require(self.current(), "revoked")
        kind, value = result
        channel, maximum = (
            (self.control, CONTROL_MAX) if kind == "control" else (self.data, FRAME_MAX)
        )
        size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
        require(
            0 < size <= maximum and channel.bufferedAmount + size <= WINDOW_MAX,
            "resource_limit",
        )
        channel.send(value)
        require(channel.bufferedAmount <= WINDOW_MAX, "resource_limit")

    async def _cleanup(self):
        self._cleaning = True
        self.invalidate()
        # Receiver SDK callbacks can no longer admit. File worker MUST really
        # exit before pinned sources/indices/FDs retire; unknown joins propagate.
        failure = None
        if self.worker is not None:
            try:
                await self.worker.close()
            except BaseException as error:
                failure = error
        if self._native_close is None:
            self._native_close = asyncio.create_task(self._pc.close())
        done, _ = await asyncio.wait({self._native_close}, timeout=8)
        require(self._native_close in done, "connection_unavailable")
        self._native_close.result()
        require(self._pc.connectionState == "closed", "connection_unavailable")
        require(
            self._ice_retirement is None or self._ice_retirement.joined,
            "connection_unavailable",
        )
        if failure is not None:
            raise failure
        self.joined = True
        # Retire the whole immutable signaling negotiation; a new peer must
        # get a fresh service-owned session/challenge and actual SDP exchange.
        self._runner.transport.invalidate()

    async def close(self):
        self.invalidate()
        if self.task is not None and self.task is not asyncio.current_task():
            if not self.task.done():
                if not self._cleaning and self.task.cancelling() == 0:
                    self.task.cancel()
                done, _ = await asyncio.wait({self.task}, timeout=12)
                require(self.task in done, "connection_unavailable")
            if not self.task.cancelled():
                self.task.exception()
        if not self.joined:
            if (
                self._closing is None
                or self._closing.done()
                and (self._closing.cancelled() or self._closing.exception() is not None)
            ):
                self._closing = asyncio.create_task(self._cleanup())
            done, _ = await asyncio.wait({self._closing}, timeout=12)
            require(self._closing in done, "connection_unavailable")
            self._closing.result()
        require(self.joined, "connection_unavailable")
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()
