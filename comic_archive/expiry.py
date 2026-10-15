"""Explicit expiration evidence for a currently readable grant."""
from datetime import datetime, timezone
from .errors import AccessDenied, UnsupportedContent
from .models import ArchiveError


def now():
    return datetime.now(timezone.utc)


def expiration(value):
    if value is None: return None
    try:
        if not isinstance(value,str) or len(value)>100: raise ValueError
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None: raise ValueError
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError,OverflowError):
        error=UnsupportedContent('Temporary access has no verified expiration timestamp.')
        error.code='temporary_access'
        raise error from None


def check_expiration(value):
    if value is not None and datetime.fromisoformat(expiration(value))<=now():
        raise AccessDenied('Episode access has expired; no reader or image request will be started.')


def daily_candidate(access, expires_at):
    from .sites import FREE, UNLOCKED
    return access in FREE or (access in UNLOCKED and expires_at is not None
                                and datetime.fromisoformat(expiration(expires_at))>now())


def queue_priority(value):
    if value not in ('expiring_first','owned_first') or type(value) is not str:
        raise ArchiveError('Queue order must be expiring_first or owned_first.')
    return value
