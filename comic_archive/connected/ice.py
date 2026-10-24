"""Explicit bounded connection settings DATA, never account or read permission."""

import os
import re
import stat

from .files import SourceError
from .jsoncodec import dumps, loads, require

MAXIMUM = 8192
_URI = re.compile(
    r"(stun|turn|turns):([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?(?:\?transport=(udp|tcp))?"
)


def servers(value):
    require(isinstance(value, list) and len(value) <= 2)
    result, seen = [], set()
    for item in value:
        require(
            isinstance(item, dict)
            and set(item) <= {"urls", "username", "credential"}
            and "urls" in item
        )
        urls = [item["urls"]] if isinstance(item["urls"], str) else item["urls"]
        require(isinstance(urls, list) and len(urls) == 1)
        url = urls[0]
        require(isinstance(url, str) and len(url) <= 512)
        match = _URI.fullmatch(url)
        require(match is not None)
        scheme, host, port, transport = match.groups()
        require(
            len(host) <= 253
            and all(
                0 < len(label) <= 63
                and not label.startswith("-")
                and not label.endswith("-")
                for label in host.split(".")
            )
        )
        if all(c in "0123456789." for c in host):
            require(
                len(host.split(".")) == 4
                and all(
                    str(int(part)) == part and 0 <= int(part) <= 255
                    for part in host.split(".")
                )
            )
        require(port is None or 1 <= int(port) <= 65535)
        kind = "stun" if scheme == "stun" else "turn"
        require(kind not in seen)
        seen.add(kind)
        if kind == "stun":
            require(transport is None and set(item) == {"urls"})
            result.append({"urls": url})
        else:
            require(scheme != "turns" or transport in {None, "tcp"})
            require(set(item) == {"urls", "username", "credential"})
            for key in ("username", "credential"):
                text = item[key]
                require(
                    isinstance(text, str)
                    and 0 < len(text.encode("utf-8")) <= 512
                    and not any(ord(c) < 32 or ord(c) == 127 for c in text)
                )
            result.append(
                {
                    "urls": url,
                    "username": item["username"],
                    "credential": item["credential"],
                }
            )
    dumps(result, MAXIMUM)
    return result


def read_servers(path):
    """Read one owned private regular file; reject aliases, replacement and FIFOs."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.geteuid()
            and not before.st_mode & 0o077
            and 0 < before.st_size <= MAXIMUM
        )
        raw = bytearray()
        while len(raw) <= MAXIMUM:
            part = os.read(fd, MAXIMUM + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
        after, named = os.fstat(fd), os.stat(path, follow_symlinks=False)
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(
            stamp(before) == stamp(after) == stamp(named) and len(raw) == before.st_size
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise SourceError("integrity_failed") from None
        return servers(loads(text, MAXIMUM))
    finally:
        os.close(fd)
