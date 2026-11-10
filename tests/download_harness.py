"""Shared checks and fakes for the download tests: the status contract, an account of many series, and a worker.

The account answers the real jobs (`worker.execute`), so receipts and the library record are the jobs' own; a
fault armed on it makes the next site call fail the way the real reader, network or sign-in would.
"""
import json
from pathlib import Path
from unittest.mock import patch

from comic_archive import account_ui, sites
from comic_archive.errors import AuthenticationError, NetworkUnavailable
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.network import NetworkPolicyHold
from comic_archive.run_receipts import failure_result
from comic_archive.task_cancellation import cancelled_result, queued_stop_requested
from comic_archive.worker import controller_ticks, execute, private_json
from test_archive import png

KEYS = {'supported', 'configured', 'coverage_verified', 'speed', 'download_types', 'status', 'phase', 'code', 'lists',
        'elapsed_seconds', 'progress_age_seconds', 'between_steps_seconds', 'retry', 'go_after_seconds', 'audit', 'purchase_history', 'transfer'}
STATUSES = {'running', 'held', 'stopped', 'finished'}
PHASES = ['existing_backlog', 'lists', 'series', 'archive', 'verify', 'finished']
RETRY_REASONS = {'connection', 'source', 'images', 'idle'}
# What the download may hold for: each a reason the person can act on (see dashboard_model.held_reason).
HOLDS = {'login_required', 'source_refused', 'source_access_changed', 'no_progress', 'task_failed',
         'task_result_invalid', 'task_unavailable', 'controller_failed', 'archive_incomplete',
         'b2_key_permissions', 'b2_bucket_mismatch', 'b2_private_bucket', 'cloud_login_required', 'cloud_space',
         'cloud_cap', 'cloud_permission', 'cloud_verification', 'cloud_transfer_limit', 'cloud_unknown'}
# Files in the worker folder that are the queue's, not a controller's.
QUEUE = {'requests', 'responses', 'cancellations', 'status.json', 'drain.json', 'progress.json'}


def status_problems(value):
    """What is wrong with one account status against the apps' contract; empty when nothing is."""
    problems = []
    if not set(value) <= KEYS:
        problems.append(f'unknown keys {sorted(set(value) - KEYS)}')
    if value.get('supported') is not True or value.get('coverage_verified') is not False:
        problems.append('supported/coverage_verified changed')
    if value.get('speed') not in account_ui.SPEEDS:
        problems.append(f"speed {value.get('speed')!r}")
    from comic_archive.download_selection import validate
    try:
        if value.get('download_types') is not None:
            validate(value['download_types'])
        elif 'download_types' in value and (value.get('status'), value.get('code')) != ('held', 'controller_failed'):
            problems.append('unavailable choices without a held status')
    except ArchiveError:
        problems.append('invalid download types')
    if not value.get('configured'):
        if value.get('status') in ('running', 'held'):
            problems.append('running or held while not configured')
        return problems
    if value.get('status') not in STATUSES:
        problems.append(f"status {value.get('status')!r}")
    if value.get('phase') not in PHASES:
        problems.append(f"phase {value.get('phase')!r}")
    if value.get('status') == 'held' and not isinstance(value.get('code'), str):
        problems.append('a hold without a code')
    if value.get('status') != 'held' and value.get('code') is not None:
        problems.append(f"code {value.get('code')!r} without a hold")
    lists = value.get('lists')
    if lists is not None and not (0 <= lists.get('checked', -1) <= lists.get('total', -1)):
        problems.append(f'lists {lists}')
    retry = value.get('retry')
    if retry is not None and (value.get('status') != 'running' or retry.get('reason') not in RETRY_REASONS
                              or type(retry.get('seconds')) is not int):
        problems.append(f'retry {retry}')
    for key in ('elapsed_seconds', 'progress_age_seconds', 'between_steps_seconds', 'go_after_seconds'):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            problems.append(f'{key} {value[key]!r}')
    audit = value.get('audit')
    transfer = value.get('transfer')
    if transfer is not None and (set(transfer) != {'scope', 'downloaded_bytes'}
                                 or type(transfer['scope']) is not float or transfer['scope'] <= 0
                                 or type(transfer['downloaded_bytes']) is not int or transfer['downloaded_bytes'] < 0):
        problems.append(f'transfer {transfer}')
    if audit is not None and not all(type(n) is int and n >= 0 for n in audit.values()):
        problems.append(f'audit {audit}')
    return problems


def tick(session):
    for _, controller in controller_ticks():
        controller(session)


def state_files(session):
    """The controllers' own saved state: everything in the worker folder that is not the queue's."""
    folder = Path(session) / 'worker'
    return [p for p in folder.rglob('*') if p.is_file()
            and p.relative_to(folder).parts[0] not in QUEUE and not p.name.startswith('.')]


class Account:
    """A Tapas account of many series, seeded; `fault` makes the next matching site call fail."""

    def __init__(self, random, series=24):
        self.random = random
        self.identity = '11'
        self.fault = None  # (kind, until) — kind fails every call until the clock passes `until`
        self.now = lambda: 0
        self.calls = 0
        self.series = {}
        self.access = {}
        number = 1000
        for index in range(series):
            key = str(100 + index)
            episodes = []
            for _ in range(random.randint(1, 4)):
                number += 1
                self.access[str(number)] = random.choice(['tapas_free', 'tapas_free', 'tapas_unlocked',
                                                          'unavailable_or_unverified'])
                episodes.append(str(number))
            self.series[key] = episodes
        keys = sorted(self.series)
        self.lists = {category: (keys if index == 0 else random.sample(keys, random.randint(0, len(keys) // 3)))
                      for index, category in enumerate(sites.DEFAULT.lists)}

    def accessible(self):
        return {key for key, access in self.access.items() if access != 'unavailable_or_unverified'}

    def episode_url(self, key):
        return f'https://tapas.io/episode/{key}'

    def _call(self, episode=None):
        self.calls += 1
        if self.fault is None:
            return
        kind, until = self.fault
        if self.now() >= until:
            self.fault = None
            return
        if kind == 'network':
            raise NetworkUnavailable('PRIVATE connection reset')
        if kind == 'policy':
            raise NetworkPolicyHold('PRIVATE not on Wi-Fi')
        if kind == 'login':
            raise AuthenticationError('PRIVATE signed out')
        if kind == 'failed':
            raise RuntimeError('PRIVATE disk trouble')
        if kind == 'refused' or (kind == 'refused_reader' and episode is not None):
            error = ArchiveError('PRIVATE refused')
            error.source_http_status = 429 if kind == 'refused' else 403
            error.source_response_kind = 'other' if kind == 'refused' else 'html'
            if kind == 'refused':
                error.retry_after_seconds = 60
            else:
                error.source_retry_episode_id = episode
            raise error

    def account_identity(self):
        self._call()
        return self.identity

    def inspect_library(self, category):
        self._call()
        return {'authenticated': True, 'membership_verified': True, 'scope': category, 'complete': True,
                'boundary_observed': True,
                'series': [{'source_series_id': key, 'title': 'S' + key} for key in self.lists.get(category, [])]}

    def inspect_series(self, key, strict_order=False):
        self._call()
        return {'complete': True, 'authenticated': True, 'source_series_id': key, 'title': 'S' + key,
                'episodes': [{'id': e, 'title': 'E' + e, 'order': n, 'access_state': self.access[e]}
                             for n, e in enumerate(self.series[key], 1)]}

    def _series_of(self, key):
        return next(s for s, episodes in self.series.items() if key in episodes)

    def episode_access(self, url):
        key = url.rsplit('/', 1)[1]
        self._call(key)
        return {'source_episode_id': key, 'access_state': self.access[key]}

    def capture_automatic(self, url):
        key = url.rsplit('/', 1)[1]
        self._call(key)
        series = self._series_of(key)
        return Manifest(Series(series, 'S' + series, platform_id=series),
                        (Episode(key, series, 'E' + key, self.series[series].index(key) + 1,
                                 (Page(1, f'https://us-a.tapas.io/pc/{key}.png', False),), self.access[key], url),))

    def retrieve(self, page, max_bytes=None):
        self._call()
        return png(1)


class Worker:
    """The downloader: takes the waiting request, runs it through the real jobs, writes its receipt."""

    def __init__(self, session, account):
        self.queue = Path(session) / 'worker'
        self.account = account
        self.claimed = None
        self.ready()

    def ready(self):
        private_json(self.queue / 'status.json', {'state': 'ready', 'cookie': 'PRIVATE'})

    def waiting(self):
        """Requests the worker has still to run; one left beside its receipt is discarded, not run."""
        return [p for p in sorted((self.queue / 'requests').glob('*.json'))
                if not (self.queue / 'responses' / p.name).exists()]

    def claim(self):
        """The worker picks up the first request: busy with it, as `status.json` says while it runs."""
        import os
        for path in sorted((self.queue / 'requests').glob('*.json')):
            if (self.queue / 'responses' / path.name).exists():
                path.unlink()  # The worker discards a request that already has its receipt.
                continue
            self.claimed = path.stem
            private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': path.stem, 'pid': os.getpid()})
            return path.stem
        return None

    def complete(self, *, leave_request=False, vanish=False, garble=False):
        """Run the claimed request; the flags are the worker dying around its receipt."""
        task, self.claimed = self.claimed, None
        path = self.queue / 'requests' / f'{task}.json'
        if task is None or not path.exists():
            self.ready()
            return None
        if vanish:
            path.unlink()
            self.ready()
            return None
        request = json.loads(path.read_text())
        try:
            with patch('builtins.print'):
                if queued_stop_requested(self.queue.parent, task):
                    # Stopped before it began: the worker's own receipt, without running it.
                    result = cancelled_result(Path('Library'), request['operation'])
                else:
                    result = execute(self.account, request)
                response = {'task_id': task, 'status': 'ok', 'result': result}
        except ArchiveError as error:
            response = {'task_id': task, 'status': 'error', 'error': str(error),
                        'error_code': 'login_required' if isinstance(error, AuthenticationError) else 'job_failed'}
            result = failure_result(error)
            if result is not None:
                response['result'] = result
                if result.get('pause_reason') == 'source_refused':
                    response['error_code'] = 'source_refused'
        if garble:
            response = {'task_id': task, 'status': 'ok', 'result': ['PRIVATE']}
        private_json(self.queue / 'responses' / f'{task}.json', response)
        if not leave_request:
            path.unlink()
        self.ready()
        return response
