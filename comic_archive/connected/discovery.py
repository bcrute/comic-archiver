"""Opt-in completed-publication discovery; no peer/account/read authority.

Only selected Archiver Library roots are scanned. One actual owned thread opens
its own bound private SQLite connections; hashing never blocks the host loop.
"""

import asyncio
import os
import sqlite3
import stat
import threading

from ..publication import validate_public_metadata
from .files import (
    BOOKS_MAX,
    Index,
    SharedRoot,
    SourceError,
    _label,
    _object,
    _parts,
    _require,
)

ENTRIES_MAX = 50000
# A pass looks at every book's folder entry. While books keep arriving the next pass follows closely; over a
# quiet library the passes thin out to one a minute, as the app itself looks at a library on a hard disk.
POLL_SECONDS = 5
IDLE_SECONDS = 60
JOIN_SECONDS = 8
# A pass the disk or the index could not finish is tried again this many times, waiting longer each time.
PASS_RETRIES = 5
RETRY_SECONDS_MAX = 300


def _stamp(root, relative):
    source = root.open(relative)
    try:
        return source.stamp
    finally:
        source.close()


def _near(directory, name):
    """A file's stamp read through its open folder: no path walked from the library's top, nothing opened."""
    try:
        value = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        raise SourceError("not_found") from None
    _require(stat.S_ISREG(value.st_mode), "not_found")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def pause(quiet, failures):
    """Seconds until the next pass, after this many passes that found nothing new or could not finish."""
    if failures:
        return min(RETRY_SECONDS_MAX, POLL_SECONDS * 2**failures)
    return min(IDLE_SECONDS, POLL_SECONDS * 2 ** min(quiet, 4))


class CompletedScan:
    """Owned-thread scan/cache over an actual local Index, never wire DATA."""

    def __init__(self, index):
        _require(type(index) is Index)
        self.index, self.cache = index, {}
        # Whether the last pass met more than it may list; what it had listed stays served.
        self.limited = False
        self.full = set()  # Books the full index left out, until they change.

    def run(self, stopped):
        index, root = self.index, self.index.root
        root.current()
        value = os.stat(index.path, follow_symlinks=False)
        _require((value.st_dev, value.st_ino) == index.identity and value.st_nlink == 1)
        seen, entries, books, registered = set(), 0, 0, 0
        cut = False  # Whether the pass stopped at a bound.
        with os.scandir(root.fd) as folders:
            for folder in folders:
                if stopped.is_set():
                    return registered
                entries += 1
                if entries > ENTRIES_MAX:
                    cut = True
                    break
                if folder.name.startswith(".") or not folder.is_dir(follow_symlinks=False):
                    continue
                directory = None
                try:
                    _parts(folder.name)
                    directory = os.open(
                        folder.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=root.fd,
                    )
                    metadata = folder.name + "/series.json"
                    series_stamp = _stamp(root, metadata)
                    series = _object(root, metadata)
                    validate_public_metadata(series, series=True)
                    title = _label(series["title"])
                    _require(series_stamp == _stamp(root, metadata), "version_changed")
                    with os.scandir(directory) as children:
                        for child in children:
                            if stopped.is_set():
                                return registered
                            entries += 1
                            if entries > ENTRIES_MAX:
                                cut = True
                                break
                            if (
                                child.name.startswith(".")
                                or not child.name.lower().endswith(".cbz")
                                or not child.is_file(follow_symlinks=False)
                            ):
                                continue
                            books += 1
                            if books > BOOKS_MAX:
                                cut = True
                                break
                            relative = folder.name + "/" + child.name
                            seen.add(relative)
                            fingerprint = None
                            try:
                                fingerprint = self._fingerprint(directory, child.name, series_stamp)
                                if self.cache.get(relative) == fingerprint:
                                    continue
                                # The existing completion verifier owns ZIP/hash/version
                                # acceptance. A bare CBZ never falls back to standalone.
                                # One recorded as it still is (a host started again) is not read over.
                                if not index.unchanged(relative, title):
                                    index.register_completed(relative, title)
                                    registered += 1
                                self.full.discard(relative)
                                if fingerprint == self._fingerprint(directory, child.name, _stamp(root, metadata)):
                                    self.cache[relative] = fingerprint
                            except SourceError as error:
                                if error.code == "resource_limit":
                                    # The index is full: this book is left out until it changes.
                                    if fingerprint is not None:
                                        self.cache[relative] = fingerprint
                                        self.full.add(relative)
                                else:
                                    self.cache.pop(relative, None)
                            except sqlite3.Error:
                                self.cache.pop(relative, None)  # One book's record; tried next pass.
                except (OSError, ValueError, SourceError):
                    # Incomplete/malformed publication or a moved folder: try next
                    # pass. Original identities are never tombstoned by a scan.
                    pass
                finally:
                    if directory is not None:
                        os.close(directory)
                if cut:
                    break
        if not cut:
            # A pass cut short has not seen every book: nothing is forgotten on its word.
            self.cache = {name: value for name, value in self.cache.items() if name in seen}
            self.full &= seen
        self.limited = cut or bool(self.full)
        root.current()
        return registered

    @staticmethod
    def _fingerprint(directory, name, series_stamp):
        return (_near(directory, name), _near(directory, name[:-4] + ".json"), series_stamp)


class CatalogWatcher:
    """Retained local write owner; stop requests are not successful joins."""

    def __init__(self, indices):
        _require(1 <= len(indices) <= 16, "resource_limit")
        self.bindings = []
        for share, index in indices.items():
            _require(type(index) is Index and share == index.share_id)
            index.root.current()
            value = os.stat(index.path, follow_symlinks=False)
            _require((value.st_dev, value.st_ino) == index.identity)
            self.bindings.append(
                (index.root.path, index.root.identity, index.path, index.identity, share)
            )
        self.stopped, self.ready = threading.Event(), threading.Event()
        self.error, self.cleanup_error, self.joined = None, False, False
        self.owned, self.scans = [], []
        self.thread = threading.Thread(target=self._run, name="comic-reader-catalog", daemon=False)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def _run(self):
        try:
            for path, root_identity, private, identity, share in self.bindings:
                if self.stopped.is_set():
                    return
                root = SharedRoot(path)
                index = None
                self.owned.append((root, index))
                _require(root.identity == root_identity)
                value = os.stat(private, follow_symlinks=False)
                _require((value.st_dev, value.st_ino) == identity)
                index = Index(private, root, create=False)
                self.owned[-1] = root, index
                _require(index.identity == identity and index.share_id == share)
            scans = [CompletedScan(index) for _, index in self.owned]
            self.scans = scans
            self.ready.set()
            failures = quiet = 0
            while not self.stopped.is_set():
                try:
                    found = 0
                    for scan in scans:
                        if self.stopped.is_set():
                            break
                        found += scan.run(self.stopped)
                    failures, quiet = 0, 0 if found else quiet + 1
                except (OSError, sqlite3.Error):
                    # A busy index or a disk that did not answer; a changed library is a SourceError and ends it.
                    failures += 1
                    if failures > PASS_RETRIES:
                        raise
                self.stopped.wait(pause(quiet, failures))
        except BaseException as error:
            self.error = error.code if isinstance(error, SourceError) else "integrity_failed"
            self.stopped.set()
        finally:
            for root, index in self.owned:
                try:
                    if index is not None:
                        index.close()
                    root.close()
                except BaseException:
                    self.cleanup_error = True
            self.ready.set()

    @property
    def limited(self):
        return any(scan.limited for scan in self.scans)

    async def monitor(self, notice=None):
        """Wait for the worker to end; notice is called once if a library holds more than can be listed."""
        while self.thread.is_alive():
            if notice is not None and self.limited:
                notice()
                notice = None
            await asyncio.sleep(0.05)
        raise SourceError(self.error or "connection_unavailable")

    async def close(self):
        self.stop()
        until = asyncio.get_running_loop().time() + JOIN_SECONDS
        while self.thread.is_alive():
            _require(asyncio.get_running_loop().time() < until, "connection_unavailable")
            await asyncio.sleep(0.01)
        if self.thread.ident is not None:
            self.thread.join(timeout=0)
        _require(not self.cleanup_error, "connection_unavailable")
        self.joined = True
