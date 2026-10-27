"""Bounded native POSIX TTY input; no thread blocked on input during shutdown."""

import asyncio
import os

from .files import SourceError
from .jsoncodec import require

_CREATE = object()
RETAINED = []


class TerminalInput:
    def __init__(self, token, fd, blocking):
        require(token is _CREATE)
        self.fd, self._blocking = fd, blocking
        self._loop = asyncio.get_running_loop()
        self._lines, self._buffer = asyncio.Queue(maxsize=4), bytearray()
        self._ended = False

    @classmethod
    def open(cls, fd):
        require(
            os.name == "posix" and type(fd) is int and os.isatty(fd),
            "unsupported_format",
        )
        asyncio.get_running_loop()  # Refuse before acquiring an FD outside its actual owner loop.
        duplicate = os.dup(fd)
        blocking = os.get_blocking(duplicate)
        result = cls(_CREATE, duplicate, blocking)
        try:
            require(os.isatty(duplicate), "unsupported_format")
            os.set_blocking(duplicate, False)
            result._loop.add_reader(duplicate, result._read)
            return result
        except BaseException:
            try:
                result.close()
            except BaseException:
                RETAINED.append(result)
                raise
            raise

    def _finish(self, error=None):
        self.close()
        # Overflow/EOF cannot leave queued approvals to execute after the local
        # input owner has gone. No command text is included in errors/logs.
        while not self._lines.empty():
            self._lines.get_nowait()
        self._lines.put_nowait(error)

    def _read(self):
        if self.fd is None:
            return
        try:
            value = os.read(self.fd, 1024)
            if not value:
                self._finish()
                return
            for byte in value:
                if byte == 10:
                    line = bytes(self._buffer).decode("utf-8")
                    self._buffer.clear()
                    if self._lines.full():
                        # Typed ahead of a busy command: drop it all rather than
                        # run part of it, and say so. Input itself keeps going.
                        while not self._lines.empty():
                            self._lines.get_nowait()
                        self._lines.put_nowait(SourceError("busy"))
                    else:
                        self._lines.put_nowait(line)
                else:
                    require(len(self._buffer) < 4096, "resource_limit")
                    self._buffer.append(byte)
        except BlockingIOError:
            return
        except Exception:
            self._finish(SourceError("resource_limit"))

    async def readline(self):
        if self._ended and self._lines.empty():
            return None
        value = await self._lines.get()
        if isinstance(value, SourceError):
            raise value
        return value

    def close(self):
        self._ended = True
        self._buffer.clear()
        if self.fd is not None:
            self._loop.remove_reader(self.fd)
            os.set_blocking(self.fd, self._blocking)
            os.close(self.fd)
            self.fd = None
