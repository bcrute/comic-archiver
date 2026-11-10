"""Persistent acquisition choices. These select work, never grant reader access."""
import json
import logging

from . import sites
from .models import ArchiveError


KEY = 'download_types'
TYPES = ('bought', 'free', 'limited', 'unclassified')
LABELS = {'bought': 'Paid / unlocked', 'free': 'Free', 'limited': 'Limited unlocks',
          'unclassified': 'Unknown type'}
HELP = {
    'bought': 'Includes free episodes from any series with a recorded permanent purchase or unlock.',
    'free': 'Includes free episodes from other series, even when none of their episodes were bought.',
    'limited': 'Already-unlocked rentals and Wait Until Free episodes. Nothing is claimed or bought.',
    'unclassified': 'Episodes whose free/paid type is unknown; these may include free episodes. Deselect for confirmed paid series only.',
}


def access_kind(mark, expires_at=None):
    if mark in sites.FREE:
        return 'free'
    if mark in sites.LIMITED or expires_at is not None:
        return 'limited'
    if mark in sites.UNLOCKED:
        return 'bought'
    return 'unclassified'


def validate(value):
    if (not isinstance(value, dict) or set(value) != set(TYPES)
            or any(type(value[key]) is not bool for key in TYPES) or not any(value.values())):
        raise ArchiveError('Choose which episode types to download.')
    return {key: value[key] for key in TYPES}


def read(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return dict.fromkeys(TYPES, True)
    try:
        return validate(json.loads(row[0]))
    except (ValueError, TypeError, RecursionError, ArchiveError):
        # Corrupt preferences must not silently re-enable unwanted downloads.
        raise ArchiveError('The saved download choices could not be read. Choose the episode types again.') from None


def save(db, value):
    value = validate(value)
    with db:
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (KEY, json.dumps(value, sort_keys=True)))
    logging.getLogger('comic_archive.download').info(
        'download choices saved: bought=%s free=%s limited=%s unclassified=%s free_paid_companions=true saved_files_changed=false',
        *(value[key] for key in TYPES))


def remember_paid_series(db, series_id=None):
    """Keep positive purchase evidence even after an episode becomes free."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='episode_expirations'").fetchone():
        return  # An old index cannot establish whether a recorded unlock is permanent.
    where = ' AND e.series_id=?' if series_id is not None else ''
    db.execute('INSERT OR IGNORE INTO download_paid_series SELECT DISTINCT e.series_id FROM episodes e '
               'WHERE e.access_state IN ' + sites.sql_list(sites.UNLOCKED)
               + ' AND NOT EXISTS (SELECT 1 FROM episode_expirations x WHERE x.episode_id=e.id)' + where,
               (series_id,) if series_id is not None else ())


def clause(db, alias='e'):
    """Fixed SQL over indexed metadata; no file reads and no provider requests."""
    if alias not in ('e', 'episodes'):
        raise ValueError('Unsupported inventory alias.')
    chosen = read(db)
    if all(chosen.values()):
        return '1'
    mark = f'{alias}.access_state'
    expiry = f'EXISTS (SELECT 1 FROM episode_expirations x WHERE x.episode_id={alias}.id)'
    free, bought, limited = (mark + ' IN ' + sites.sql_list(marks)
                            for marks in (sites.FREE, sites.UNLOCKED, sites.LIMITED))
    parts = []
    if chosen['free']:
        parts.append(free)
    if chosen['bought']:
        parts += [f'({bought} AND NOT {expiry})',
                  f'({free} AND EXISTS (SELECT 1 FROM download_paid_series p WHERE p.series_id={alias}.series_id))']
    if chosen['limited']:
        parts.append(f'({limited} OR (NOT ({free}) AND {expiry}))')
    if chosen['unclassified']:
        parts.append('(' + mark + ' NOT IN ' + sites.sql_list(sites.FREE | sites.UNLOCKED | sites.LIMITED)
                     + ' AND NOT ' + expiry + ')')
    return '(' + ' OR '.join(parts) + ')' if parts else '0'


def selected(db, key):
    return db.execute('SELECT 1 FROM episodes e WHERE e.id=? AND ' + clause(db), (key,)).fetchone() is not None
