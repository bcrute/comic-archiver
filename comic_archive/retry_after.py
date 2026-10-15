"""Parse only a server's wait duration; never retain raw header values."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re

MAX_RETRY_AFTER = 2**31 - 1


def retry_after_seconds(value, now=None):
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    value = value.strip()
    if re.fullmatch(r'[0-9]+', value):
        # Saturation still exceeds every supported job/completion deadline.
        return min(MAX_RETRY_AFTER, int(value))
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        current = datetime.now(timezone.utc) if now is None else now
        return min(MAX_RETRY_AFTER, max(0, math.ceil((stamp-current).total_seconds())))
    except (TypeError, ValueError, OverflowError):
        return None


def retain_retry_after(error, value):
    delay = retry_after_seconds(value)
    if delay is not None:
        error.retry_after_seconds = delay
    return error
