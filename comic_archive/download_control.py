"""The account's download: one controller, its state in one small SQLite file beside the worker's queue.

Between two worker tasks it settles the receipt of the task it had out, then asks the library what is next:
episodes waiting are saved first, then each reading list is read, then the round's series are checked, then the
saved files are audited. It never contacts a site itself; the worker's bounded jobs do. Holds are only for what
the person must fix; anything that may clear by itself is a wait, and progress forgives the tries spent on it.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3

from . import osdep, sites
from .errors import LibraryBusy
from .inventory import ASSET_RETRY_COOLDOWN_SECONDS, Inventory
from .locking import LibraryLock, wait_for_library
from .models import ArchiveError
from .queue_client import submit_job, task_status
from .support_log import DOWNLOAD_LOG, describe

LOG = logging.getLogger(DOWNLOAD_LOG)
LOG.addHandler(logging.NullHandler())
STATE_FILE = 'download.sqlite3'
SCHEMA_VERSION = 1
STATUSES = ('running', 'waiting', 'held', 'stopped', 'finished')
STAGES = ('lists', 'series', 'archive', 'verify', 'finished')
# A hold is something the person must fix; each has its reason on its site card (dashboard_model.held_reason).
HOLDS = frozenset({'login_required', 'source_refused', 'source_access_changed', 'no_progress', 'task_failed',
                   'task_result_invalid', 'task_unavailable', 'controller_failed', 'archive_incomplete',
                   'b2_key_permissions', 'b2_bucket_mismatch', 'b2_private_bucket', 'cloud_login_required',
                   'cloud_space', 'cloud_cap', 'cloud_permission', 'cloud_verification', 'cloud_transfer_limit',
                   'cloud_unknown'})
# Cloud troubles that pass by themselves, waited out like the connection.
CLOUD_WAITS = frozenset({'cloud_connection', 'cloud_rate_limited', 'cloud_network_held'})
WAITS = ('connection', 'network_policy', 'source', 'images', 'idle')
# How long each kind of wait lasts, try by try; the last stays for every try after it.
CONNECTION_WAITS = (60, 120, 240, 480, 900)
POLICY_WAIT = 300
IDLE_WAITS = (300, 900, 3600)  # A step that did nothing; the next after the last holds as no_progress.
REFUSAL_WAITS = (300, 900, 3600)  # Refused with nothing to probe; the next after the last holds.
SOURCE_PROBES = 4  # The refused episode tried alone this many times before the download holds.
MIN_SOURCE_DELAY, MAX_SOURCE_DELAY = 30, 1800
FAILED_RETRIES = 1  # A failed, unreadable or vanished step is tried again this many times in a row.
FAILED_WAIT = 300
HISTORY_DAYS = 30
HISTORY_ROWS = 10000
# Each download step's own bounds; the pace (speed, workers, interval, batch bytes) comes from the chosen speed.
DOWNLOAD = {'limit': 100, 'max_pages': 2000, 'max_seconds': 1800}
LIST_READ = {'scan_only': True, 'scan_limit': 1, 'limit': 1, 'max_seconds': 7200}
SERIES_CHECK = {'refresh_lists': False, 'scan_only': True, 'scan_limit': 10, 'limit': 1, 'max_seconds': 900}
AUDIT_ZEROES = ('missing_recorded_accessible_episode_count', 'inventory_index_problem_count', 'unindexed_archive_count')
AUDIT_KEYS = ('verified_inventory_episode_count', *AUDIT_ZEROES)
TASK = re.compile(r'job-[a-f0-9]{24}')
# The three records 0.2 kept, moved into this one at the first open after the update (each under its lock).
OLD_FILES = ('account-export.json', 'completion.json', 'backlog.json')
OLD_LOCKS = ('.account-export-state', '.completion-state', '.backlog-state')
OLD_LIMIT = 1024 * 1024
EPISODE = re.compile(r'[1-9][0-9]{0,19}')

SCHEMA = f'''
CREATE TABLE state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT NOT NULL CHECK (status IN {STATUSES}),
  code TEXT,
  stage TEXT NOT NULL CHECK (stage IN {STAGES}),
  round_started_at TEXT NOT NULL,
  stretch_started_at TEXT NOT NULL,
  wait_until TEXT,
  go_after TEXT,
  idle_waits INTEGER NOT NULL DEFAULT 0 CHECK (idle_waits >= 0),
  network_waits INTEGER NOT NULL DEFAULT 0 CHECK (network_waits >= 0),
  source_probes INTEGER NOT NULL DEFAULT 0 CHECK (source_probes >= 0),
  source_delay INTEGER NOT NULL DEFAULT {MIN_SOURCE_DELAY} CHECK (source_delay BETWEEN {MIN_SOURCE_DELAY} AND {MAX_SOURCE_DELAY}),
  source_episode TEXT,
  failed_retries INTEGER NOT NULL DEFAULT 0 CHECK (failed_retries >= 0),
  episodes_waiting INTEGER NOT NULL DEFAULT 0 CHECK (episodes_waiting >= 0),
  audit TEXT,
  trimmed_at TEXT,
  updated_at TEXT NOT NULL,
  CHECK ((status = 'held') = (code IS NOT NULL AND code IN {tuple(sorted(HOLDS))}) OR status = 'waiting'),
  CHECK ((status = 'waiting') = (code IN {WAITS} AND wait_until IS NOT NULL) OR status = 'held'));
CREATE TABLE lists (
  category TEXT PRIMARY KEY,
  read_at TEXT,
  complete INTEGER CHECK (complete IN (0, 1)),
  observed_series_count INTEGER CHECK (observed_series_count >= 0));
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY CHECK (task_id GLOB 'job-*' AND length(task_id) = 28),
  kind TEXT NOT NULL CHECK (kind IN ('list', 'series', 'download', 'probe', 'audit')),
  operation TEXT NOT NULL CHECK (operation IN ('sync', 'archive_discovered', 'audit_account')),
  options TEXT NOT NULL,
  reserved_at TEXT NOT NULL,
  published_at TEXT,
  finished_at TEXT,
  outcome TEXT,
  pages INTEGER, bytes INTEGER, episodes INTEGER, series_checked INTEGER, progressed INTEGER);
CREATE UNIQUE INDEX one_outstanding ON tasks ((1)) WHERE finished_at IS NULL;
CREATE INDEX tasks_by_finish ON tasks (finished_at);
PRAGMA user_version = {SCHEMA_VERSION};
'''


class Damaged(ArchiveError):
    """The controller's own record cannot be read; the library's record is untouched."""

    def __init__(self):
        super().__init__('The download record is damaged; Continue starts the download again from the saved library.')


def _now():
    return datetime.now(timezone.utc)


def _stamp(value=None):
    return (value or _now()).isoformat()


def _instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('naive time')
    return result.astimezone(timezone.utc)


def lists(session):
    """The account lists Go reads for the site this sign-in belongs to."""
    return sites.of_session(Path(session)).lists


def _root(session, library, *, require_queue=True):
    from .state_paths import SESSION_NAME
    session = Path(session).absolute()
    library = Path(library).absolute()
    if (session.name != SESSION_NAME or library.name != 'Library' or session.parent != library.parent
            or any(p.is_symlink() for path in (session, library) for p in (path, *path.parents))):
        raise ArchiveError('Run downloads from the project containing Library and .comic-session.')
    root = session / 'worker'
    if any(p.is_symlink() for path in (root / 'requests', root / 'responses') for p in (path, *path.parents)):
        raise ArchiveError('Download queue folders must not be symlinked.')
    if require_queue and (not (root / 'requests').is_dir() or not (root / 'responses').is_dir()):
        raise ArchiveError('Start the downloader before downloading.')
    return root


def _connect(root, *, create=False):
    """The record, or None when there is none yet and none is to be made."""
    path = root / STATE_FILE
    if path.is_symlink() or root.is_symlink():
        raise ArchiveError('The download record must not be symlinked.')
    if not path.exists():
        if not create:
            return None
        root.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600))
        os.chmod(path, 0o600)
    try:
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
    except sqlite3.DatabaseError:
        raise Damaged() from None
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0 and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            db.executescript('BEGIN;' + SCHEMA + 'COMMIT;')
        elif version != SCHEMA_VERSION or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Damaged()
        return db
    except (Damaged, sqlite3.DatabaseError):
        db.close()
        raise Damaged() from None


def _state(db):
    row = db.execute('SELECT * FROM state WHERE id=1').fetchone()
    if row is None:
        return None
    value = dict(row)
    try:
        for key in ('round_started_at', 'stretch_started_at', 'updated_at'):
            _instant(value[key])
        for key in ('wait_until', 'go_after', 'trimmed_at'):
            if value[key] is not None:
                _instant(value[key])
        if value['source_episode'] is not None and not EPISODE.fullmatch(value['source_episode']):
            raise ValueError('episode')
        value['audit'] = None if value['audit'] is None else json.loads(value['audit'])
        if value['audit'] is not None and not isinstance(value['audit'], dict):
            raise ValueError('audit')
    except (TypeError, ValueError):
        raise Damaged() from None
    return value


def _said(status, code, stage):
    return f"{status}{'/' + code if code else ''} at {stage}"


def _save(db, value, why=None):
    """Write the download's state; a change of status, code or stage, or the person's own step (why), is logged."""
    old = db.execute('SELECT status, code, stage FROM state WHERE id=1').fetchone()
    if why or old is None or tuple(old) != (value['status'], value['code'], value['stage']):
        line = f"download: {_said(*old) if old else 'new'} -> {_said(value['status'], value['code'], value['stage'])}"
        if value['wait_until'] and value['status'] == 'waiting':
            line += f", until {value['wait_until']}"
        tries = [f'{key} {value[key]}' for key in ('idle_waits', 'network_waits', 'source_probes', 'failed_retries')
                 if value[key]]
        LOG.info(line + (f", tries: {', '.join(tries)}" if tries else '') + (f' ({why})' if why else ''))
    value = dict(value, updated_at=_stamp())
    value['audit'] = None if value['audit'] is None else json.dumps(value['audit'], sort_keys=True)
    db.execute('UPDATE state SET status=:status, code=:code, stage=:stage, round_started_at=:round_started_at, '
               'stretch_started_at=:stretch_started_at, wait_until=:wait_until, go_after=:go_after, '
               'idle_waits=:idle_waits, network_waits=:network_waits, source_probes=:source_probes, '
               'source_delay=:source_delay, source_episode=:source_episode, failed_retries=:failed_retries, '
               'episodes_waiting=:episodes_waiting, audit=:audit, trimmed_at=:trimmed_at, updated_at=:updated_at '
               'WHERE id=1', value)


def _outstanding(db):
    row = db.execute('SELECT * FROM tasks WHERE finished_at IS NULL').fetchone()
    if row is None:
        return None
    value = dict(row)
    try:
        if not TASK.fullmatch(value['task_id']):
            raise ValueError('task')
        value['options'] = json.loads(value['options'])
        if not isinstance(value['options'], dict):
            raise ValueError('options')
    except (TypeError, ValueError):
        raise Damaged() from None
    return value


def _inventory(library):
    """What the library has waiting: episodes to save and the round's series still to check."""
    if not (Path(library) / '.archive-state/inventory.sqlite3').is_file():
        return {'pending': 0, 'remaining': 0}  # Nothing saved or found yet: the first list read makes the record.
    with Inventory(library, read_only=True) as inventory:
        status = inventory.status()
        return {'pending': len(inventory.pending()), 'remaining': status['scan_remaining_series_count']}


def _pace(session):
    from .account_ui import SPEEDS, speed
    from types import SimpleNamespace
    pace = SPEEDS[speed(SimpleNamespace(session=Path(session)))]
    options = DOWNLOAD | {'max_bytes': pace['batch_bytes'], 'request_interval': pace['request_interval'],
                          'free_only': False}
    if pace['download_workers'] != 1:
        options['download_workers'] = pace['download_workers']
    if pace['speed'] == 'fast':
        options['speed'] = 'fast'
    return options, pace['speed']


def _reserve(db, kind, operation, options):
    """The task's identity is saved before it is published, so a restart publishes the same task, never a second."""
    db.execute('INSERT INTO tasks (task_id, kind, operation, options, reserved_at) VALUES (?,?,?,?,?)',
               ('job-' + secrets.token_hex(12), kind, operation, json.dumps(options, sort_keys=True), _stamp()))


def _wait(value, reason, seconds):
    value.update(status='waiting', code=reason, wait_until=_stamp(_now() + timedelta(seconds=max(1, seconds))))


def _hold(value, code):
    value.update(status='held', code=code, wait_until=None)


def _forgiven(value):
    value.update(idle_waits=0, network_waits=0, source_probes=0, failed_retries=0)


def _valid(result, task):
    """Whether a download or check receipt can be counted: whole numbers within what the task was allowed."""
    options = task['options']
    return (isinstance(result, dict)
            and all(type(result.get(key)) is int and result[key] >= 0
                    for key in ('downloaded_pages', 'downloaded_bytes', 'pending_episode_count'))
            and isinstance(result.get('archived_episode_ids'), list)
            and all(isinstance(key, str) and EPISODE.fullmatch(key) for key in result['archived_episode_ids'])
            and len(set(result['archived_episode_ids'])) == len(result['archived_episode_ids'])
            and len(result['archived_episode_ids']) <= options.get('limit', 1)
            and result['downloaded_pages'] <= options.get('max_pages', result['downloaded_pages'])
            and result['downloaded_bytes'] <= options.get('max_bytes', result['downloaded_bytes']))


def progressed(result):
    """Whether a receipt shows its task got something done: episodes saved or settled, or series checked.

    Counts of what is left cannot tell: they also rise when a round of checks starts over or a series appears."""
    refresh = result.get('refresh')
    checked = refresh.get('series_checks_completed') if isinstance(refresh, dict) else None
    return bool(result.get('archived_episode_ids') or result.get('downloaded_pages')
                or type(checked) is int and checked > 0
                or any(isinstance(result.get(key), list) and result[key]
                       for key in ('unavailable_episode_ids', 'unsupported_episode_ids', 'not_free_episode_ids')))


def _retry_after(result):
    from .retry_after import MAX_RETRY_AFTER
    delay = result.get('retry_after_seconds') if isinstance(result, dict) else None
    return delay if type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER else 0


def _failed(value, code):
    """A step that failed, could not be read or vanished: tried once more, then held for the person."""
    if value['failed_retries'] >= FAILED_RETRIES:
        _hold(value, code)
    else:
        value['failed_retries'] += 1
        if value['status'] == 'running':
            _wait(value, 'idle', FAILED_WAIT if code == 'task_failed' else 1)


def _connection(value, result):
    tries = value['network_waits']
    value['network_waits'] += 1
    if value['status'] == 'running':
        _wait(value, 'connection', max(CONNECTION_WAITS[min(tries, len(CONNECTION_WAITS) - 1)], _retry_after(result)))


def _refused(value, task, result):
    value['source_probes'] += 1
    episode = result.get('source_retry_episode_id') if isinstance(result, dict) else None
    later = _retry_after(result)
    if (isinstance(episode, str) and EPISODE.fullmatch(episode) and result.get('source_http_status') == 403
            and result.get('source_response_kind') == 'html'):
        if task['options'].get('retry_episode_id') == episode:
            value['source_delay'] = min(MAX_SOURCE_DELAY, value['source_delay'] * 2)
        value['source_episode'] = episode
        if value['source_probes'] > SOURCE_PROBES:
            _hold(value, 'source_refused')
        elif value['status'] == 'running':
            _wait(value, 'source', max(value['source_delay'], later))
    elif value['source_probes'] > len(REFUSAL_WAITS):
        _hold(value, 'source_refused')
    elif value['status'] == 'running':
        _wait(value, 'source', max(REFUSAL_WAITS[value['source_probes'] - 1], later))
    if value['status'] == 'held' and later:
        value['go_after'] = _stamp(_now() + timedelta(seconds=later))


def _settle(db, value, task, response, session):
    """Record what the task did and decide what that means for the download, in the caller's transaction."""
    result = response.get('result') if isinstance(response, dict) else None
    counts = {}
    if isinstance(result, dict):
        refresh = result.get('refresh') if isinstance(result.get('refresh'), dict) else {}
        counts = {'pages': result.get('downloaded_pages'), 'bytes': result.get('downloaded_bytes'),
                  'episodes': len(result['archived_episode_ids']) if isinstance(result.get('archived_episode_ids'), list) else None,
                  'series_checked': refresh.get('series_checks_completed')}
        counts = {key: n if type(n) is int and n >= 0 else None for key, n in counts.items()}
    # Only a receipt that can be counted shows progress; one that cannot never forgives a retry.
    done = isinstance(result, dict) and _valid(result, task) and progressed(result)
    outcome = response.get('status') if isinstance(response, dict) else 'invalid'
    db.execute('UPDATE tasks SET finished_at=?, outcome=?, pages=?, bytes=?, episodes=?, series_checked=?, progressed=? '
               'WHERE task_id=?', (_stamp(), outcome, counts.get('pages'), counts.get('bytes'), counts.get('episodes'),
                                   counts.get('series_checked'), int(done), task['task_id']))
    said = [outcome if isinstance(outcome, str) else 'invalid']
    if isinstance(response, dict) and isinstance(response.get('error_code'), str):
        said.append(response['error_code'])
    if isinstance(result, dict):
        said += [result[key] for key in ('status', 'pause_reason') if isinstance(result.get(key), str)]
    did = [f'{key} {n}' for key, n in counts.items() if n is not None] + (['progressed'] if done else [])
    LOG.info(f"download: step {task['task_id']} ({task['kind']}) ended {' '.join(said)}"
             + (f": {', '.join(did)}" if did else ''))
    if done:
        _forgiven(value)
    if not isinstance(response, dict) or response.get('status') not in ('ok', 'error'):
        _failed(value, 'task_result_invalid')
        return
    if response['status'] == 'error':
        code = response.get('error_code')
        if code == 'login_required':
            _hold(value, 'login_required')
        elif code == 'source_refused' and isinstance(result, dict) and result.get('pause_reason') == 'source_refused':
            _refused(value, task, result)
        elif code in CLOUD_WAITS:
            _connection(value, result)
        elif code in HOLDS and code.startswith(('b2_', 'cloud_')):
            _hold(value, code)
        else:
            _failed(value, 'task_failed')
        return
    if task['kind'] == 'audit':
        if not isinstance(result, dict) or 'status' in result:
            _failed(value, 'task_result_invalid')
            return
        value['audit'] = {key: result[key] for key in AUDIT_KEYS if type(result.get(key)) is int and result[key] >= 0}
        LOG.info('download: the check counted ' + ', '.join(f'{key} {n}' for key, n in sorted(value['audit'].items())))
        value['failed_retries'] = 0
        if result.get('archive_integrity_passed') is True and all(
                type(result.get(key)) is int and result[key] == 0 for key in AUDIT_ZEROES):
            value.update(stage='finished', status='finished', code=None, wait_until=None)
        else:
            _hold(value, 'archive_incomplete')
        return
    if not _valid(result, task) or result.get('status') not in ('finished', 'limited', 'cancelled', 'paused'):
        _failed(value, 'task_result_invalid')
        return
    value['failed_retries'] = 0
    if result['status'] == 'cancelled':
        # A step stopped by a Pause (or a start of the app) that Go has since overruled leaves the download going.
        overruled = _instant(task['published_at']) < _instant(value['stretch_started_at'])
        if value['status'] in ('running', 'waiting') and not overruled:
            value.update(status='stopped', code=None, wait_until=None)
        return
    if result['status'] == 'paused':
        reason = result.get('pause_reason')
        if reason == 'network_policy':
            if value['status'] == 'running':
                _wait(value, 'network_policy', POLICY_WAIT)
        elif reason == 'network_unavailable':
            _connection(value, result)
        else:
            _failed(value, 'task_failed')
        return
    if task['kind'] == 'probe':
        episode = task['options']['retry_episode_id']
        value['source_episode'] = None
        if not (result.get('reader_probe_episode_id') == episode and result.get('reader_probe_succeeded') is True):
            _hold(value, 'source_access_changed')
            return
    if task['kind'] == 'list':
        refresh = result.get('refresh')
        checks = refresh.get('reading_lists') if isinstance(refresh, dict) else None
        category = task['options']['categories'][0]
        found = [c for c in checks if isinstance(c, dict) and c.get('category') == category] if isinstance(checks, list) else []
        check = found[0] if len(found) == 1 else {}
        count = check.get('observed_series_count')
        # A list whose reading could not be confirmed is still read for this round; the next round reads it again.
        db.execute('UPDATE lists SET read_at=?, complete=?, observed_series_count=? WHERE category=?',
                   (_stamp(), int(check.get('complete') is True), count if type(count) is int and count >= 0 else 0,
                    category))
        return
    wait = result.get('asset_retry_after_seconds')
    if (result['status'] == 'limited' and result['pending_episode_count'] > 0 and type(wait) is int
            and 1 <= wait <= ASSET_RETRY_COOLDOWN_SECONDS):
        if value['status'] == 'running':
            _wait(value, 'images', wait)
        return
    if not done and task['kind'] in ('download', 'series', 'probe'):
        if value['idle_waits'] >= len(IDLE_WAITS):
            _hold(value, 'no_progress')
        else:
            value['idle_waits'] += 1
            if value['status'] == 'running':
                _wait(value, 'idle', IDLE_WAITS[value['idle_waits'] - 1])


def _next(db, value, session, library):
    """Reserve the next step, or move the round on; what the library has waiting decides which."""
    waiting = _inventory(library)
    value['episodes_waiting'] = waiting['pending']
    read = {row['category'] for row in db.execute('SELECT category FROM lists WHERE read_at IS NOT NULL')}
    unread = [c for c in lists(session) if c not in read]
    if value['stage'] == 'lists' and not unread:
        value['stage'] = 'series'
    if value['stage'] == 'series' and not waiting['remaining']:
        value['stage'] = 'archive'
    if value['stage'] == 'archive' and not waiting['pending']:
        value['stage'] = 'verify'
    options, speed = _pace(session)
    if value['source_episode'] is not None:
        _reserve(db, 'probe', 'archive_discovered', options | {'limit': 1, 'retry_episode_id': value['source_episode']})
    elif waiting['pending']:
        _reserve(db, 'download', 'archive_discovered', options)
    elif value['stage'] == 'lists':
        _reserve(db, 'list', 'sync', LIST_READ | {'categories': [unread[0]]})
    elif value['stage'] == 'series':
        _reserve(db, 'series', 'sync', SERIES_CHECK | {'metadata_speed': speed})
    else:
        _reserve(db, 'audit', 'audit_account', {})


def _trim(db, value):
    """History is kept for a month and never past a fixed number of steps, however long the download lives."""
    now = _now()
    if value['trimmed_at'] and now - _instant(value['trimmed_at']) < timedelta(days=1):
        return
    db.execute('DELETE FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?',
               (_stamp(now - timedelta(days=HISTORY_DAYS)),))
    db.execute('DELETE FROM tasks WHERE finished_at IS NOT NULL AND task_id NOT IN '
               '(SELECT task_id FROM tasks ORDER BY reserved_at DESC LIMIT ?)', (HISTORY_ROWS,))
    value['trimmed_at'] = _stamp(now)


def _publishable(root, library):
    """The worker is free, nothing else is queued, and no other writer has the library."""
    try:
        worker = json.loads(osdep.read_text(root / 'status.json'))
    except (OSError, ValueError):
        return False
    if not isinstance(worker, dict) or worker.get('state') != 'ready' or any((root / 'requests').glob('*.json')):
        return False
    try:
        with LibraryLock(library):
            return True
    except LibraryBusy:
        return False


def _step(db, root, session, library):
    """One pass; True when another pass may go further (a receipt settled or a step reserved)."""
    value = _state(db)
    if value is None:
        return False
    task = _outstanding(db)
    if task is not None:
        request = root / 'requests' / f"{task['task_id']}.json"
        receipt = root / 'responses' / f"{task['task_id']}.json"
        if task['published_at'] is None:
            if value['status'] != 'running':
                db.execute('DELETE FROM tasks WHERE task_id=?', (task['task_id'],))  # It never went out.
                return False
            if not request.exists() and not receipt.exists() and not _publishable(root, library):
                return False
            submit_job(session, task['operation'], task['options'], _task_id=task['task_id'])
            db.execute('UPDATE tasks SET published_at=? WHERE task_id=?', (_stamp(), task['task_id']))
            LOG.info(f"download: step {task['task_id']} ({task['kind']}) sent")
            return False
        if not request.exists() and not receipt.exists():
            if value['failed_retries'] >= FAILED_RETRIES or value['status'] != 'running':
                db.execute("UPDATE tasks SET finished_at=?, outcome='vanished' WHERE task_id=?",
                           (_stamp(), task['task_id']))
                LOG.info(f"download: step {task['task_id']} ({task['kind']}) vanished")
                _failed(value, 'task_unavailable')
            elif _publishable(root, library):
                value['failed_retries'] += 1
                submit_job(session, task['operation'], task['options'], _task_id=task['task_id'])
                LOG.info(f"download: step {task['task_id']} ({task['kind']}) vanished and was sent again")
            _save(db, value)
            return False
        try:
            response = task_status(session, task['task_id'])
        except ArchiveError:
            response = None  # A receipt that cannot be read is settled as one.
        if response is not None and response['status'] in ('queued', 'running'):
            return False
        _settle(db, value, task, response, session)
        _save(db, value)
        return True
    if value['status'] == 'waiting':
        if _now() < _instant(value['wait_until']):
            return False
        value.update(status='running', code=None, wait_until=None)
    if value['status'] != 'running':
        return False
    _trim(db, value)
    _next(db, value, session, library)
    _save(db, value)
    return True


def worker_tick(session, library=Path('Library')):
    """One look between worker tasks; a failure it did not expect holds the download, never the worker."""
    try:
        root = _root(session, library)
    except ArchiveError:
        return
    if not (root / STATE_FILE).exists():
        return
    try:
        with LibraryLock(root / '.download-state'):
            db = _connect(root)
            if db is None:
                return
            try:
                for _ in range(3):  # Settle, reserve, publish: each committed before the next.
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        more = _step(db, root, session, library)
                        db.execute('COMMIT')
                    except BaseException:
                        db.execute('ROLLBACK')
                        raise
                    if not more:
                        break
            finally:
                db.close()
    except LibraryBusy:
        return
    except Damaged:
        return  # The page shows the damage as a hold; Go sets the record aside.
    except Exception as error:
        LOG.error(f'download: the controller failed: {describe(error)}')
        _mark_failed(root)


def _mark_failed(root):
    try:
        with LibraryLock(root / '.download-state'):
            db = _connect(root)
            if db is None:
                return
            try:
                db.execute("UPDATE state SET status='held', code='controller_failed', wait_until=NULL WHERE id=1")
            finally:
                db.close()
    except (ArchiveError, OSError, sqlite3.Error):
        pass


def _set_aside(root):
    """A damaged record is kept beside the new one for a look later; only the newest three are kept."""
    stamp = _now().strftime('%Y%m%dT%H%M%SZ')
    for suffix in ('', '-wal', '-shm'):
        path = root / (STATE_FILE + suffix)
        if path.exists():
            path.replace(root / f'{STATE_FILE}.damaged-{stamp}{suffix}')
    kept = sorted(root.glob(f'{STATE_FILE}.damaged-*'), reverse=True)
    stamps = sorted({p.name.split('.damaged-', 1)[1][:16] for p in kept}, reverse=True)
    for path in kept:
        if path.name.split('.damaged-', 1)[1][:16] not in stamps[:3]:
            path.unlink(missing_ok=True)


def _old(path):
    """An old controller's record as far as it can be read; one that cannot be is moved aside unread."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > OLD_LIMIT:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _old_task(session, root, pending):
    """The step an old controller had sent out, as this one's task; None when it was only reserved."""
    if not isinstance(pending, dict):
        return None
    task_id, operation, options = pending.get('task_id'), pending.get('operation', 'sync'), pending.get('options')
    if (not isinstance(task_id, str) or not TASK.fullmatch(task_id) or not isinstance(options, dict)
            or operation not in ('sync', 'archive_discovered', 'audit_account')):
        return None
    if not any((root / folder / f'{task_id}.json').exists() for folder in ('requests', 'responses')):
        return None
    if operation == 'audit_account':
        kind = 'audit'
    elif operation == 'archive_discovered':
        episode = options.get('retry_episode_id')
        kind = 'probe' if isinstance(episode, str) and EPISODE.fullmatch(episode) else 'download'
    else:
        categories = options.get('categories')
        kind = 'list' if isinstance(categories, list) and len(categories) == 1 and categories[0] in lists(session) else 'series'
    return {'task_id': task_id, 'kind': kind, 'operation': operation, 'options': options}


def _move_old(session, library, root):
    """The first open after the update: the download the old controllers left becomes this one's record.

    Only its place is carried: whether it finished or needs a sign-in, which lists were read, the step that is
    out, and the reader's learned delay. Anything else was going stops, and Go continues it. The old files are
    kept in worker/legacy for a look later; going back to an older version starts that one's download afresh."""
    path = root / STATE_FILE
    if path.exists() or path.is_symlink() or not any((root / name).exists() or (root / name).is_symlink()
                                                     for name in OLD_FILES):
        return
    if (root / 'legacy').is_symlink():
        raise ArchiveError('The download record must not be symlinked.')
    with ExitStack() as locks:
        for name in ('.download-state', *OLD_LOCKS):
            locks.enter_context(LibraryLock(root / name))
        if path.exists() or path.is_symlink():
            return  # Another process moved it first.
        export, backlog = _old(root / 'account-export.json'), _old(root / 'backlog.json')
        now = _stamp()
        value = {'status': 'stopped', 'code': None, 'stage': 'lists', 'round_started_at': now,
                 'stretch_started_at': now, 'wait_until': None, 'go_after': None, 'idle_waits': 0, 'network_waits': 0,
                 'source_probes': 0, 'source_delay': MIN_SOURCE_DELAY, 'source_episode': None, 'failed_retries': 0,
                 'episodes_waiting': 0, 'audit': None, 'trimmed_at': None}
        try:
            if _instant(export['started_at']) <= _now():
                value['round_started_at'] = export['started_at']
        except (KeyError, TypeError, ValueError):
            pass
        categories = lists(session)
        checks = export.get('list_checks') if isinstance(export.get('list_checks'), list) else []
        checks = {c['category']: c for c in checks if isinstance(c, dict) and c.get('category') in categories}
        phase, read = export.get('phase'), []
        if export.get('status') == 'finished':
            value.update(status='finished', stage='finished')
            audit = export.get('audit') if isinstance(export.get('audit'), dict) else {}
            value['audit'] = {key: audit[key] for key in AUDIT_KEYS if type(audit.get(key)) is int and audit[key] >= 0}
            read = categories
        elif phase in ('series', 'archive', 'verify', 'finished'):
            value['stage'] = 'verify' if phase == 'finished' else phase
            read = categories
        elif phase == 'lists':
            read = [c for c in categories if c in checks and checks[c].get('complete') is True]
        if export.get('status') == 'held' and export.get('code') == 'login_required':
            value.update(status='held', code='login_required')
        delay, episode = backlog.get('source_recovery_delay'), backlog.get('source_recovery_episode')
        if type(delay) is int and MIN_SOURCE_DELAY <= delay <= MAX_SOURCE_DELAY:
            value['source_delay'] = delay
        if isinstance(episode, str) and EPISODE.fullmatch(episode):
            value['source_episode'] = episode
        try:
            value['episodes_waiting'] = _inventory(library)['pending']
        except (ArchiveError, OSError, sqlite3.Error):
            pass  # The first tick after Go counts them.
        rows = []
        for category in categories:
            check = checks.get(category, {})
            count = check.get('observed_series_count')
            rows.append((category, now, None if 'complete' not in check else int(check['complete'] is True),
                         count if type(count) is int and count >= 0 else None) if category in read
                        else (category, None, None, None))
        tasks = [task for task in (_old_task(session, root, export.get('pending')),
                                   _old_task(session, root, backlog.get('pending'))) if task]
        if len(tasks) == 2 and tasks[0]['task_id'] == tasks[1]['task_id']:
            tasks.pop()
        fresh = root / (STATE_FILE + '.moving')
        try:
            # A log left without its record would be read into the new one.
            for name in (fresh, fresh.with_name(fresh.name + '-journal'), path.with_name(STATE_FILE + '-wal'),
                         path.with_name(STATE_FILE + '-shm')):
                name.unlink(missing_ok=True)
            os.close(os.open(fresh, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600))
            db = sqlite3.connect(fresh, isolation_level=None)
            try:
                db.executescript('BEGIN;' + SCHEMA + 'COMMIT;')
                db.execute('BEGIN')
                db.execute("INSERT INTO state (id, status, stage, round_started_at, stretch_started_at, updated_at) "
                           "VALUES (1, 'stopped', 'lists', ?, ?, ?)", (now, now, now))
                _save(db, value, 'carried over from the earlier version')
                db.executemany('INSERT INTO lists (category, read_at, complete, observed_series_count) VALUES (?,?,?,?)',
                               rows)
                for task in tasks[:1]:
                    db.execute('INSERT INTO tasks (task_id, kind, operation, options, reserved_at, published_at) '
                               'VALUES (?,?,?,?,?,?)', (task['task_id'], task['kind'], task['operation'],
                                                        json.dumps(task['options'], sort_keys=True), now, now))
                db.execute('COMMIT')
            finally:
                db.close()
            for task in tasks[1:]:
                # Only one step can be out; the other, if still queued, is ended unrun as a start of the app ends it.
                _end_unrun(session, library, root, task['task_id'])
            os.replace(fresh, path)
            place = root / 'legacy' / _now().strftime('%Y%m%dT%H%M%SZ')
            place.mkdir(parents=True, exist_ok=True)
            for folder in (root / 'legacy', place):
                folder.chmod(0o700)
            for name in (*OLD_FILES, 'backlog-history'):
                if (root / name).exists() or (root / name).is_symlink():
                    (root / name).replace(place / name)
        except (OSError, sqlite3.Error):
            fresh.unlink(missing_ok=True)
            raise ArchiveError('Comic Archiver could not move the download to its new record. '
                               'Check that the library folder can be written to.') from None


def _end_unrun(session, library, root, task_id):
    from .task_cancellation import end_unstarted
    try:
        worker = json.loads(osdep.read_text(root / 'status.json'))
    except (OSError, ValueError):
        worker = {}
    if isinstance(worker, dict) and worker.get('state') == 'busy' and worker.get('task_id') == task_id:
        return  # It is running; it ends by itself and its receipt is not read.
    try:
        end_unstarted(session, library, task_id)
    except (ArchiveError, OSError):
        pass


def _transaction(session, library, action, *, create=False, require_queue=True):
    """Run one change under the record's lock. With `create` (Go), a damaged record is set aside and begun anew."""
    root = _root(session, library, require_queue=require_queue)
    with wait_for_library(root / '.download-state', 5):
        _move_old(session, library, root)
        for attempt in range(2):
            try:
                db = _connect(root, create=create)
                if db is None:
                    return action(None, None, root)
                try:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        result = action(db, _state(db), root)
                        db.execute('COMMIT')
                        return result
                    except BaseException:
                        db.execute('ROLLBACK')
                        raise
                finally:
                    db.close()
            except (Damaged, sqlite3.DatabaseError):
                if not create or attempt:
                    raise
                _set_aside(root)


def start(session, library=Path('Library')):
    """Go: continue from wherever the download stopped, start a new round after a finished one, or begin."""
    def go(db, value, root):
        _outstanding(db)  # A step that cannot be read damages the record as much as the rest: Go sets it aside.
        now = _stamp()
        if value is None:
            db.execute('INSERT INTO state (id, status, stage, round_started_at, stretch_started_at, updated_at) '
                       "VALUES (1, 'running', 'lists', ?, ?, ?)", (now, now, now))
            db.executemany('INSERT INTO lists (category) VALUES (?)', [(c,) for c in lists(session)])
            LOG.info('download: begun (Go)')
            return
        db.executemany('INSERT OR IGNORE INTO lists (category) VALUES (?)', [(c,) for c in lists(session)])
        if value['status'] in ('running', 'waiting'):
            LOG.info(f"download: Go while already {value['status']}")
            return
        if value['status'] == 'finished':
            value.update(stage='lists', round_started_at=now, audit=None)
            db.execute('UPDATE lists SET read_at=NULL, complete=NULL, observed_series_count=NULL')
        elif value['code'] == 'archive_incomplete':
            value['audit'] = None  # Audited again from where it stopped; the last counts no longer stand.
        value.update(status='running', code=None, wait_until=None, stretch_started_at=now, idle_waits=0,
                     network_waits=0, source_probes=0, failed_retries=0)
        if value['go_after'] and _now() < _instant(value['go_after']):
            # The site's own wait still runs; Go cannot cut it short, only start when it ends.
            value.update(status='waiting', code='source', wait_until=value['go_after'])
        value['go_after'] = None
        _save(db, value, 'Go')
    _transaction(session, library, go, create=True)


def request_stop(session, library=Path('Library')):
    """Pause: nothing more is asked for until Go. The task already out is cancelled by the caller."""
    def stop(db, value, root):
        if value is None or value['status'] not in ('running', 'waiting'):
            return
        task = _outstanding(db)
        if task is not None and task['published_at'] is None:
            db.execute('DELETE FROM tasks WHERE task_id=?', (task['task_id'],))
        value.update(status='stopped', code=None, wait_until=None)
        _save(db, value, 'Pause')
    try:
        _transaction(session, library, stop, require_queue=False)
    except (Damaged, sqlite3.DatabaseError):
        pass  # Nothing runs from a damaged record; the task already out is still cancelled by the caller.


def reopen(session, library=Path('Library'), *, idle=True):
    """A start of the app: a download that was going waits for Go. True when one was going.

    With no downloader running, a task left queued would be run by the next one unasked; it is ended unrun."""
    from .task_cancellation import end_unstarted

    def stopped(db, value, root):
        if value is None:
            return False
        going = value['status'] in ('running', 'waiting')
        if going:
            value.update(status='stopped', code=None, wait_until=None)
            _save(db, value, 'the app started')
        task = _outstanding(db)
        if idle and task is not None:
            if task['published_at'] is None:
                db.execute('DELETE FROM tasks WHERE task_id=?', (task['task_id'],))
            else:
                end_unstarted(session, library, task['task_id'])
        return going
    try:
        return _transaction(session, library, stopped, require_queue=False)
    except (Damaged, sqlite3.DatabaseError):
        return False


def outstanding(session, library=Path('Library')):
    """The id of the task the download has out (published or about to be), or None."""
    root = _root(session, library, require_queue=False)
    try:
        db = _connect(root)
        if db is None:
            return None
        try:
            task = _outstanding(db)
            return task['task_id'] if task else None
        finally:
            db.close()
    except (Damaged, sqlite3.DatabaseError):
        return None


def forget_empty_list_reads(session, library=Path('Library')):
    """Lists recorded as read but empty and unconfirmed are read again at the next Go (the phone's old reader)."""
    def forget(db, value, root):
        if value is None or value['stage'] != 'lists' or value['status'] not in ('held', 'stopped'):
            return
        task = _outstanding(db)
        if task is not None and task['published_at'] is not None:
            if task_status(session, task['task_id'])['status'] not in ('ok', 'error'):
                raise ArchiveError('The previous download is still stopping; try Go again shortly.')
        db.execute('UPDATE lists SET read_at=NULL, complete=NULL, observed_series_count=NULL '
                   'WHERE complete=0 AND observed_series_count=0')
    _transaction(session, library, forget, require_queue=False)


def status(session, library=Path('Library')):
    """The download as the apps see it (account_ui adds the speed and the clocks). Never raises for its record."""
    root = _root(session, library, require_queue=False)
    try:
        _move_old(session, library, root)
        db = _connect(root)
    except LibraryBusy:
        db = None  # Another process is moving or reading the old record; the next look shows it.
    except ArchiveError:
        return {'configured': True, 'status': 'held', 'phase': 'lists', 'code': 'controller_failed'}
    if db is None:
        return {'configured': False}
    try:
        value = _state(db)
        if value is None:
            return {'configured': False}
        rows = {row['category']: row for row in db.execute('SELECT * FROM lists')}
        task = _outstanding(db)
    except (Damaged, sqlite3.DatabaseError):
        return {'configured': True, 'status': 'held', 'phase': 'lists', 'code': 'controller_failed'}
    finally:
        db.close()
    now = _now()
    result = {'configured': True, 'status': 'running' if value['status'] == 'waiting' else value['status']}
    categories = lists(session)
    read = [c for c in categories if c in rows and rows[c]['read_at'] is not None]
    stage = value['stage']
    result['phase'] = ('existing_backlog' if stage == 'lists' and not read and value['episodes_waiting'] else stage)
    result['lists'] = {'checked': len(read), 'total': len(categories)}
    if stage == 'lists' and len(read) < len(categories):
        result['lists']['current'] = next(c for c in categories if c not in read)
    if value['status'] == 'held':
        result['code'] = value['code']
    if value['status'] == 'waiting':
        left = math.ceil((_instant(value['wait_until']) - now).total_seconds())
        reason = 'connection' if value['code'] in ('connection', 'network_policy') else value['code']
        if left > 0:  # A wait that is over is acted on at the next tick.
            result['retry'] = {'reason': reason, 'seconds': left}
    if value['go_after'] and value['status'] in ('held', 'stopped'):
        left = math.ceil((_instant(value['go_after']) - now).total_seconds())
        if left > 0:
            result['go_after_seconds'] = left
    result['started_at'] = _instant(value['stretch_started_at']).timestamp()
    if value['audit'] is not None:
        result['audit'] = value['audit']
    result['outstanding'] = task['task_id'] if task else None
    return result


def history(session, library=Path('Library'), limit=300):
    """(state, steps) for a log saved for support: the download as it stands and its last steps, oldest first.

    None when there is no download yet. Raises Damaged when its record cannot be read."""
    root = _root(session, library, require_queue=False)
    db = _connect(root)
    if db is None:
        return None
    try:
        value = _state(db)
        steps = [dict(row) for row in db.execute(
            'SELECT * FROM (SELECT task_id, kind, reserved_at, published_at, finished_at, outcome, pages, bytes, episodes, '
            'series_checked, progressed FROM tasks ORDER BY reserved_at DESC LIMIT ?) ORDER BY reserved_at', (limit,))]
    except sqlite3.DatabaseError:
        raise Damaged() from None
    finally:
        db.close()
    return None if value is None else (value, steps)


def source_recovery(session, library=Path('Library')):
    """The reader retry the download is waiting on, for the monitor; None when there is none."""
    root = _root(session, library, require_queue=False)
    try:
        db = _connect(root)
    except Damaged:
        return None
    if db is None:
        return None
    try:
        value = _state(db)
    except Damaged:
        return None
    finally:
        db.close()
    if not value or value['source_episode'] is None or value['code'] != 'source' or value['status'] != 'waiting':
        return None
    return {'episode_id': value['source_episode'], 'retry_at': value['wait_until'], 'attempts': value['source_probes'],
            'limit': SOURCE_PROBES, 'learned_delay_seconds': value['source_delay'], 'automatic': True}
