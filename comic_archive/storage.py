"""Device-space planning and persistent, explicit storage choices; no network."""
import json
from pathlib import Path
from .image_profiles import validate_profile
from .models import ArchiveError

DEFAULTS = {'schema_version': 1, 'image_profile': 'original', 'purge_completed_cache': True}


def safe_root(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ArchiveError('Storage paths must not be symlinked.')
    return root


def policy(root):
    path = safe_root(root) / '.archive-state/storage-policy.json'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Storage settings must not be symlinked.')
    if not path.exists():
        return dict(DEFAULTS)
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text())
        if (set(value) != set(DEFAULTS) or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['purge_completed_cache']) is not bool):
            raise ValueError
        validate_profile(value['image_profile'])
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ArchiveError('Storage settings are invalid; existing files are retained.') from None


