"""Bounded strict JSON shared by CL-1 control and encrypted signaling."""

import json
import math

from .files import SourceError


def require(value, code="integrity_failed"):
    if not value:
        raise SourceError(code)


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def _shape(value, depth=0):
    require(depth <= 16)
    if isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            _shape(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _shape(child, depth + 1)
    elif isinstance(value, str):
        value.encode("utf-8")
    elif type(value) is int:
        require(abs(value) <= 2**53 - 1)
    elif type(value) is float:
        require(
            math.isfinite(value) and (not value.is_integer() or abs(value) <= 2**53 - 1)
        )


def loads(text, maximum):
    try:
        require(
            isinstance(text, str)
            and 0 < len(text) <= maximum
            and len(text.encode("utf-8")) <= maximum
        )
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        _shape(value)
        return value
    except (ValueError, UnicodeError, RecursionError, SourceError):
        raise SourceError("integrity_failed") from None


def dumps(value, maximum):
    try:
        _shape(value)
        text = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        require(len(text.encode("utf-8")) <= maximum, "resource_limit")
        return text
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SourceError("integrity_failed") from None
