"""Serialized CL-1 endpoint RPC over actual indices/ranges, not peer authentication.

An authenticated RTC/lease owner must supply the authorization callback and own
all calls on the same file/SQLite worker. No listener is started by this module.
"""
from collections import OrderedDict
import hashlib
import time
import uuid

from .files import ARCHIVE_MAX, Index, SourceError
from .jsoncodec import dumps, loads
from .wire import FRAME_MAX, RANGE_MAX, WINDOW_MAX, ReadRange, _id

CONTROL_MAX = 65536
SEEN_MAX = 65536
CACHE_MAX = 1024**2
ERRORS = frozenset({'host_offline', 'connection_unavailable', 'auth_required', 'revoked', 'share_removed',
                    'not_found', 'version_changed', 'catalog_changed', 'unsupported_format',
                    'resource_limit', 'busy', 'cancelled', 'integrity_failed'})


def _require(value, code='integrity_failed'):
    if not value:
        raise SourceError(code)


def parse_control(text):
    try:
        value = loads(text, CONTROL_MAX)
        _require(isinstance(value, dict) and type(value.get('v')) is int and value['v'] == 1
                 and isinstance(value.get('op'), str) and isinstance(value.get('args'), dict))
        _id(value.get('id'))
        return value
    except (ValueError, UnicodeError, RecursionError, SourceError):
        raise SourceError('integrity_failed') from None


def encode_control(value):
    return dumps(value, CONTROL_MAX)


def _integer(value, low, high):
    _require(type(value) is int and low <= value <= high, 'resource_limit')
    return value


class Session:
    """Single-worker protocol owner. A policy callback is not account/peer proof."""

    def __init__(self, indices, authorize, session_challenge, clock=time.monotonic, lease_remaining=None):
        _require(isinstance(indices, dict) and 1 <= len(indices) <= 16)
        _require(all(isinstance(index, Index) and share == index.share_id for share, index in indices.items()))
        _id(session_challenge)
        self.challenge = session_challenge
        self._lease_remaining = lease_remaining
        self.indices, self.authorize, self.clock = dict(indices), authorize, clock
        self.closed, self.negotiated = False, False
        self.archive_max = ARCHIVE_MAX
        self.streams, self.snapshots, self.cursors = {}, {}, {}
        self.seen, self.cache = {}, OrderedDict()
        self.cache_bytes = 0

    def close(self):
        self.closed = True
        for stream, _ in self.streams.values():
            stream.close()
        self.streams.clear()
        self.snapshots.clear()
        self.cursors.clear()
        self.cache.clear()
        self.seen.clear()
        self.cache_bytes = 0

    def _authorized(self, share):
        try:
            return self.authorize(share) is True
        except Exception:
            return False

    def _current(self, share=None):
        _require(not self.closed, 'cancelled')
        if not self._authorized(None):
            self.close()
            raise SourceError('revoked')
        if share is not None and (share not in self.indices or not self._authorized(share)):
            for read_id, (stream, stream_share) in list(self.streams.items()):
                if stream_share == share:
                    stream.close()
                    del self.streams[read_id]
            for key, snapshot in list(self.snapshots.items()):
                if snapshot['share'] == share:
                    del self.snapshots[key]
                    self.cursors = {token: value for token, value in self.cursors.items() if value[0] != key}
            raise SourceError('share_removed')

    def _reply(self, request, result=None, error=None):
        value = {'v': 1, 'id': request['id']}
        if error:
            value['error'] = {'code': error, 'retryable': error in {'busy', 'host_offline', 'connection_unavailable', 'catalog_changed'}}
        else:
            value['ok'] = result
        return encode_control(value)

    def handle(self, text):
        try:
            request = parse_control(text)
        except SourceError:
            self.close()
            raise
        self._current()
        fingerprint = hashlib.sha256(text.encode('utf-8')).digest()
        prior = self.seen.get(request['id'])
        if prior is not None:
            if prior[0] != fingerprint:
                self.close()
                raise SourceError('integrity_failed')
            for share in prior[1]:
                self._current(share)
            if request['id'] not in self.cache:
                return self._reply(request, error='busy')  # Never execute an evicted operation again.
            return self.cache[request['id']]
        _require(len(self.seen) < SEEN_MAX, 'busy')
        args = request['args']
        scope = set()
        if isinstance(args.get('share_id'), str) and args['share_id'] in self.indices:
            scope.add(args['share_id'])
        read_id = args.get('read_id')
        if isinstance(read_id, str) and read_id in self.streams:
            scope.add(self.streams[read_id][1])
        if request['op'] == 'hello':
            scope.update(share for share in self.indices if self._authorized(share))
        self.seen[request['id']] = (fingerprint, scope)  # BEFORE any read/credit mutation.
        try:
            result = self._dispatch(request)
            self._current()
            if request['op'] == 'hello':
                scope.update(result['shares'])
            for share in scope:
                self._current(share)
            reply = self._reply(request, result=result)
        except SourceError as error:
            reply = self._reply(request, error=error.code if error.code in ERRORS else 'integrity_failed')
        if self.closed:
            return reply
        self.cache[request['id']] = reply
        self.cache_bytes += len(reply.encode('utf-8'))
        while len(self.cache) > 128 or self.cache_bytes > CACHE_MAX:
            _, retired = self.cache.popitem(last=False)
            self.cache_bytes -= len(retired.encode('utf-8'))
        return reply

    def _share(self, args):
        share = args.get('share_id')
        _id(share)
        self._current(share)
        return share, self.indices[share]

    def _dispatch(self, request):
        op, args = request['op'], request['args']
        if op in {'hello', 'ping', 'access.lease'}:
            _require(args.get('session_challenge') == self.challenge, 'auth_required')
        if op == 'access.lease':
            _require(set(args)=={'session_challenge'} and callable(self._lease_remaining), 'unsupported_format')
            remaining = _integer(self._lease_remaining(), 1, 10000)
            return dict(expires_in_ms=remaining, session_challenge=self.challenge)
        if op == 'hello':
            _require(not self.negotiated, 'busy')
            majors = args.get('supported_majors')
            _require(isinstance(majors, list) and len(majors) <= 8
                     and all(type(value) is int for value in majors) and 1 in majors, 'unsupported_format')
            limits = args.get('limits', {})
            _require(isinstance(limits, dict))
            self.archive_max = _integer(limits.get('archive_bytes', ARCHIVE_MAX), 1, ARCHIVE_MAX)
            self.negotiated = True
            return dict(major=1, limits=dict(archive_bytes=self.archive_max, range_bytes=RANGE_MAX,
                        frame_bytes=FRAME_MAX, window_bytes=WINDOW_MAX),
                        shares=[share for share in self.indices if self._authorized(share)],
                        catalog_revisions={share: index.revision for share, index in self.indices.items() if self._authorized(share)})
        _require(self.negotiated, 'auth_required')
        if op == 'catalog.list':
            return self._catalog(args)
        if op in {'book.describe', 'read.open'}:
            share, index = self._share(args)
            book, version = args.get('book_id'), args.get('content_version')
            _id(book)
            _id(version)
            record = index.describe(book, version)
            _require(record['byte_length'] <= self.archive_max, 'resource_limit')
            if op == 'book.describe':
                return record
            _require(len(self.streams) < 2, 'busy')
            offset = _integer(args.get('offset'), 0, record['byte_length'] - 1)
            length = _integer(args.get('length'), 1, min(RANGE_MAX, record['byte_length'] - offset))
            source = index.open_original(book, version)
            stream = ReadRange(source, offset, length, lambda: self._allowed(share), self.clock)
            self.streams[stream.read_id] = (stream, share)
            return dict(read_id=stream.read_id, offset=offset, length=length, content_version=version,
                        expires_in_ms=max(0, int((stream.deadline - self.clock()) * 1000)))
        if op in {'read.credit', 'read.cancel'}:
            read_id = args.get('read_id')
            _id(read_id)
            entry = self.streams.get(read_id)
            if op == 'read.cancel':
                if entry:
                    entry[0].close()
                    del self.streams[read_id]
                return {}
            _require(entry is not None, 'not_found')
            stream, share = entry
            self._current(share)
            try:
                stream.add_credit(args.get('bytes'))
            except SourceError:
                stream.close()
                del self.streams[read_id]
                raise
            return {}
        if op == 'ping':
            return {'available': True}
        raise SourceError('unsupported_format')

    def _allowed(self, share):
        self._current(share)
        return True

    def _expire(self):
        now = self.clock()
        for key, snapshot in list(self.snapshots.items()):
            if now >= snapshot['deadline']:
                del self.snapshots[key]
                self.cursors = {token: value for token, value in self.cursors.items() if value[0] != key}

    def _catalog(self, args):
        share, index = self._share(args)
        limit = _integer(args.get('limit', 50), 1, 50)
        self._expire()
        cursor = args.get('cursor')
        if cursor is None:
            _require(len(self.snapshots) < 2, 'busy')
            revision, entries = index.snapshot()
            key = str(uuid.uuid4())
            snapshot = dict(share=share, revision=revision, entries=entries, deadline=self.clock() + 60, tokens={}, limit=limit)
            self.snapshots[key] = snapshot
            start = 0
        else:
            _require(isinstance(cursor, str) and len(cursor) <= 256 and cursor in self.cursors, 'catalog_changed')
            key, start = self.cursors[cursor]
            snapshot = self.snapshots.get(key)
            _require(snapshot is not None and snapshot['share'] == share and snapshot['limit'] == limit, 'catalog_changed')
        _require(index.revision == snapshot['revision'], 'catalog_changed')
        entries = []
        for record in snapshot['entries'][start:start + limit]:
            proposed = dict(entries=entries + [record], revision=snapshot['revision'], next='x' * 36)
            try:
                page_bytes = len(encode_control({'v': 1, 'id': 'x' * 36, 'ok': proposed}).encode('utf-8'))
            except SourceError:
                break
            if page_bytes > CONTROL_MAX - 128:
                break
            entries.append(record)
        stop = start + len(entries)
        _require(entries or start == len(snapshot['entries']), 'resource_limit')
        next_cursor = None
        if stop < len(snapshot['entries']):
            if stop not in snapshot['tokens']:
                _require(len(self.cursors) < 1024, 'busy')
                token = str(uuid.uuid4())
                snapshot['tokens'][stop] = token
                self.cursors[token] = (key, stop)
            next_cursor = snapshot['tokens'][stop]
        self._current(share)
        if next_cursor is None:
            self.snapshots.pop(key, None)
            self.cursors = {token: value for token, value in self.cursors.items() if value[0] != key}
        return dict(entries=entries, revision=snapshot['revision'], next=next_cursor)

    def next_frame(self, read_id, buffered_amount=0):
        """Caller sends frame only while its actual channel remains authorized.

        File IO and close must stay on this worker; no concurrent FD retirement.
        RTC bufferedAmount is supplied by the transport before each single send.
        """
        _id(read_id)
        _integer(buffered_amount, 0, 2**53 - 1)
        self._current()
        entry = self.streams.get(read_id)
        _require(entry is not None, 'not_found')
        stream, share = entry
        try:
            self._current(share)
            stream.poll()
            if buffered_amount > WINDOW_MAX - FRAME_MAX:
                return None
            value = stream.next_frame()
            self._current(share)
            if stream.closed:
                self.streams.pop(read_id, None)
            return value
        except SourceError:
            stream.close()
            self.streams.pop(read_id, None)
            raise

    def read_error(self, read_id, error):
        """Safe async data-path event; caller must close/retire that real read."""
        _id(read_id)
        entry = self.streams.pop(read_id, None)
        if entry:
            entry[0].close()
        code = error.code if isinstance(error, SourceError) and error.code in ERRORS else 'integrity_failed'
        return encode_control({'v': 1, 'event': 'read.error', 'read_id': read_id, 'error': {'code': code, 'retryable': False}})
