"""In-app qualification checks, run inside the built app on an emulator or device.

scripts/android_feasibility.py launches the app with the selfcheck extra and collects the JSON
this module returns; results are evidence only when collected remotely, never from host Linux
Python. The checks mirror the core's own contracts: the zstandard version floor (>=0.25, never
downgraded to fit an old wheel), a checksummed round-trip through StrictZstdReader, and the
reader's rejection of a trailing second frame, truncation, a corrupted checksum and an
over-window frame. Every field is named evidence; a missing field is never success.
"""
import io
import json
import os
import platform
import sys

IDENTITY = "comic-archiver/android-selfcheck/1"


def _page_size():
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def _strict_reader(frame, maximum):
    from comic_archive.publication import StrictZstdReader
    return StrictZstdReader(io.BytesIO(frame), maximum)


def _collect(frame, maximum):
    """Drain the whole frame so the checksum and terminator are actually verified."""
    reader = _strict_reader(frame, maximum)
    data = bytearray()
    while True:
        chunk = reader.read(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _zstandard():
    try:
        import zstandard
    except ImportError:
        return {"imported": False}

    def rejected(frame, maximum):
        try:
            _collect(frame, maximum)
            return False
        except ValueError:
            return True

    payload = b"comic-archiver android selfcheck " * 64
    frame = zstandard.ZstdCompressor(level=1, write_checksum=True).compress(payload)
    corrupt = frame[:-4] + bytes([frame[-4] ^ 0xFF]) + frame[-3:]
    checks = {
        "imported": True,
        "version": zstandard.__version__,
        "payload_bytes": len(payload),
        "frame_bytes": len(frame),
        "roundtrip": _collect(frame, len(payload)) == payload,
        "trailing_frame": rejected(frame + frame, len(payload) + 1024),
        "truncation": rejected(frame[: max(1, len(frame) // 2)], len(payload) + 1024),
    }
    try:
        _collect(corrupt, len(payload) + 1024)
        checks["checksum"] = "accepted"
    except (ValueError, zstandard.ZstdError):
        checks["checksum"] = "rejected"
    try:
        params = zstandard.ZstdCompressionParameters(compression_level=1, window_log=24)
        buffer = io.BytesIO()
        with zstandard.ZstdCompressor(compression_params=params).stream_writer(buffer, closefd=False) as writer:
            writer.write(b"comic-archiver window-bound frame")
        checks["window_bound"] = rejected(buffer.getvalue(), 1 << 24)
    except (AttributeError, TypeError, ValueError):
        checks["window_bound"] = "unavailable"
    return checks


def run():
    """One JSON line of observed facts about this process; it contains no credentials."""
    return json.dumps({"identity": IDENTITY, "python": sys.version.split()[0], "machine": platform.machine(),
                       "implementation": sys.implementation.name, "page_size": _page_size(),
                       "zstandard": _zstandard()})
