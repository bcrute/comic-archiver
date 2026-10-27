"""CL-1 bounded binary ranges. These are file/credit primitives, not peer auth."""

import struct
import time
import uuid

from .files import SourceError

HEADER = struct.Struct("!4sB3x16sQII")
FRAME_MAX = 16384
PAYLOAD_MAX = FRAME_MAX - HEADER.size
WINDOW_MAX = 256 * 1024
RANGE_MAX = 4 * 1024**2
SAFE_INTEGER = 2**53 - 1


def _require(value, code="integrity_failed"):
    if not value:
        raise SourceError(code)


def _id(value):
    try:
        parsed = uuid.UUID(value)
        _require(parsed.version == 4 and str(parsed) == value)
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise SourceError("integrity_failed") from None


def encode_frame(read_id, offset, sequence, data, terminal):
    _require(
        type(offset) is int
        and 0 <= offset <= SAFE_INTEGER
        and type(sequence) is int
        and 0 <= sequence < 2**32
        and isinstance(data, bytes)
        and 0 < len(data) <= PAYLOAD_MAX
        and offset + len(data) <= SAFE_INTEGER
        and type(terminal) is bool
    )
    return (
        HEADER.pack(
            b"CLB1", int(terminal), _id(read_id).bytes, offset, sequence, len(data)
        )
        + data
    )


def decode_frame(data):
    _require(isinstance(data, bytes) and HEADER.size < len(data) <= FRAME_MAX)
    magic, flags, key, offset, sequence, count = HEADER.unpack(data[: HEADER.size])
    read_id = str(uuid.UUID(bytes=key))
    _require(
        magic == b"CLB1"
        and flags in {0, 1}
        and data[5:8] == b"\0\0\0"
        and offset + count <= SAFE_INTEGER
        and count == len(data) - HEADER.size
    )
    _id(read_id)
    return read_id, offset, sequence, data[HEADER.size :], bool(flags)


class ReadRange:
    """Owns a real pinned file; caller must supply/recheck actual authorization.

    No network listener/session or account authority is constructed here. The
    callback is required before and after bytes, including credit and idle polls.
    Production pairing/lease implementation must supply it before enabling RPC.
    Single-owner interface: serialize each range/file on its owned worker. Do
    not close/reuse an FD concurrently with an in-flight read; cancellation must
    signal revocation then join that worker before retirement. No worker exists
    in this primitive module and no asynchronous lifecycle is claimed here.
    """

    def __init__(self, source, offset, length, authorize, clock=time.monotonic):
        self.source, self.authorize, self.clock = source, authorize, clock
        self.credit, self.sequence, self.closed = 0, 0, False
        self.read_id = str(uuid.uuid4())
        now = clock()
        self.deadline, self.activity = now + 60, now
        try:
            _require(
                type(offset) is int
                and type(length) is int
                and 0 <= offset <= SAFE_INTEGER
                and 0 < length <= RANGE_MAX
                and offset + length <= source.stamp.size,
                "resource_limit",
            )
            self.offset, self.end = offset, offset + length
            self._current()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.closed = True
        self.credit = 0
        self.source.close()

    def _current(self):
        _require(not self.closed, "cancelled")
        now = self.clock()
        _require(now < self.deadline and now - self.activity < 30, "cancelled")
        _require(self.authorize() is True, "revoked")
        self.source.current()

    def poll(self):
        try:
            self._current()
        except BaseException:
            self.close()
            raise

    def add_credit(self, count):
        try:
            self._current()
            _require(
                type(count) is int and count > 0 and self.credit + count <= WINDOW_MAX,
                "resource_limit",
            )
            self.credit += count
            self.activity = self.clock()
        except BaseException:
            self.close()
            raise

    def next_frame(self):
        try:
            self._current()
            if self.credit == 0:
                return None
            length = min(PAYLOAD_MAX, self.credit, self.end - self.offset)
            data = self.source.chunk(self.offset, length)
            self._current()
            final = self.offset + length == self.end
            value = encode_frame(self.read_id, self.offset, self.sequence, data, final)
            self.offset += length
            self.sequence += 1
            self.credit -= length
            self.activity = self.clock()
            if final:
                self.close()
            return value
        except BaseException:
            self.close()
            raise
