"""Per-provider comic choices affect future acquisition, never saved files.

An absent setting preserves older applications' all-comics behaviour. The
default also applies to comics discovered later; exceptions name recorded IDs.
"""
import json

from .discovery import observed_id
from .models import ArchiveError

KEY = 'comic_selection'
MAX_EXCEPTIONS = 100_000


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'default', 'exceptions'}
            or type(value['default']) is not bool or not isinstance(value['exceptions'], list)
            or len(value['exceptions']) > MAX_EXCEPTIONS
            or any(not observed_id(key) for key in value['exceptions'])
            or len(set(value['exceptions'])) != len(value['exceptions'])):
        raise ArchiveError('The comic choices could not be read. Choose the comics again.')
    return {'default': value['default'], 'exceptions': sorted(value['exceptions'])}


def read(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return {'default': True, 'exceptions': []}
    try:
        if len(row[0]) > 8 * 1024 * 1024:
            raise ValueError
        return validate(json.loads(row[0]))
    except (ValueError, TypeError, RecursionError):
        raise ArchiveError('The comic choices could not be read. Choose the comics again.') from None


def includes(value, series_id):
    return value['default'] != (series_id in value['exceptions'])


def clause(db, alias='e'):
    if alias not in {'e', 'episodes', 's', 'series'}:
        raise ValueError('Unsupported comic inventory alias.')
    value = read(db)
    if not value['exceptions']:
        return '1' if value['default'] else '0'
    column = 'id' if alias in {'s', 'series'} else 'series_id'
    # Validation admits only bounded ASCII decimal identifiers, never SQL text.
    identifiers = ','.join("'" + key + "'" for key in value['exceptions'])
    return f"{alias}.{column} {'NOT IN' if value['default'] else 'IN'} ({identifiers})"


def change(db, chosen, series_id=None):
    """Change all comics, including future discoveries, or one recorded comic."""
    if type(chosen) is not bool or series_id is not None and not observed_id(series_id):
        raise ArchiveError('Choose a recorded comic and an explicit download choice.')
    if series_id is None:
        value = {'default': chosen, 'exceptions': []}
    else:
        if db.execute('SELECT 1 FROM series WHERE id=?', (series_id,)).fetchone() is None:
            raise ArchiveError('This comic is no longer recorded in the library.')
        value = read(db)
        exceptions = set(value['exceptions'])
        if chosen == value['default']:
            exceptions.discard(series_id)
        else:
            exceptions.add(series_id)
        value['exceptions'] = sorted(exceptions)
    value = validate(value)
    with db:
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (KEY, json.dumps(value, sort_keys=True)))
        # A newly included comic may have been skipped in this scan round.
        # Retain actual scan timestamps and queues; only invalidate completion.
        db.execute("DELETE FROM settings WHERE key='completion_retry'")
        from .inventory import timestamp
        db.execute("INSERT INTO settings VALUES ('scan_cycle_started',?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (timestamp(),))
    return value
