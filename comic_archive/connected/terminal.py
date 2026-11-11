"""Bounded native POSIX TTY input; no thread blocked on input during shutdown."""

import asyncio
import os

from .files import SourceError
from .jsoncodec import require

_CREATE = object()
RETAINED = []


class TerminalInput:
    def __init__(self, token, fd):
        require(token is _CREATE)
        self.fd = fd
        self._loop = asyncio.get_running_loop()
        self._lines, self._buffer = asyncio.Queue(maxsize=4), bytearray()
        self._ended = False
        self._skip = False  # Inside a line already too long to be a command.

    @classmethod
    def open(cls, fd):
        require(
            os.name == "posix" and type(fd) is int and os.isatty(fd),
            "unsupported_format",
        )
        asyncio.get_running_loop()  # Refuse before acquiring an FD outside its actual owner loop.
        # The terminal opened anew, not a copy of the descriptor: a copy shares its settings with the shell
        # and with this program's own output, which would then stop waiting too, and stay so after a kill.
        try:
            own = os.open(
                os.ttyname(fd), os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
            )
        except OSError:
            raise SourceError("unsupported_format") from None
        result = cls(_CREATE, own)
        try:
            require(
                os.isatty(own) and os.fstat(own).st_rdev == os.fstat(fd).st_rdev,
                "unsupported_format",
            )
            result._loop.add_reader(own, result._read)
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
                    # A line that is too long or is not text is refused by itself; input keeps going.
                    try:
                        line = (
                            SourceError("invalid_command")
                            if self._skip
                            else bytes(self._buffer).decode("utf-8")
                        )
                    except UnicodeDecodeError:
                        line = SourceError("invalid_command")
                    self._skip = False
                    self._buffer.clear()
                    if self._lines.full():
                        # Typed ahead of a busy command: drop it all rather than
                        # run part of it, and say so. Input itself keeps going.
                        while not self._lines.empty():
                            self._lines.get_nowait()
                        self._lines.put_nowait(SourceError("busy"))
                    else:
                        self._lines.put_nowait(line)
                elif len(self._buffer) >= 4096:
                    self._buffer.clear()
                    self._skip = True
                elif not self._skip:
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
            os.close(self.fd)
            self.fd = None
