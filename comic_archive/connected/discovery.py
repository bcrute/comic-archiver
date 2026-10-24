"""Opt-in completed-publication discovery; no peer/account/read authority.

Only selected Archiver Library roots are scanned. One actual owned thread opens
its own bound private SQLite connections; hashing never blocks the host loop.
"""

import asyncio
import os
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
POLL_SECONDS = 5
JOIN_SECONDS = 8


def _stamp(root, relative):
    source = root.open(relative)
    try:
        return source.stamp
    finally:
        source.close()


class CompletedScan:
    """Owned-thread scan/cache over an actual local Index, never wire DATA."""

    def __init__(self, index):
        _require(type(index) is Index)
        self.index, self.cache = index, {}

    def run(self, stopped):
        index, root = self.index, self.index.root
        root.current()
        value = os.stat(index.path, follow_symlinks=False)
        _require((value.st_dev, value.st_ino) == index.identity and value.st_nlink == 1)
        seen, entries, books, registered = set(), 0, 0, 0
        with os.scandir(root.fd) as folders:
            for folder in folders:
                if stopped.is_set():
                    return registered
                entries += 1
                _require(entries <= ENTRIES_MAX, "resource_limit")
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
                            _require(entries <= ENTRIES_MAX, "resource_limit")
                            if (
                                child.name.startswith(".")
                                or not child.name.lower().endswith(".cbz")
                                or not child.is_file(follow_symlinks=False)
                            ):
                                continue
                            books += 1
                            _require(books <= BOOKS_MAX, "resource_limit")
                            relative = folder.name + "/" + child.name
                            seen.add(relative)
                            try:
                                fingerprint = self._fingerprint(relative, series_stamp)
                                if self.cache.get(relative) == fingerprint:
                                    continue
                                # The existing completion verifier owns ZIP/hash/version
                                # acceptance. A bare CBZ never falls back to standalone.
                                index.register_completed(relative, title)
                                registered += 1
                                if fingerprint == self._fingerprint(relative, _stamp(root, metadata)):
                                    self.cache[relative] = fingerprint
                            except SourceError as error:
                                if error.code == "resource_limit":
                                    raise
                                self.cache.pop(relative, None)
                except (OSError, ValueError, SourceError) as error:
                    if isinstance(error, SourceError) and error.code == "resource_limit":
                        raise
                    # Incomplete/malformed publication or a moved folder: try next
                    # pass. Original identities are never tombstoned by a scan.
                finally:
                    if directory is not None:
                        os.close(directory)
        self.cache = {name: value for name, value in self.cache.items() if name in seen}
        root.current()
        return registered

    def _fingerprint(self, relative, series_stamp):
        return (
            _stamp(self.index.root, relative),
            _stamp(self.index.root, relative[:-4] + ".json"),
            series_stamp,
        )


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
        self.owned = []
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
            self.ready.set()
            while not self.stopped.is_set():
                for scan in scans:
                    if self.stopped.is_set():
                        break
                    scan.run(self.stopped)
                self.stopped.wait(POLL_SECONDS)
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

    async def monitor(self):
        while self.thread.is_alive():
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
