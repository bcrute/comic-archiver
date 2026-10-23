"""Bounded actual file/SQLite thread. An actual accepted peer owns admission.

No public JSON/native-resource constructor or generic executor. Cancellation
retires permission first; unknown joins retain the actual thread/FD sources.
"""
import asyncio
from concurrent.futures import Future
import queue
import threading

from .files import SourceError, read_source
from .jsoncodec import require
from .rpc import Session

_CREATE = object()


class FileWorker:
    def __init__(self, token, peer, indices):
        require(token is _CREATE and peer.current() and 1 <= len(indices) <= 16, 'revoked')
        self.peer = peer
        self.sources, self.commands, self.stopped = {}, queue.Queue(maxsize=8), threading.Event()
        self.ready, self.error, self.active = threading.Event(), None, None
        self.cleanup_error, self._owned_indices = False, {}
        self.thread = threading.Thread(target=self._run, name='comic-reader-files', daemon=False)
        self.started, self.joined, self._wrapped = False, False, None
        try:
            for share, index in indices.items():
                require(peer._gate.authorize(share), 'revoked')
                source = read_source(index)  # Actual selected Index on its owner thread.
                self.sources[share] = source
                require(source.share == share)
        except BaseException:
            if not self.thread.is_alive():
                for source in self.sources.values():
                    source.close()
            raise

    @classmethod
    def start(cls, peer, indices):
        # Local import avoids a constructor accepting an arbitrary callback/DTO.
        from .rtc import HostPeer
        require(isinstance(peer, HostPeer) and peer.current(), 'revoked')
        worker = cls(_CREATE, peer, indices)
        peer.worker = worker  # Retain actual thread/FD sources BEFORE native start.
        try:
            worker.thread.start()
            worker.started = True
        except BaseException:
            if not worker.thread.is_alive():
                for source in worker.sources.values():
                    source.close()
            raise
        return worker

    def stop(self):
        self.stopped.set()
        self.peer._gate.retire()

    def _run(self):
        indices, session = self._owned_indices, None
        try:
            for share, source in self.sources.items():
                require(not self.stopped.is_set() and self.peer._gate.authorize(share), 'revoked')
                indices[share] = source.open()
            session = Session(indices, self.peer._gate.authorize, self.peer.session_challenge, lease_remaining=self.peer._gate.remaining_ms)
            self.ready.set()
            while not self.stopped.is_set():
                try:
                    job, future = self.commands.get(timeout=0.1)
                except queue.Empty:
                    if not self.peer._gate.authorize(None):
                        break
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    require(not self.stopped.is_set() and self.peer._gate.authorize(None), 'revoked')
                    kind, value = job
                    if kind == 'control':
                        result = ('control', session.handle(value))
                    elif kind == 'frame':
                        result = None
                        for read_id in list(session.streams):
                            try:
                                frame = session.next_frame(read_id, value)
                                if frame is not None:
                                    result = ('data', frame)
                                    break
                            except SourceError as error:
                                result = ('control', session.read_error(read_id, error))
                                break
                    else:
                        raise SourceError('integrity_failed')
                    require(not self.stopped.is_set() and self.peer._gate.authorize(None), 'revoked')
                    future.set_result(result)
                except BaseException as error:
                    future.set_exception(error if isinstance(error, SourceError) else SourceError('integrity_failed'))
        except BaseException as error:
            self.error = error.code if isinstance(error, SourceError) else 'integrity_failed'
            self.stopped.set()
            self.peer._gate.retire()
        finally:
            # Actual running IO has returned before any range/index/root closes.
            if session is not None:
                try:
                    session.close()
                except BaseException:
                    self.cleanup_error = True
            for index in indices.values():
                try:
                    index.close()
                except BaseException:
                    self.cleanup_error = True
                try:
                    index.root.close()
                except BaseException:
                    self.cleanup_error = True
            while True:
                try:
                    _, future = self.commands.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(SourceError('cancelled'))
            self.ready.set()

    async def wait_ready(self):
        until = asyncio.get_running_loop().time() + 5
        while not self.ready.is_set():
            require(self.thread.is_alive() and asyncio.get_running_loop().time() < until, 'connection_unavailable')
            await asyncio.sleep(0.01)
        require(self.error is None and not self.stopped.is_set() and self.thread.is_alive(), 'connection_unavailable')

    async def call(self, kind, value):
        require(self.peer.current() and not self.stopped.is_set() and self.thread.is_alive(), 'revoked')
        require(self.active is None or self.active.done(), 'busy')
        future = Future()
        self.active = future  # Retained before enqueue/await; never cancel running IO.
        try:
            self.commands.put_nowait(((kind, value), future))
        except queue.Full:
            future.set_exception(SourceError('busy'))
            raise SourceError('busy') from None
        self._wrapped = asyncio.wrap_future(future)
        result = await asyncio.shield(self._wrapped)
        require(self.peer.current(), 'revoked')
        return result

    async def close(self):
        self.stop()
        until = asyncio.get_running_loop().time() + 8
        while self.thread.is_alive():
            require(asyncio.get_running_loop().time() < until, 'connection_unavailable')
            await asyncio.sleep(0.01)
        if self.thread.ident is not None:
            self.thread.join(timeout=0)  # Actual observed terminal thread, never block loop.
        if self.active is not None and self.active.done() and not self.active.cancelled():
            self.active.exception()
        if self._wrapped is not None and self._wrapped.done() and not self._wrapped.cancelled():
            self._wrapped.exception()
        require(not self.cleanup_error, 'connection_unavailable')
        for source in self.sources.values():
            source.close()  # Only AFTER actual worker exit/owned range cleanup.
        self.joined = True
