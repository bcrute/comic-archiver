"""Durable reading-list discovery, watched-series refresh, extraction and audit.

Only normal importer jobs contact the source. An empty queue is never enough
to finish: discovery runs first and the saved files are checked afterwards.
"""
from pathlib import Path
import re
import secrets

from . import batches, completion, sites
from .archive import atomic_json
from .errors import LibraryBusy
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError
from .queue_client import submit_job, task_status

CATEGORIES = sites.DEFAULT.lists
PHASES = {'existing_backlog', 'lists', 'series', 'archive', 'verify', 'finished'}
STATES = {'running', 'held', 'stopped', 'finished'}


def lists(session):
    """The account lists Go reads for the site this sign-in belongs to."""
    return sites.of_session(Path(session)).lists


def _load(root, categories=CATEGORIES):
    value = batches._read(root / 'account-export.json')
    if value is None:
        return None
    try:
        if (type(value['schema_version']) is not int or value['schema_version'] != 1 or value['status'] not in STATES
                or value['phase'] not in PHASES or type(value['category_index']) is not int
                or not 0 <= value['category_index'] <= len(categories)
                or not isinstance(value['list_checks'], list)
                or len(value['list_checks']) > len(categories)):
            raise ValueError
        if value['audit'] is not None and not isinstance(value['audit'], dict):
            raise ValueError
        for check in value['list_checks']:
            if (not isinstance(check, dict) or set(check) != {'category', 'complete', 'boundary_observed', 'observed_series_count'}
                    or check['category'] not in categories or type(check['complete']) is not bool
                    or type(check['boundary_observed']) is not bool
                    or type(check['observed_series_count']) is not int or check['observed_series_count'] < 0):
                raise ValueError
        batches._instant(value['started_at'])
        value['window_budgets'] = batches.budgets(value['window_budgets'])
        if not value['window_budgets']['archive_only'] or value['window_budgets']['refresh_lists']:
            raise ValueError
        pending = value['pending']
        previous = value.get('last_failed_task_id')
        if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r'job-[a-f0-9]{24}', previous)):
            raise ValueError
        if pending is not None:
            if (not re.fullmatch(r'job-[a-f0-9]{24}', pending['task_id'])
                    or pending['operation'] not in {'sync', 'audit_account'}
                    or not isinstance(pending['options'], dict)
                    or type(pending['before_remaining']) is not int or pending['before_remaining'] < 0):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ArchiveError('Account export checkpoint is invalid; existing tasks are retained.') from None
    return value


def _save(root, value):
    atomic_json(root / 'account-export.json', value)
    (root / 'account-export.json').chmod(0o600)


def _public(value):
    if value is None:
        return {'configured': False, 'account_inventory_complete': False}
    return {k: value[k] for k in ('status', 'phase', 'code', 'started_at', 'list_checks', 'audit')} | {
        'configured': True, 'account_inventory_complete': False,
        'scope': 'provided_reading_lists_and_watched_series',
        'pending_task_id': value['pending']['task_id'] if value['pending'] else None,
        'last_failed_task_id': value.get('last_failed_task_id'),
        'coverage_blockers': sorted({'independent_purchase_inventory_unverified',
             *(value['audit'] or {}).get('coverage_blockers', []),
             *(['provided_list_boundary_unverified'] if any(not c['complete'] for c in value['list_checks']) else [])})}


def status(session, library=Path('Library')):
    return _public(_load(batches._root(session, library, require_queue=False), lists(session)))


def _task_succeeded(pending, response):
    result = response.get('result')
    return (response['status'] == 'ok' and isinstance(result, dict)
            and (result.get('status') is None if pending['operation'] == 'audit_account'
                 else result.get('status') in ('finished', 'limited')))


def configure(session, library=Path('Library'), *, resume=False, window_budgets=None):
    root = batches._root(session, library)
    with LibraryLock(root / '.account-export-state'):
        old = _load(root, lists(session))
        if resume:
            if old is None or old['status'] == 'finished':
                raise ArchiveError('No unfinished account export exists to resume.')
            if completion.status(session, library).get('status') in {'paused', 'stopped'}:
                raise ArchiveError('Resolve and explicitly resume the held backlog before resuming account discovery.')
            pending = old['pending']
            if pending:
                request = root / 'requests' / (pending['task_id'] + '.json')
                receipt = root / 'responses' / (pending['task_id'] + '.json')
                if request.exists() or request.is_symlink() or receipt.exists() or receipt.is_symlink():
                    response = task_status(session, pending['task_id'])
                    if response['status'] in {'ok', 'error'} and not _task_succeeded(pending, response):
                        # Explicit operator resume authorizes one new task. Keep
                        # the failed receipt immutable and reserve before publication.
                        old['last_failed_task_id'] = pending['task_id']
                        _reserve(root, old, pending['operation'], pending['options'], pending['before_remaining'])
            if old['phase'] == 'verify':
                old['audit'] = None
            old.update(status='running', code=None)
            _save(root, old)
            return _public(old)
        if old and old['status'] != 'finished':
            raise ArchiveError('An unfinished account export exists; resume it instead.')
        plan = completion.status(session, library)
        if plan.get('status') in {'paused', 'stopped'}:
            raise ArchiveError('Resolve the held backlog before starting account discovery.')
        config = batches.budgets(completion.WINDOW_DEFAULTS | dict(window_budgets or {}))
        value = {'schema_version': 1, 'status': 'running', 'code': None,
                 'phase': 'existing_backlog' if plan.get('status') == 'running' else 'lists',
                 'started_at': batches._now().isoformat(), 'category_index': 0,
                 'list_checks': [], 'pending': None, 'last_failed_task_id': None,
                 'audit': None, 'window_budgets': config}
        _save(root, value)
        return _public(value)


def request_stop(session, library=Path('Library')):
    root = batches._root(session, library)
    with LibraryLock(root / '.account-export-state'):
        value = _load(root, lists(session))
        if value is None:
            raise ArchiveError('No account export is configured.')
        if value['status'] == 'finished':
            return _public(value)
        value.update(status='stopped', code=None)
        _save(root, value)
        if completion.status(session, library).get('status') in {'running', 'paused'}:
            completion.request_stop(session, library)
        return _public(value)


def _inventory(library):
    with Inventory(library, read_only=True) as inventory:
        result = inventory.status()
        result['pending_episode_count'] = len(inventory.pending())
        return result


def _reserve(root, value, operation, options, before=0):
    value['pending'] = {'task_id': 'job-' + secrets.token_hex(12),
                        'operation': operation, 'options': options, 'before_remaining': before}
    _save(root, value)  # Persist identity BEFORE queue publication.


def _advance(root, session, library, value):
    pending = value['pending']
    if pending:
        submit_job(session, pending['operation'], pending['options'], _task_id=pending['task_id'])
        response = task_status(session, pending['task_id'])
        if response['status'] in {'running', 'queued'}:
            return
        result = response.get('result', {})
        if not isinstance(result, dict):
            value.update(status='held', code='task_result_invalid')
            return
        if not _task_succeeded(pending, response):
            code = result.get('pause_reason') or response.get('error_code')
            if not isinstance(code, str) or code not in (batches.CODES | {'job_failed'}):
                code = 'task_failed'
            value.update(status='held', code=code)
            return  # Keep the exact receipt; never automatically replay a failed source task.
        value['pending'] = None
        if value['phase'] == 'lists':
            refresh = result.get('refresh')
            checks = refresh.get('reading_lists') if isinstance(refresh, dict) else None
            categories = lists(session)
            category = categories[value['category_index']]
            matches = [c for c in checks if isinstance(c, dict) and c.get('category') == category] if isinstance(checks, list) else []
            check = matches[0] if len(matches) == 1 else None
            if (check is None or set(check) != {'category', 'complete', 'boundary_observed', 'observed_series_count'}
                    or type(check['complete']) is not bool or type(check['boundary_observed']) is not bool
                    or type(check['observed_series_count']) is not int or check['observed_series_count'] < 0):
                value.update(status='held', code='list_checkpoint_incomplete')
                return
            value['list_checks'].append(check)
            value['category_index'] += 1
            if value['category_index'] == len(categories):
                value['phase'] = 'series'
        elif value['phase'] == 'series':
            remaining = _inventory(library)['scan_remaining_series_count']
            if remaining == 0:
                value['phase'] = 'archive'
            elif remaining >= pending['before_remaining']:
                value.update(status='held', code='scan_no_progress')
        elif value['phase'] == 'verify':
            value['audit'] = result
            if (result.get('archive_integrity_passed') is True
                    and all(type(result.get(k)) is int and result[k] == 0 for k in (
                        'missing_recorded_accessible_episode_count',
                        'inventory_index_problem_count', 'unindexed_archive_count'))):
                value.update(status='finished', phase='finished', code=None)
            else:
                value.update(status='held', code='archive_incomplete')
        return
    if value['phase'] == 'existing_backlog':
        plan = completion.status(session, library)
        if plan.get('status') == 'known_backlog_drained':
            value['phase'] = 'lists'
        elif plan.get('status') != 'running':
            value.update(status='held', code=plan.get('code') or 'backlog_held')
    elif value['phase'] == 'lists':
        _reserve(root, value, 'sync', {'categories': [lists(session)[value['category_index']]],
                 'scan_only': True, 'scan_limit': 1, 'limit': 1, 'max_seconds': 7200})
    elif value['phase'] == 'series':
        plan = completion.status(session, library)
        if plan.get('status') == 'running':
            return
        if plan.get('status') in {'paused', 'stopped'}:
            value.update(status='held', code=plan.get('code') or 'backlog_held')
            return
        inventory = _inventory(library)
        # Preserve newly discovered grants before another metadata batch. The
        # series phase stays checkpointed while normal extraction drains them.
        if inventory['pending_episode_count']:
            completion.continue_discovered(session, library, window_budgets=value['window_budgets'])
            return
        remaining = inventory['scan_remaining_series_count']
        if not remaining:
            value['phase'] = 'archive'
        else:
            _reserve(root, value, 'sync', {'refresh_lists': False, 'scan_only': True,
                     'metadata_speed': value['window_budgets']['speed'],
                     'scan_limit': 10, 'limit': 1, 'max_seconds': 900}, remaining)
    elif value['phase'] == 'archive':
        plan = completion.status(session, library)
        if plan.get('status') == 'running':
            return
        if plan.get('status') in {'paused', 'stopped'}:
            value.update(status='held', code=plan.get('code') or 'backlog_held')
        elif _inventory(library)['pending_episode_count']:
            completion.continue_discovered(session, library, window_budgets=value['window_budgets'])
        else:
            value['phase'] = 'verify'
    elif value['phase'] == 'verify':
        _reserve(root, value, 'audit_account', {})


def worker_tick(session, library=Path('Library')):
    root = None
    try:
        root = batches._root(session, library)
        if _load(root, lists(session)) is None:
            return
        with LibraryLock(root / '.account-export-state'):
            value = _load(root, lists(session))
            if value['status'] == 'running':
                _advance(root, session, library, value)
                _save(root, value)
            return _public(value)
    except LibraryBusy:
        return
    except Exception:
        # Anything unexpected, a damaged inventory database among them, holds the export instead of the worker.
        if root is None:
            return
        try:
            with LibraryLock(root / '.account-export-state'):
                value = _load(root, lists(session))
                if value:
                    value.update(status='held', code='controller_failed')
                    _save(root, value)
        except (ArchiveError, OSError):
            pass
