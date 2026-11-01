"""Output path checks shared by archive publication and process locks."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import threading

from .errors import LibraryMoved
from .models import ArchiveError

# The library folders open in this process. One that is missing was renamed, moved or deleted while open:
# making it again would start a second, empty library where the first one was.
_kept = Counter()
_guard = threading.Lock()


def keep(folder):
    """Never make this folder again while it is kept; returns it as kept, for release."""
    folder = Path(folder).absolute()
    with _guard:
        _kept[folder] += 1
    return folder


def release(folder):
    with _guard:
        _kept[folder] -= 1
        if _kept[folder] <= 0:
            del _kept[folder]


@contextmanager
def kept(folder):
    folder = keep(folder)
    try:
        yield folder
    finally:
        release(folder)


def directory(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError("Refusing a symlink in archive output.")
    if not path.is_dir():
        absolute = Path(path).absolute()
        with _guard:
            folders = list(_kept)
        if any((absolute == folder or folder in absolute.parents) and not folder.is_dir() for folder in folders):
            raise LibraryMoved()
    path.mkdir(parents=True, exist_ok=True)
    return path
