"""Bounded consecutive imports through an existing, private browser worker.

Only one reserved task is outstanding. Checkpoints survive controller restarts;
they never contain sessions or authorize source access. The normal importer
still checks the account and every selected episode in every batch.
"""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import secrets

from .archive import atomic_json
from .archive_paths import directory
from .errors import LibraryBusy
from .locking import LibraryLock
from .models import ArchiveError
from .queue_client import submit_job, task_status


DEFAULTS={'max_batches':4,'max_pages':8000,'max_bytes':4*1024**3,'max_seconds':7200,
          'batch_limit':100,'batch_pages':2000,'batch_bytes':1024**3,'batch_seconds':1800,
          'scan_limit':50,'request_interval':1.0,'free_only':False,'refresh_lists':True,'archive_only':False,
          'recover_network':False,'network_recovery_limit':2,'download_workers':1,'speed':'normal',
          'recover_source':True,'source_recovery_limit':4}
MAX_NETWORK_RECOVERIES=2
MAX_SOURCE_RECOVERIES=4
MIN_SOURCE_DELAY=30
MAX_SOURCE_DELAY=1800
# A window's tail shorter than this is not worth a batch: signing in alone can outlast it.
MIN_BATCH_SECONDS=60
STATUSES={'running','budget_reached','known_backlog_drained','paused','stopped','interrupted'}
CODES={None,'login_required','task_failed','task_unavailable','invalid_result','cancelled','no_progress','network_unavailable','network_policy','source_refused','source_access_changed'}


def budgets(values=None):
    if values is not None and (not isinstance(values,dict) or set(values)-set(DEFAULTS)):
        raise ArchiveError('Consecutive batches accept only fixed import budgets.')
    values=DEFAULTS|dict(values or {})
    from .downloads import validate_download_workers
    validate_download_workers(values['download_workers'])
    from .fast_import import validate_speed
    validate_speed(values['speed'])
    if values['speed'] == 'fast' and not values['archive_only']:
        raise ArchiveError('Fast consecutive imports use the known queue; select archive-only.')
    caps={'max_batches':48,'max_pages':100000,'max_bytes':64*1024**3,'max_seconds':86400,
          'batch_limit':200,'batch_pages':10000,'batch_bytes':8*1024**3,'batch_seconds':7200,'scan_limit':1000}
    if any(type(values[key]) is not int or not 1<=values[key]<=maximum for key,maximum in caps.items()):
        raise ArchiveError('Consecutive import budgets must be positive integers within local limits.')
    spacing=values['request_interval']
    from .downloads import validate_request_interval
    validate_request_interval(spacing)
    if any(type(values[key]) is not bool for key in ('free_only','refresh_lists','archive_only','recover_network','recover_source')):
        raise ArchiveError('Use explicit access/list options.')
    if values['archive_only'] and values['refresh_lists']:
        raise ArchiveError('Archive-only batches cannot also refresh reading lists.')
    if type(values['network_recovery_limit']) is not int or not 0<=values['network_recovery_limit']<=MAX_NETWORK_RECOVERIES:
        raise ArchiveError('Network recovery limits must be from 0 to 2.')
    if type(values['source_recovery_limit']) is not int or not 0<=values['source_recovery_limit']<=MAX_SOURCE_RECOVERIES:
        raise ArchiveError('Reader recovery limits must be from 0 to 4.')
    return values


def _now():
    return datetime.now(timezone.utc)


def _instant(value):
    try:
        if not isinstance(value,str) or len(value)>50:raise ValueError
        result=datetime.fromisoformat(value)
        if result.tzinfo is None:raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError,TypeError):
        raise ArchiveError('Consecutive import timestamps are invalid.') from None


def _root(session,library,*,require_queue=True):
    from .state_paths import SESSION_NAME
    session=Path(session).absolute();library=Path(library).absolute()
    if (session.name!=SESSION_NAME or library.name!='Library' or session.parent!=library.parent
            or any(p.is_symlink() for path in (session,library) for p in (path,*path.parents))):
        raise ArchiveError('Run consecutive imports from the project containing Library and .comic-session.')
    root=session/'worker'
    if any(p.is_symlink() for path in (root/'requests',root/'responses') for p in (path,*path.parents)):
        raise ArchiveError('Consecutive import queue directories must not be symlinked.')
    if require_queue and (not (root/'requests').is_dir() or not (root/'responses').is_dir()):
        raise ArchiveError('Start the downloader before running consecutive imports.')
    return root


def _read(path):
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ArchiveError('Consecutive import state must not be symlinked.')
    if not path.exists():return None
    try:
        if not path.is_file() or path.stat().st_size>16384:raise ValueError
        value=json.loads(path.read_text())
        if not isinstance(value,dict):raise ValueError
        return value
    except (OSError,ValueError,RecursionError):
        raise ArchiveError('Consecutive import state is invalid; pending work is retained.') from None


def _load(root):
    state=_read(root/'backlog.json')
    if state is None:return None
    try:
        if (type(state['schema_version']) is not int or state['schema_version']!=1
                or state['status'] not in STATUSES or state['code'] not in CODES
                or type(state['stop_requested']) is not bool):raise ValueError
        state['budgets']=budgets(state['budgets'])
        state.setdefault('network_recovery_attempts',0)
        state.setdefault('network_retry_after',None)
        state.setdefault('source_retry_after',None)
        state.setdefault('asset_retry_after',None)
        state.setdefault('source_recovery_attempts',0)
        state.setdefault('source_recovery_delay',MIN_SOURCE_DELAY)
        state.setdefault('source_recovery_wait',None)
        state.setdefault('source_recovery_episode',None)
        if (type(state['source_recovery_attempts']) is not int
                or not 0<=state['source_recovery_attempts']<=MAX_SOURCE_RECOVERIES
                or type(state['source_recovery_delay']) is not int
                or not MIN_SOURCE_DELAY<=state['source_recovery_delay']<=MAX_SOURCE_DELAY):raise ValueError
        retry_id=state['source_recovery_episode']
        if retry_id is not None and (not isinstance(retry_id,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',retry_id)):raise ValueError
        if state['source_recovery_wait'] is not None:
            if (not state['budgets']['recover_source'] or state['source_recovery_attempts']<1
                    or retry_id is None or state['status']!='paused' or state['code']!='source_refused'
                    or state['pending'] is not None
                    or not _instant(state['started_at'])<=_instant(state['source_recovery_wait'])<_instant(state['submit_before'])):raise ValueError
        if state['asset_retry_after'] is not None:
            if (_instant(state['asset_retry_after']) < _instant(state['started_at'])
                    or state['pending'] is not None):raise ValueError
        if state['source_retry_after'] is not None and _instant(state['source_retry_after']) < _instant(state['started_at']):raise ValueError
        if (type(state['network_recovery_attempts']) is not int
                or not 0<=state['network_recovery_attempts']<=MAX_NETWORK_RECOVERIES):raise ValueError
        if state['network_retry_after'] is not None:
            if (not state['budgets']['recover_network'] or state['network_recovery_attempts']<1
                    or state['status']!='paused' or state['code']!='network_unavailable'
                    or state['pending'] is not None
                    or not _instant(state['started_at'])<=_instant(state['network_retry_after'])<_instant(state['submit_before'])):
                raise ValueError
        for key in ('submitted_batches','completed_batches','archived_episodes','downloaded_pages','downloaded_bytes'):
            if type(state[key]) is not int or state[key]<0:raise ValueError
        legacy_failed_count=(state['submitted_batches']-state['completed_batches']
                             if state.get('pending') is None and state['status']=='paused'
                             and state['code'] in {'task_failed','login_required','network_unavailable','source_refused'} else 0)
        for key,used,batch,total in (('unreported_reserved_pages','downloaded_pages','batch_pages','max_pages'),
                                   ('unreported_reserved_bytes','downloaded_bytes','batch_bytes','max_bytes')):
            state.setdefault(key,min(max(0,legacy_failed_count)*state['budgets'][batch],
                                     max(0,state['budgets'][total]-state[used])))
            if type(state[key]) is not int or state[key]<0:raise ValueError
        if state['completed_batches']>state['submitted_batches'] or state['submitted_batches']>state['budgets']['max_batches']:raise ValueError
        duration=(_instant(state['submit_before'])-_instant(state['started_at'])).total_seconds()
        if not 0<duration<=state['budgets']['max_seconds']:raise ValueError
        for key in ('previous_pending_count','previous_scan_remaining'):
            if state[key] is not None and (type(state[key]) is not int or state[key]<0):raise ValueError
        pending=state['pending']
        if pending is not None:
            if (not isinstance(pending,dict) or not re.fullmatch(r'job-[a-f0-9]{24}',pending['task_id'])
                    or pending['phase'] not in {'reserved','queued'}):raise ValueError
            args=pending['options'];config=state['budgets']
            operation=pending.get('operation','sync')
            if operation not in {'sync','archive_discovered'}:raise ValueError
            if config['archive_only'] and operation!='archive_discovered':raise ValueError
            expected={'limit','max_pages','max_bytes','max_seconds','request_interval','free_only'}
            if 'retry_episode_id' in args:
                expected.add('retry_episode_id')
                if operation!='archive_discovered' or args['retry_episode_id']!=retry_id or retry_id is None or args['limit']!=1:raise ValueError
            if 'download_workers' in args:expected.add('download_workers')
            if 'speed' in args or config['speed'] == 'fast':expected.add('speed')
            if args.get('speed', 'normal') != config['speed']:raise ValueError
            if (type(args.get('download_workers',1)) is not int
                    or args.get('download_workers',1)!=config['download_workers']):raise ValueError
            if operation=='sync':expected|={'scan_limit','refresh_lists'}
            elif state['previous_scan_remaining']!=0 and not config['archive_only'] and 'retry_episode_id' not in args:raise ValueError
            if set(args)!=expected:raise ValueError
            for key,cap in (('limit','batch_limit'),('max_pages','batch_pages'),('max_bytes','batch_bytes')):
                if type(args[key]) is not int or not 1<=args[key]<=config[cap]:raise ValueError
            if operation=='sync' and (type(args['scan_limit']) is not int or not 1<=args['scan_limit']<=config['scan_limit']
                    or type(args['refresh_lists']) is not bool):raise ValueError
            seconds=args['max_seconds']
            if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=config['batch_seconds']:raise ValueError
            if (args['free_only'] is not config['free_only']
                    or type(args['request_interval']) not in (int,float)
                    or args['request_interval']!=config['request_interval']):raise ValueError
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ArchiveError('Consecutive import checkpoint has an unsupported schema; pending work is retained.') from None
    return state


def _save(root,state):
    state['updated_at']=_now().isoformat()
    atomic_json(root/'backlog.json',state);(root/'backlog.json').chmod(0o600)


def _public(state):
    if state is None:return {'configured':False,'account_inventory_complete':False}
    result={key:state[key] for key in ('status','code','started_at','submit_before','submitted_batches',
        'completed_batches','archived_episodes','downloaded_pages','downloaded_bytes','stop_requested')}
    result.update(unreported_reserved_pages=state.get('unreported_reserved_pages',0),
                  unreported_reserved_bytes=state.get('unreported_reserved_bytes',0))
    result.update(configured=True,account_inventory_complete=False,budgets=dict(state['budgets']),
                  pending_task_id=state['pending']['task_id'] if state['pending'] else None,
                  pending_operation=state['pending'].get('operation','sync') if state['pending'] else None,
                  submission_window_open=_now()<_instant(state['submit_before']),
                  network_recovery_attempts=state.get('network_recovery_attempts',0),
                  network_retry_after=state.get('network_retry_after'),source_retry_after=state.get('source_retry_after'),
                  asset_retry_after=state.get('asset_retry_after'))
    result.update(source_recovery_attempts=state.get('source_recovery_attempts',0),
                  source_recovery_delay=state.get('source_recovery_delay',MIN_SOURCE_DELAY),
                  source_recovery_wait=state.get('source_recovery_wait'),
                  source_recovery_episode=state.get('source_recovery_episode'))
    return result


def status(session,library=Path('Library')):
    return _public(_load(_root(session,library)))


def request_stop(session,library=Path('Library')):
    root=_root(session,library)
    with LibraryLock(root/'.backlog-state'):
        state=_load(root)
        if state is None:raise ArchiveError('No consecutive import is configured.')
        state['stop_requested']=True;_save(root,state)
    return _public(state)


def _start(root,values,resume,*,now=None):
    previous=_load(root)
    if resume:
        if previous is None:raise ArchiveError('No consecutive import checkpoint exists to resume.')
        if previous.get('source_retry_after') and _now() < _instant(previous['source_retry_after']):
            raise ArchiveError('The saved source cooldown must finish before resuming.')
        if previous['code']=='source_refused' and previous['source_recovery_episode'] is not None and previous['source_recovery_wait'] is None:
            # The automatic retries ran out. Pressing Go again is the person's own retry: the refused episode
            # is tried first, alone, and the retries start over with the longer waits already learned.
            previous['source_recovery_attempts']=0
        previous['source_retry_after'] = None
        if previous['code']=='invalid_result' and previous['pending'] is not None:
            # A receipt that cannot be counted holds once. Resuming counts its whole allowance as used and moves on.
            previous['unreported_reserved_pages']+=previous['pending']['options']['max_pages']
            previous['unreported_reserved_bytes']+=previous['pending']['options']['max_bytes']
            previous['pending']=None
        waiting=(previous['status']=='paused' and (previous['code']=='network_unavailable'
                 and previous['network_retry_after'] is not None or previous['code']=='source_refused'
                 and previous['source_recovery_wait'] is not None))
        previous.update(stop_requested=False)
        # Resuming is a person's own act: the window gets its automatic network recoveries back.
        if not waiting:previous.update(status='running',code=None,network_retry_after=None,network_recovery_attempts=0)
        _save(root,previous);return previous
    if previous and previous['pending']:
        raise ArchiveError('A previous batch is unresolved. Resume the download so it can finish first.')
    config=budgets(values)
    now=_now() if now is None else _instant(now.isoformat())
    if previous:
        history=directory(root/'backlog-history')
        history.chmod(0o700)
        snapshot=history/(now.strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(8)+'.json')
        atomic_json(snapshot,{'schema_version':1,'document_type':'backlog_window_history',
                              'recorded_at':now.isoformat(),**_public(previous)})
        snapshot.chmod(0o600)
    state={'schema_version':1,'status':'running','code':None,'budgets':config,
        'started_at':now.isoformat(),'submit_before':(now+timedelta(seconds=config['max_seconds'])).isoformat(),
        'submitted_batches':0,'completed_batches':0,'archived_episodes':0,'downloaded_pages':0,'downloaded_bytes':0,
        'pending':None,'stop_requested':False,'previous_pending_count':None,'previous_scan_remaining':None,
        'network_recovery_attempts':0,'network_retry_after':None,
        'source_recovery_attempts':0,'source_recovery_wait':None,'source_recovery_episode':None,
        'source_recovery_delay':previous['source_recovery_delay'] if previous else MIN_SOURCE_DELAY}
    _save(root,state);return state


def _recover_network(state):
    """Schedule a bounded cooldown; never issue a request or replace pending work."""
    if (state['status']!='paused' or state['code']!='network_unavailable'
            or not state['budgets']['recover_network'] or state['pending'] is not None):return False
    now=_now();config=state['budgets']
    if state['stop_requested']:
        state.update(status='stopped',network_retry_after=None);return True
    if (now>=_instant(state['submit_before']) or state['submitted_batches']>=config['max_batches']
            or state['downloaded_pages']+state.get('unreported_reserved_pages',0)>=config['max_pages']
            or state['downloaded_bytes']+state.get('unreported_reserved_bytes',0)>=config['max_bytes']):
        state.update(status='budget_reached',network_retry_after=None);return True
    waiting=state['network_retry_after']
    if waiting is not None:
        if now<_instant(waiting):return False
        state.update(status='running',code=None,network_retry_after=None);return True
    attempts=state['network_recovery_attempts']
    if attempts>=config['network_recovery_limit']:return False
    after=now+timedelta(seconds=60*2**attempts)
    if state.get('source_retry_after'):
        after=max(after,_instant(state['source_retry_after']))
    if after>=_instant(state['submit_before']):return False
    state.update(network_recovery_attempts=attempts+1,network_retry_after=after.isoformat())
    return True


def _recover_source(state):
    """Probe only a freshly authorized reader; retain learned waits and caps."""
    if (state['status']!='paused' or state['code']!='source_refused'
            or not state['budgets']['recover_source'] or state['pending'] is not None
            or state['source_recovery_episode'] is None):return False
    now=_now();config=state['budgets']
    if state['stop_requested']:
        if state['source_recovery_wait'] is not None:
            existing=state['source_retry_after']
            state['source_retry_after']=max(state['source_recovery_wait'],existing) if existing else state['source_recovery_wait']
        state.update(status='stopped',code=None,source_recovery_wait=None,source_recovery_episode=None);return True
    if (now>=_instant(state['submit_before']) or state['submitted_batches']>=config['max_batches']
            or state['downloaded_pages']+state['unreported_reserved_pages']>=config['max_pages']
            or state['downloaded_bytes']+state['unreported_reserved_bytes']>=config['max_bytes']):
        state.update(status='budget_reached',source_recovery_wait=None);return True
    wait=state['source_recovery_wait']
    if wait is not None:
        if now<_instant(wait):return False
        state.update(status='running',code=None,source_recovery_wait=None,source_retry_after=None);return True
    if state['source_recovery_attempts']>=config['source_recovery_limit']:return False
    after=now+timedelta(seconds=state['source_recovery_delay'])
    if state['source_retry_after'] is not None:after=max(after,_instant(state['source_retry_after']))
    if after>=_instant(state['submit_before']):return False
    state['source_recovery_delay']=min(MAX_SOURCE_DELAY,max(state['source_recovery_delay'],math.ceil((after-now).total_seconds())))
    state['source_recovery_attempts']+=1
    state['source_recovery_wait']=after.isoformat()
    return True


def _valid_accounting(result, pending, remaining, *, failed=False):
    if not isinstance(result, dict):
        return False
    statuses = {'login_required', 'paused', 'interrupted'} if failed else {'finished', 'limited', 'cancelled', 'paused'}
    return (result.get('status') in statuses
            and (failed or result.get('status') != 'paused' or result.get('pause_reason') in {'network_unavailable', 'network_policy'})
            and (not failed or (isinstance(result.get('run_id'), str)
                                and re.fullmatch(r'[a-f0-9]{24}', result['run_id']) is not None))
            and all(type(result.get(key)) is int and result[key] >= 0
                    for key in ('downloaded_pages', 'downloaded_bytes', 'pending_episode_count'))
            and isinstance(result.get('archived_episode_ids'), list)
            and all(isinstance(key, str) and re.fullmatch(r'[1-9][0-9]{0,19}', key)
                    for key in result['archived_episode_ids'])
            and type(remaining) is int and remaining >= 0
            and result['downloaded_pages'] <= pending['options']['max_pages']
            and result['downloaded_bytes'] <= pending['options']['max_bytes']
            and len(result['archived_episode_ids']) <= pending['options']['limit']
            and len(set(result['archived_episode_ids'])) == len(result['archived_episode_ids']))


def _charge(state, result):
    state['completed_batches'] += 1
    state['archived_episodes'] += len(result['archived_episode_ids'])
    state['downloaded_pages'] += result['downloaded_pages']
    state['downloaded_bytes'] += result['downloaded_bytes']


def step(session,library=Path('Library'),*,observe_only=False):
    """One bounded queue observation/publication; never issue source requests."""
    root=_root(session,library)
    with LibraryLock(root/'.backlog-state'):
        state=_load(root)
        if state is None:raise ArchiveError('Start consecutive imports before advancing them.')
        if not observe_only and (_recover_source(state) or _recover_network(state)):_save(root,state)
        if state['status']!='running' and not (observe_only and state['pending']):return _public(state)
        pending=state['pending']
        if pending:
            stopping=observe_only or state['stop_requested'] or _now()>=_instant(state['submit_before'])
            if pending['phase']=='reserved' and stopping:
                # An unpublished reservation may be discarded. An already
                # queued/running task stays recorded until its result exists.
                path=root/'requests'/(pending['task_id']+'.json')
                response=root/'responses'/(pending['task_id']+'.json')
                if not path.exists() and not response.exists():
                    state['pending']=None;state['status']='stopped' if state['stop_requested'] else 'budget_reached'
                    _save(root,state);return _public(state)
                pending['phase']='queued';_save(root,state)
            if pending['phase']=='reserved':
                submit_job(session,pending.get('operation','sync'),pending['options'],_task_id=pending['task_id'])
                pending['phase']='queued';_save(root,state)
            try:value=task_status(session,pending['task_id'])
            except ArchiveError:
                state.update(status='paused',code='task_unavailable');_save(root,state);return _public(state)
            if value['status'] in {'running','queued'}:
                # No timeout is treated as completion, and a different task is
                # never substituted for an unresolved reserved identity.
                if stopping:
                    state['status']='stopped' if state['stop_requested'] else 'budget_reached';_save(root,state)
                return _public(state)
            result=value.get('result')
            if isinstance(result,dict):
                from .retry_after import MAX_RETRY_AFTER
                delay=result.get('retry_after_seconds')
                if (result.get('status') == 'paused' and type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER):
                    state['source_retry_after']=(_now()+timedelta(seconds=delay)).isoformat()
            observation=None;remaining_key='remaining_series_count'
            if isinstance(result,dict):
                if pending.get('operation','sync')=='sync' and isinstance(result.get('refresh'),dict):
                    observation=result['refresh']
                elif (pending.get('operation','sync')=='archive_discovered' or value['status']=='error'
                      or result.get('status') in {'cancelled','paused','limited'}):
                    # A limit that cut the refresh short leaves the same scan count in the inventory summary.
                    observation=result.get('inventory');remaining_key='scan_remaining_series_count'
            remaining=observation.get(remaining_key) if isinstance(observation,dict) else None
            if value['status']=='error':
                state['source_recovery_episode']=None
                code='login_required' if value.get('error_code')=='login_required' else 'task_failed'
                if (value.get('error_code') == 'source_refused' and isinstance(result,dict)
                        and result.get('pause_reason') == 'source_refused'
                        and type(result.get('source_http_status')) is int and result['source_http_status'] in {403,429}):
                    code='source_refused'
                if (_valid_accounting(result, pending, remaining, failed=True)
                        and (result['status']=='login_required') == (code=='login_required')):
                    _charge(state, result)
                    state['previous_pending_count'] = result['pending_episode_count']
                    state['previous_scan_remaining'] = remaining
                    retry_id=result.get('source_retry_episode_id')
                    if (code=='source_refused' and result.get('source_http_status')==403
                            and result.get('source_response_kind')=='html'
                            and isinstance(retry_id,str) and re.fullmatch(r'[1-9][0-9]{0,19}',retry_id)):
                        state['source_recovery_episode']=retry_id
                        if pending['options'].get('retry_episode_id')==retry_id:
                            state['source_recovery_delay']=min(MAX_SOURCE_DELAY,state['source_recovery_delay']*2)
                else:
                    # Legacy/invalid error receipts retain the full reservation.
                    state['unreported_reserved_pages']+=pending['options']['max_pages']
                    state['unreported_reserved_bytes']+=pending['options']['max_bytes']
                state['pending']=None
                # Legacy workers report this exact safe message without counters.
                # Keep their full reservation; never infer partial usage from another run.
                if (code=='task_failed' and state['budgets']['recover_network']
                        and value.get('error')=='Page network request failed; resume later.'):
                    code='network_unavailable'
                state.update(status='paused',code=code)
            elif not _valid_accounting(result, pending, remaining):
                state.update(status='paused',code='invalid_result')
            else:
                state['pending']=None
                _charge(state, result)
                if result['status']=='cancelled' and result.get('usage_unreported') is True:
                    # Cut off with its worker, the batch reported nothing it used: its whole allowance counts.
                    state['unreported_reserved_pages']+=pending['options']['max_pages']
                    state['unreported_reserved_bytes']+=pending['options']['max_bytes']
                # Recoveries are counted while nothing gets through: a batch that saved an episode shows
                # the connection works, however it ended.
                if result['archived_episode_ids']:state['network_recovery_attempts']=0
                retry_id=pending['options'].get('retry_episode_id')
                if retry_id is not None:
                    recovered=(result.get('reader_probe_episode_id')==retry_id
                               and result.get('reader_probe_succeeded') is True)
                    state['source_recovery_episode']=None
                    if not recovered:state.update(status='paused',code='source_access_changed')
                from .inventory import ASSET_RETRY_COOLDOWN_SECONDS
                asset_wait=result.get('asset_retry_after_seconds')
                cooling=(result['status']=='limited' and result['pending_episode_count']>0
                         and type(asset_wait) is int and 1<=asset_wait<=ASSET_RETRY_COOLDOWN_SECONDS)
                if cooling:
                    state['asset_retry_after']=(_now()+timedelta(seconds=asset_wait)).isoformat()
                if result['status']=='cancelled':state.update(status='paused',code='cancelled')
                elif result['status']=='paused' and result.get('pause_reason')=='network_policy':
                    # The connection rule held the batch; it waits for an allowed connection and Go.
                    state.update(status='paused',code='network_policy')
                elif result['status']=='paused':
                    source_status=result.get('source_http_status')
                    code=('network_unavailable' if source_status is None
                          or (type(source_status) is int and source_status in {502,503,504}) else 'task_failed')
                    state.update(status='paused',code=code)
                elif state['status']=='paused' and state['code']=='source_access_changed':pass
                elif result['pending_episode_count']==0 and (remaining==0 or state['budgets']['archive_only']):
                    state['status']='known_backlog_drained'
                elif (not cooling and not result['archived_episode_ids'] and not result['downloaded_pages']
                        and state['previous_pending_count'] is not None and state['previous_scan_remaining'] is not None
                        and result['pending_episode_count']>=state['previous_pending_count']
                        and remaining>=state['previous_scan_remaining']):
                    state.update(status='paused',code='no_progress')
                state['previous_pending_count']=result['pending_episode_count']
                state['previous_scan_remaining']=remaining
                if state['stop_requested'] and state['status']=='running':state['status']='stopped'
            _save(root,state)
            if not observe_only and (_recover_source(state) or _recover_network(state)):_save(root,state)
            # Commit accounting before considering another reservation.
            return _public(state)
        if observe_only:return _public(state)
        if state['stop_requested']:
            state['status']='stopped';_save(root,state);return _public(state)
        config=state['budgets'];seconds=(_instant(state['submit_before'])-_now()).total_seconds()
        pages=config['max_pages']-state['downloaded_pages']-state['unreported_reserved_pages']
        remaining_bytes=config['max_bytes']-state['downloaded_bytes']-state['unreported_reserved_bytes']
        if (state['submitted_batches']>=config['max_batches'] or min(seconds,pages,remaining_bytes)<=0
                or state['submitted_batches'] and seconds<MIN_BATCH_SECONDS):
            state['status']='budget_reached';_save(root,state);return _public(state)
        if state['asset_retry_after'] is not None:
            if _now() < _instant(state['asset_retry_after']):return _public(state)
            state['asset_retry_after']=None;_save(root,state)
        worker=_read(root/'status.json') or {}
        if worker.get('state')!='ready' or any((root/'requests').glob('*.json')):return _public(state)
        try:
            # Let a local audit/backup writer finish before starting another job.
            with LibraryLock(library):pass
        except LibraryBusy:return _public(state)
        operation='archive_discovered' if config['archive_only'] or state['previous_scan_remaining']==0 else 'sync'
        options={'limit':config['batch_limit'],'max_pages':min(config['batch_pages'],pages),
            'max_bytes':min(config['batch_bytes'],remaining_bytes),'max_seconds':min(config['batch_seconds'],seconds),
            'request_interval':config['request_interval'],'free_only':config['free_only']}
        if config['download_workers']!=1:options['download_workers']=config['download_workers']
        if config['speed'] == 'fast':options['speed']='fast'
        if state['source_recovery_episode'] is not None:
            operation='archive_discovered'
            options.update(limit=1,retry_episode_id=state['source_recovery_episode'])
        if operation=='sync':
            options.update(scan_limit=config['scan_limit'],refresh_lists=config['refresh_lists'] and state['submitted_batches']==0)
        state['pending']={'task_id':'job-'+secrets.token_hex(12),'phase':'reserved','operation':operation,'options':options}
        state['submitted_batches']+=1;_save(root,state)
        return _public(state)


