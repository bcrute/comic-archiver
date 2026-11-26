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


def _episode_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comic_episode_choices'").fetchone() is not None


def episode_choices(db):
    if not _episode_table(db):
        return {}
    return {row[0]: (row[1], bool(row[2])) for row in db.execute(
        'SELECT choice.episode_id,e.series_id,choice.chosen FROM comic_episode_choices choice '
        'JOIN episodes e ON e.id=choice.episode_id')}


def includes_episode(value, choices, series_id, episode_id):
    override = choices.get(episode_id)
    return includes(value, series_id) if override is None else override[1]


def clause(db, alias='e'):
    if alias not in {'e', 'episodes', 's', 'series'}:
        raise ValueError('Unsupported comic inventory alias.')
    value = read(db)
    if not value['exceptions']:
        # SQLite interprets a bare integer in GROUP BY as a column position.
        fallback = '1' if value['default'] else '(0=1)'
    else:
        column = 'id' if alias in {'s', 'series'} else 'series_id'
        # Validation admits only bounded ASCII decimal identifiers, never SQL text.
        identifiers = ','.join("'" + key + "'" for key in value['exceptions'])
        fallback = f"{alias}.{column} {'NOT IN' if value['default'] else 'IN'} ({identifiers})"
    if alias in {'e', 'episodes'} and _episode_table(db):
        # The aliases are fixed and the fallback comes from the validated policy above.
        return ('COALESCE((SELECT choice.chosen FROM comic_episode_choices choice '  # noqa: S608
                f'WHERE choice.episode_id={alias}.id), ({fallback}))')
    return fallback


def _invalidate_completion(db, added, series_id=None):
    """Forget the retry of the last check's gaps, and have what the choice added read.

    One added comic is marked unchecked by itself. The round of checks that is open keeps every other check it
    has made; with none open, the last one is opened again from before its earliest check, so that only this
    comic is left to read. Every comic added at once begins the round again. A comic taken out needs no read.
    """
    from .inventory import timestamp

    db.execute("DELETE FROM settings WHERE key='completion_retry'")
    if not added:
        return
    if series_id is None:
        db.execute("INSERT INTO settings VALUES ('scan_cycle_started',?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (timestamp(),))
        return
    db.execute('DELETE FROM series_scans WHERE series_id=?', (series_id,))
    earliest = db.execute('SELECT MIN(observed_at) FROM (SELECT observed_at FROM series_scans '
                          'UNION ALL SELECT observed_at FROM series_scan_gaps)').fetchone()[0]
    db.execute("INSERT OR IGNORE INTO settings VALUES ('scan_cycle_started',?)", (earliest or timestamp(),))


def change_episode(db, chosen, series_id, episode_id):
    if type(chosen) is not bool or not observed_id(series_id) or not observed_id(episode_id):
        raise ArchiveError('Choose a recorded episode and an explicit download choice.')
    if db.execute('SELECT 1 FROM episodes WHERE id=? AND series_id=?', (episode_id, series_id)).fetchone() is None:
        raise ArchiveError('This episode is no longer recorded in the library.')
    policy = read(db)
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS comic_episode_choices '
                   '(episode_id TEXT PRIMARY KEY, chosen INTEGER NOT NULL CHECK (chosen IN (0, 1)))')
        if chosen == includes(policy, series_id):
            db.execute('DELETE FROM comic_episode_choices WHERE episode_id=?', (episode_id,))
        else:
            db.execute('INSERT INTO comic_episode_choices VALUES (?,?) '
                       'ON CONFLICT(episode_id) DO UPDATE SET chosen=excluded.chosen', (episode_id, int(chosen)))
        # This episode is already indexed. Recheck completion without asking
        # the provider to enumerate its series again.
        db.execute("DELETE FROM settings WHERE key='completion_retry'")


def change(db, chosen, series_id=None):
    """Change all comics, including future discoveries, or one recorded comic."""
    if type(chosen) is not bool or series_id is not None and not observed_id(series_id):
        raise ArchiveError('Choose a recorded comic and an explicit download choice.')
    if series_id is None:
        value = {'default': chosen, 'exceptions': []}
        added = chosen
    else:
        if db.execute('SELECT 1 FROM series WHERE id=?', (series_id,)).fetchone() is None:
            raise ArchiveError('This comic is no longer recorded in the library.')
        value = read(db)
        added = chosen and not includes(value, series_id)
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
        if _episode_table(db):
            if series_id is None:
                db.execute('DELETE FROM comic_episode_choices')
            else:
                db.execute('DELETE FROM comic_episode_choices WHERE episode_id IN '
                           '(SELECT id FROM episodes WHERE series_id=?)', (series_id,))
        # A newly included comic may have been skipped in this scan round.
        # Retain every other comic's scan timestamp and the queues.
        _invalidate_completion(db, added, series_id)
    return value
