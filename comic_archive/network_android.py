"""Android metering probe: reads a status file the Kotlin service maintains (frozen seam).

The download service writes an app-private JSON file with the connectivity the platform
reports; ``probe`` returns the shape comic_archive.network expects. A missing, stale-shaped,
symlinked or unreadable file means *unknown*, which the core's policy holds: transfers stay
stopped until the network is verified unmetered.
"""
import json
from pathlib import Path

UNKNOWN = {"connection": "unknown", "metered": None}
_ALLOWED = {"wifi", "wired", "cellular"}
_path = {"value": None}


def configure(path) -> None:
    """Record the app-private status file; called once at startup by the bridge."""
    _path["value"] = str(Path(path)) if path is not None else None


def configured() -> bool:
    """Whether the app registered its status file; until then the desktop probe applies."""
    return _path.get("value") is not None


def _status_file(path: Path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        return None
    try:
        if not path.is_file() or path.stat().st_size > 4096:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None


def probe():
    """The connection facts for this moment; never guesses beyond what the file states."""
    value = _path.get("value")
    if value is None:
        return dict(UNKNOWN)
    observed = _status_file(Path(value))
    if not isinstance(observed, dict) or observed.get("connection") not in _ALLOWED \
            or type(observed.get("metered")) is not bool:
        return dict(UNKNOWN)
    return {"connection": observed["connection"], "metered": observed["metered"]}
