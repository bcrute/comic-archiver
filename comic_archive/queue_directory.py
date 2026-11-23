"""Open the desktop worker's queue files against the directories it selected."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import stat

from . import windows_privacy
from .archive import atomic_json
from .archive_paths import pinned_publication
from .models import ArchiveError
from .osdep import NOFOLLOW, NONBLOCK
from .state_paths import queue_directory_identity, queue_entry_identity


@contextmanager
def open_queue(path, identity, *, private=False):
    """Pin a POSIX directory by descriptor. Windows pins the path during each operation."""
    if os.name != 'posix':
        yield QueueDirectory(path, identity, None)
        return
    if '..' in path.parts:
        raise ArchiveError('The worker queue must use ordinary directories.')
    common = os.O_DIRECTORY | NOFOLLOW | os.O_CLOEXEC
    flags = common | os.O_RDONLY
    # Android app data sits below searchable directories that the app cannot
    # list. Pin ancestors without asking for read access to their entries.
    traverse = common | getattr(os, 'O_PATH', os.O_RDONLY)
    parts = path.parts[1:]
    fd = os.open(path.anchor, traverse if parts else flags)
    try:
        for index, part in enumerate(parts):
            child = os.open(part, flags if index == len(parts) - 1 else traverse, dir_fd=fd)
            os.close(fd)
            fd = child
        if queue_entry_identity(os.fstat(fd)) != identity:
            raise ArchiveError('The worker queue changed during startup.')
        if private:
            os.fchmod(fd, 0o700)
        yield QueueDirectory(path, identity, fd)
    finally:
        os.close(fd)


def _ordinary(entry):
    return stat.S_ISREG(entry.st_mode) and not (
        getattr(entry, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class QueueDirectory:
    def __init__(self, path, identity, fd):
        self.path, self.identity, self.fd = path, identity, fd

    @contextmanager
    def _location(self, name):
        if Path(name).name != name or name in ('', '.', '..'):
            raise ArchiveError('Invalid worker queue filename.')
        if self.fd is not None:
            yield name, self.fd
        else:
            with pinned_publication(self.path):
                if queue_directory_identity(self.path) != self.identity:
                    raise ArchiveError('The worker queue changed before access.')
                yield self.path / name, None

    def requests(self):
        if self.fd is not None:
            names = os.listdir(self.fd)
        else:
            with self._location('requests.json'):
                names = os.listdir(self.path)
        return sorted(name for name in names if name.endswith('.json'))

    def directory(self, name):
        with self._location(name) as (path, fd):
            try:
                os.mkdir(path, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            identity = queue_entry_identity(os.stat(path, dir_fd=fd, follow_symlinks=False))
        return self.path / name, identity

    def entry(self, name):
        with self._location(name) as (path, fd):
            try:
                return os.stat(path, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None

    def ordinary(self, name):
        entry = self.entry(name)
        return entry is not None and _ordinary(entry)

    def read(self, name, maximum):
        with self._location(name) as (path, fd):
            before = os.stat(path, dir_fd=fd, follow_symlinks=False)
            if not _ordinary(before):
                raise ArchiveError('Worker queue files must be ordinary files.')
            if fd is None:
                descriptor = windows_privacy.open_pinned_read(path)
            else:
                descriptor = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=fd)
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not _ordinary(opened) or not os.path.samestat(before, opened):
                    raise ArchiveError('A worker queue file changed before it could be read.')
                if opened.st_size > maximum:
                    raise ArchiveError('Worker request is too large.')
                data = stream.read(maximum + 1)
                if len(data) > maximum:
                    raise ArchiveError('Worker request is too large.')
                return data

    def write(self, name, value):
        with self._location(name) as (path, fd):
            if fd is None:
                atomic_json(path, value, pinned=True)
                path.chmod(0o600)
                return
            temporary = '.queue-' + secrets.token_hex(16) + '.tmp'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def unlink(self, name):
        with self._location(name) as (path, fd):
            os.unlink(path, dir_fd=fd)
