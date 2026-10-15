"""Output path checks shared by archive publication and process locks."""
from .models import ArchiveError


def directory(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError("Refusing a symlink in archive output.")
    path.mkdir(parents=True, exist_ok=True)
    return path
