"""Actual bounded outbound WSS signaling; no RTC or file-read authority.

One event-loop thread owns this connection and PairingOwner. Its service lease
response is tied to an actual socket/request/monotonic start. The RTC/file owner
must additionally verify its actual peer, local scope and joined worker lifecycle.
No offline queue, automatic reconnect, token logging or private-key export.
"""
import asyncio
import re
import time
import uuid
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.connection import ClientConnection
from websockets.protocol import State

from .files import SourceError
from .jsoncodec import dumps, loads, require
from .pairing import PairingOwner
from .wire import _id

WIRE = 196608
FRAME = WIRE + 2048
_CREATE = object()


class PinnedConnect(connect):
    """Pinned15.0.1 extension: credentials never follow any redirect."""
    def process_redirect(self, exc):
        return exc


class UnclosedSocket(SourceError):
    """Retains the ACTUAL native handle after unknown open cleanup."""
    def __init__(self, token, socket):
        require(token is _CREATE and isinstance(socket, ClientConnection))
        super().__init__('connection_unavailable')
        self.socket = socket

    async def close(self):
        await self.socket.close()
        require(self.socket.state is State.CLOSED, 'connection_unavailable')


class HostRendezvous:
    def __init__(self, token, owner, pairing_id, socket, pending, draft):
        require(token is _CREATE and isinstance(owner, PairingOwner) and isinstance(socket, ClientConnection))
        self.owner, self.pairing_id, self._socket, self.pending = owner, pairing_id, socket, pending
        self._binding, self._closed, self._seq, self._incoming = None, False, 0, 0
        self._draft, self._bundle_sent = draft, False
        self._lease_request, self._service_deadline = None, 0.0
        self._receive_busy, self._send_lock = False, asyncio.Lock()

    @classmethod
    async def open(cls, owner, origin, pairing_id, host_token=None, preview_token=None):
        require(isinstance(owner, PairingOwner))
        proof, pending = owner.control_socket(pairing_id, origin, host_token)
        draft = owner.drafts.get(pairing_id) if pending else None
        parsed = urlsplit(origin)
        headers = {'Authorization': 'CL1-Proof ' + proof}
        if preview_token is not None:
            require(isinstance(preview_token, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', preview_token))
            headers['Cookie'] = 'ink_preview=' + preview_token
        url = ('wss' if parsed.scheme == 'https' else 'ws') + '://' + parsed.netloc + '/api/connected-library/v1/signals'
        # Explicit proxy/compression/ping policy; app heartbeats are serialized
        # with normal traffic, never an invisible extra background owner.
        socket = await PinnedConnect(url, additional_headers=headers, subprotocols=['cl1.signal.v1'],
                               origin=None, proxy=None, compression=None, ping_interval=None,
                               max_size=FRAME, max_queue=2, write_limit=32768,
                               open_timeout=5, close_timeout=1, user_agent_header=None)
        try:
            require(socket.subprotocol == 'cl1.signal.v1')
            ready = loads(await asyncio.wait_for(socket.recv(), 5), 1024)
            require(set(ready) == {'v','type','connection_id','pending'} and ready['v'] == 1
                    and type(ready['v']) is int and ready['type'] == 'ready' and ready['pending'] is pending)
            _id(ready['connection_id'])
            result = cls(_CREATE, owner, pairing_id, socket, pending, draft)
            result._current()
            return result
        except BaseException:
            try:
                await socket.close()
                require(socket.state is State.CLOSED, 'connection_unavailable')
            except BaseException:
                raise UnclosedSocket(_CREATE, socket) from None
            raise

    def _current(self):
        require(not self._closed and self._socket.state is State.OPEN, 'cancelled')
        self.owner.current()
        row = self.owner._record(self.pairing_id)
        require(row is not None and row['status'] in {'pending','approved'} and row['generation'] == 0, 'revoked')
        if self.pending:
            draft = self._draft
            require(draft is not None and self.owner.clock() < draft.deadline, 'revoked')
            if row['status'] == 'pending':
                require(self.owner.drafts.get(self.pairing_id) is draft and self.owner._live(draft), 'revoked')
            else:
                # The existing native connection may carry the ALREADY SEALED
                # pair.accept after approval retires bootstrap crypto. Keep the
                # actual original draft/review; rows cannot recreate this path.
                # It never becomes active or grants a lease/new approval.
                review = draft.review
                require(draft.closed and review is not None and review.used and draft.approval_id == row['approval_id']
                        and review.digest == row['request_sha256'] and review.account_binding == row['account_binding']
                        and review.reader_keys == row['reader_keys'], 'revoked')
        else:
            require(row['status'] == 'approved', 'revoked')
            if self._binding is not None:
                require(row['account_binding'] == self._binding['account_binding']
                        and row['reader_keys'] == self._binding['reader_keys']
                        and row['shares'] == self._binding['shares'], 'revoked')
        return row

    async def _send(self, value):
        self._current()
        async with self._send_lock:
            self._current()
            try:
                await asyncio.wait_for(self._socket.send(dumps(value, FRAME)), 1)
                self._current()
            except BaseException:
                self._service_deadline = 0
                await self.close()
                raise

    @property
    def binding(self):
        self._current()
        return None if self._binding is None else loads(dumps(self._binding, 4096), 4096)

    @property
    def renewal_deadline(self):
        self._current()
        return None if self._lease_request is None else self._lease_request[2] + 10

    async def attest(self):
        self._current()
        require(self._binding is not None and not self._bundle_sent, 'integrity_failed')
        proof = self.owner.state.identity.attest(self._binding['challenge'], 'host', int(self.owner.wall() * 1000))
        self._bundle_sent = True
        await self._send(dict(v=1, type='bundle', session_id=self._binding['session_id'], proof=proof))

    def invalidate(self):
        # Synchronous permission retirement; the actual network owner still owes
        # awaited close/receive-task join before reporting cleanup.
        self._closed, self._service_deadline, self._lease_request = True, 0.0, None

    async def heartbeat(self):
        await self._send(dict(v=1, type='heartbeat', id=str(uuid.uuid4())))

    async def signal(self, ciphertext):
        require(self._binding is not None and isinstance(ciphertext, str) and 0 < len(ciphertext) <= WIRE)
        require(self._seq < 256, 'resource_limit')
        sequence = self._seq
        # Spend before awaiting: a lost send cannot re-use this sequence.
        self._seq += 1
        await self._send(dict(v=1, type='signal', session_id=self._binding['session_id'], seq=sequence, ciphertext=ciphertext))

    async def renew(self):
        self._current()
        require(not self.pending and self._binding is not None and self._lease_request is None, 'revoked')
        request, challenge = str(uuid.uuid4()), str(uuid.uuid4())
        # This is the actual request's start, never response receipt wall time.
        self._lease_request = (request, challenge, time.monotonic())
        await self._send(dict(v=1, type='lease', session_id=self._binding['session_id'], request_id=request, challenge=challenge))

    @property
    def service_deadline(self):
        """Service permission deadline only; never a standalone file-read grant."""
        self._current()
        return self._service_deadline

    async def receive(self):
        require(not self._receive_busy, 'resource_limit')
        self._receive_busy = True
        try:
            self._current()
            # A forgotten renewal cannot keep the connection looking usable.
            maximum = 45 if self._lease_request is None else max(0.001, self._lease_request[2] + 10 - time.monotonic())
            wire = await asyncio.wait_for(self._socket.recv(), maximum)
            value = loads(wire, FRAME)
            require(isinstance(value, dict) and type(value.get('v')) is int and value['v'] == 1)
            self._current()
            kind = value.get('type')
            if kind == 'peer':
                require(set(value) == {'v','type','binding'} and self._binding is None)
                binding = value['binding']
                require(isinstance(binding, dict) and set(binding) == {'pairing_id','session_id','challenge','account_binding',
                    'host_keys','reader_keys','generation','shares','pending'} and binding['pairing_id'] == self.pairing_id
                    and binding['pending'] is self.pending and binding['host_keys'] == self.owner.state.identity.public_bundle()
                    and type(binding['generation']) is int and binding['generation'] == 0 and isinstance(binding['shares'], list) and len(binding['shares']) <= 16)
                for name in ['pairing_id','session_id','challenge','account_binding']:
                    _id(binding[name])
                for share in binding['shares']:
                    _id(share)
                require(len(set(binding['shares'])) == len(binding['shares']))
                self._binding = binding
                self._current()
            elif kind == 'signal':
                require(self._binding is not None and set(value) == {'v','type','session_id','seq','ciphertext'}
                        and value['session_id'] == self._binding['session_id'] and type(value['seq']) is int
                        and value['seq'] == self._incoming and self._incoming < 256
                        and isinstance(value['ciphertext'], str) and 0 < len(value['ciphertext']) <= WIRE)
                self._incoming += 1
            elif kind == 'heartbeat':
                require(set(value) == {'v','type','id'})
                _id(value['id'])
            elif kind == 'lease':
                request = self._lease_request
                require(not self.pending and request is not None and self._binding is not None
                        and set(value) == {'v','type','session_id','request_id','challenge','lease_id','ttl_ms','generation','shares'}
                        and value['session_id'] == self._binding['session_id'] and value['request_id'] == request[0]
                        and value['challenge'] == request[1] and type(value['ttl_ms']) is int and 0 < value['ttl_ms'] <= 10000
                        and value['generation'] == self._binding['generation'] and value['shares'] == self._binding['shares'])
                _id(value['lease_id'])
                self._lease_request = None  # Duplicate/unsolicited response never renews.
                deadline = request[2] + value['ttl_ms'] / 1000
                require(time.monotonic() < deadline, 'revoked')
                self._service_deadline = deadline
            else:
                raise SourceError('integrity_failed')
            return loads(dumps(value, FRAME), FRAME)  # Copy DATA, never RTC/file authority.
        except BaseException:
            self._service_deadline = 0
            await self.close()
            raise
        finally:
            self._receive_busy = False

    async def close(self):
        self.invalidate()
        # Actual library close awaited. Unknown close propagates, no join success.
        await self._socket.close()
        require(self._socket.state is State.CLOSED, 'connection_unavailable')
