"""A reentrant process lock shared by CLI jobs, stores and browser workers."""
import os
import threading
import time
import math
from contextlib import contextmanager
from pathlib import Path
from . import osdep
from .archive_paths import directory
from .errors import LibraryBusy
from .models import ArchiveError


_held = {}
_guard = threading.Lock()


@contextmanager
def wait_for_library(root, seconds=0):
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds < 0:
        raise ArchiveError('Library lock wait must be a finite nonnegative number.')
    deadline = time.monotonic() + seconds
    lock = LibraryLock(root)
    while True:
        try:
            lock.__enter__()
            break
        except LibraryBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(1,deadline-time.monotonic()))
    try:
        yield lock
    finally:
        lock.__exit__(None,None,None)


class LibraryLock:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.key = (os.getpid(), str(self.root))
        self.entered = False
        self.depth = 0

    def __enter__(self):
        with _guard:
            owner = threading.get_ident()
            if self.key in _held:
                record = _held[self.key]
                if record[0] != owner:
                    raise LibraryBusy("Another job is writing this library; the pending queue is retained.")
                record[2] += 1
            else:
                directory(self.root)
                state = directory(self.root / ".archive-state")
                state.chmod(0o700)
                osdep.hide(state)
                fd = os.open(state / "writer.lock", os.O_CREAT | os.O_RDWR | osdep.NOFOLLOW, 0o600)
                try:
                    osdep.private_file(fd)
                    osdep.lock(fd)
                except BlockingIOError:
                    os.close(fd)
                    raise LibraryBusy("Another job is writing this library; the pending queue is retained.") from None
                except BaseException:
                    os.close(fd)
                    raise
                _held[self.key] = [owner, fd, 1]
            self.entered = True
            self.depth += 1
        return self

    def __exit__(self, *args):
        if not self.depth:
            return
        with _guard:
            record = _held[self.key]
            record[2] -= 1
            if record[2] == 0:
                osdep.unlock(record[1])
                os.close(record[1])
                del _held[self.key]
            self.depth -= 1
            self.entered = bool(self.depth)
