"""Reported expiration metadata and user-selected queue preferences."""
from datetime import datetime, timezone
from .errors import UnsupportedContent
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


def observed_expiration(value):
    """Keep usable provider metadata without turning it into a reader veto."""
    try:
        return expiration(value)
    except UnsupportedContent:
        import logging
        logging.getLogger('comic_archive.download').info('access metadata: ignored field=expiration reason=invalid_timestamp')
        return None


def daily_candidate(access, expires_at):
    from .sites import FREE, UNLOCKED, LIMITED
    return access in FREE | LIMITED or (access in UNLOCKED and observed_expiration(expires_at) is not None)


def queue_priority(value):
    if value not in ('expiring_first','owned_first') or type(value) is not str:
        raise ArchiveError('Queue order must be expiring_first or owned_first.')
    return value
