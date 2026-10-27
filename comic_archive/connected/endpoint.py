"""Canonical reader origin DATA only; HTTPS or an explicit loopback test origin."""

from urllib.parse import urlsplit

from .files import SourceError
from .jsoncodec import require


def reader_origin(value):
    try:
        require(isinstance(value, str) and value.isascii() and len(value) <= 512)
        parsed = urlsplit(value)
        require(
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        require(
            parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path == ""
        )
        host = (
            "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
        )
        default = 443 if parsed.scheme == "https" else 80
        authority = host + (
            ":" + str(parsed.port)
            if parsed.port is not None and parsed.port != default
            else ""
        )
        require(value == parsed.scheme + "://" + authority)
        return value
    except (ValueError, TypeError):
        raise SourceError("integrity_failed") from None
