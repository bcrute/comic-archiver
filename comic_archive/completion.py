"""Resident, bounded continuation of the known backlog across import windows.

The browser worker calls tick between tasks. This controller never touches the
browser, discovers new source IDs or relaxes authorization. A small ledger
counts each window once, including unknown usage reserved by legacy workers.
"""
from datetime import timedelta
from pathlib import Path

from . import batches
from .archive import atomic_json
from .errors import LibraryBusy
from .locking import LibraryLock
from .models import ArchiveError

DEFAULT_LIMITS={'max_windows':7,'max_days':7,'max_pages':700000,
                'max_bytes':448*1024**3,'max_network_recoveries':4,'max_source_recoveries':8}
WINDOW_DEFAULTS=batches.DEFAULTS|{'max_batches':48,'max_pages':100000,
    'max_bytes':64*1024**3,'max_seconds':86400,'archive_only':True,
    'refresh_lists':False,'recover_network':True}
METRICS=('archived_episodes','downloaded_pages','downloaded_bytes',
         'unreported_reserved_pages','unreported_reserved_bytes','network_recovery_attempts','source_recovery_attempts')
STATES={'running','paused','stopped','budget_reached','known_backlog_drained'}
REASONS={None,*batches.CODES,'window_changed','accounting_changed','controller_failed',
         'network_recovery_limit','source_recovery_limit'}
AGGREGATE_LIMITS={'max_windows','max_days','max_pages','max_bytes'}


def limits(values=None, *, until_drained=False):
    if type(until_drained) is not bool:
        raise ArchiveError('Until-drained continuation must be an explicit boolean.')
    if values is not None and (not isinstance(values,dict) or set(values)-set(DEFAULT_LIMITS)):
        raise ArchiveError('Backlog completion accepts only fixed overall limits.')
    result=DEFAULT_LIMITS|dict(values or {})
    for key,cap in DEFAULT_LIMITS.items():
        if until_drained and key in AGGREGATE_LIMITS:
            result[key]=None
            continue
        minimum=0 if key in {'max_network_recoveries','max_source_recoveries'} else 1
        if type(result[key]) is not int or not minimum<=result[key]<=cap:
            raise ArchiveError('Use positive completion limits within seven days/windows and the local resource caps.')
    return result


def _load(root):
    value=batches._read(root/'completion.json')
    if value is None:return None
    try:
        if (type(value['schema_version']) is not int or value['schema_version']!=1
                or value['status'] not in STATES or value['code'] not in REASONS):raise ValueError
        until_drained=value.get('until_drained',False)
        if until_drained and any(value['limits'].get(key) is not None for key in AGGREGATE_LIMITS):raise ValueError
        value['limits']=limits(value['limits'],until_drained=until_drained)
        value['window_budgets']=batches.budgets(value['window_budgets'])
        batches._instant(value['started_at'])
        if not value['window_budgets']['archive_only'] or value['window_budgets']['refresh_lists']:raise ValueError
        if until_drained:
            if value['submit_before'] is not None:raise ValueError
        elif not 0<(batches._instant(value['submit_before'])-batches._instant(value['started_at'])).total_seconds()<=value['limits']['max_days']*86400:
            raise ValueError
        windows=value['windows']
        if not isinstance(windows,list) or (not until_drained and len(windows)>value['limits']['max_windows']):raise ValueError
        seen=set()
        for window in windows:
            if isinstance(window,dict):window.setdefault('source_recovery_attempts',0)
            if not isinstance(window,dict) or set(window)!={'started_at',*METRICS}:raise ValueError
            stamp=batches._instant(window['started_at']).isoformat()
            if stamp in seen:raise ValueError
            seen.add(stamp)
            if any(type(window[k]) is not int or window[k]<0 for k in METRICS):raise ValueError
        reserved=value['reserved_window']
        if reserved is not None:
            if (not isinstance(reserved,dict) or set(reserved)!={'started_at','budgets'}
                    or not until_drained and len(windows)>=value['limits']['max_windows']):raise ValueError
            batches._instant(reserved['started_at'])
            config=batches.budgets(reserved['budgets'])
            if (not config['archive_only'] or config['refresh_lists']
                    or reserved['started_at'] in seen):raise ValueError
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ArchiveError('Backlog completion checkpoint is invalid; existing work is retained.') from None
    return value


def _save(root,value):
    atomic_json(root/'completion.json',value);(root/'completion.json').chmod(0o600)


def _totals(value):
    return {key:sum(window[key] for window in value['windows']) for key in METRICS}


def _public(value):
    if value is None:return {'configured':False,'account_inventory_complete':False}
    return {key:value[key] for key in ('status','code','started_at','submit_before','limits')}|{
        'configured':True,'account_inventory_complete':False,'scope':'known_backlog_only',
        'until_drained':value.get('until_drained',False),
        'window_count':len(value['windows']),'window_reserved':value['reserved_window'] is not None,
        'window_budgets':value['window_budgets'],**_totals(value)}


def status(session,library=Path('Library')):
    return _public(_load(batches._root(session,library)))


def _record(window):
    return {'started_at':window['started_at'],**{key:window.get(key,0) for key in METRICS}}


def configure(session,library=Path('Library'),values=None,*,resume=False,window_budgets=None,until_drained=False):
    root=batches._root(session,library)
    with LibraryLock(root/'.completion-state'),LibraryLock(root/'.backlog-state'):
        old=_load(root);window=batches._load(root)
        if type(until_drained) is not bool or resume and until_drained:
            raise ArchiveError('Enable until-drained explicitly on a new configuration; resume retains its saved policy.')
        if resume:
            if old is None:raise ArchiveError('No backlog completion plan exists to resume.')
            # Explicitly repairing/restarting a failed window remains separate.
            if window and (window['status'] not in {'running','budget_reached'} or window['code'] is not None):
                raise ArchiveError('Repair and explicitly resume the held backlog window before resuming completion.')
            old.update(status='running',code=None);_save(root,old);return _public(old)
        if old and (old['status']=='running' or old['reserved_window'] is not None):
            raise ArchiveError('A completion plan already exists. Use completion-status, stop-completion or --resume.')
        config=limits(values,until_drained=until_drained);budget=batches.budgets(WINDOW_DEFAULTS|dict(window_budgets or {}))
        if not budget['archive_only'] or budget['refresh_lists']:
            raise ArchiveError('Completion windows archive the observed queue; daily jobs perform discovery.')
        if window and (window['status'] not in {'running','budget_reached','known_backlog_drained'}
                       or window['stop_requested'] or window['code'] is not None):
            raise ArchiveError('A previous backlog is held; resolve it before creating a completion plan.')
        if window and ((not until_drained and (window['budgets']['max_pages']>config['max_pages']
                       or window['budgets']['max_bytes']>config['max_bytes']))
                       or window['network_recovery_attempts']>config['max_network_recoveries']):
            raise ArchiveError('The existing window does not fit the completion limits; let it finish first.')
        if until_drained and old:
            if old['status'] not in {'budget_reached','known_backlog_drained'} or not _credit(old,window):
                raise ArchiveError('Until-drained continuation requires a resolved previous plan; holds retain their repair requirement.')
            # Removing aggregate caps must never erase usage, unknown legacy
            # reservations, learned waits or spent recovery attempts.
            old.update(until_drained=True,limits=limits(old['limits'],until_drained=True),
                       submit_before=None,status='running',code=None,window_budgets=budget)
            _save(root,old);return _public(old)
        now=batches._now()
        if window:
            window['budgets']['network_recovery_limit']=min(window['budgets']['network_recovery_limit'],
                                                           config['max_network_recoveries'])
            batches._save(root,window)
        value={'schema_version':1,'status':'running','code':None,'limits':config,'window_budgets':budget,
               'started_at':now.isoformat(),'submit_before':None if until_drained else (now+timedelta(days=config['max_days'])).isoformat(),
               'until_drained':until_drained,
               'windows':[_record(window)] if window else [],'reserved_window':None}
        _save(root,value);return _public(value)


def request_stop(session,library=Path('Library')):
    root=batches._root(session,library)
    with LibraryLock(root/'.completion-state'):
        value=_load(root)
        if value is None:raise ArchiveError('No backlog completion plan exists.')
        value.update(status='stopped',code=None);_save(root,value)
        if batches._load(root) is not None:batches.request_stop(session,library)
    return _public(value)


def continue_discovered(session, library=Path('Library'), *, window_budgets=None):
    """Renew a drained window after discovery, preserving the existing ledger."""
    root = batches._root(session, library)
    with LibraryLock(root / '.completion-state'), LibraryLock(root / '.backlog-state'):
        value = _load(root)
        window = batches._load(root)
        if value is not None:
            if (value['status'] not in {'known_backlog_drained', 'budget_reached'} or window is None
                    or window['status'] not in {'known_backlog_drained', 'budget_reached'} or window['pending'] is not None
                    or window['code'] is not None or window['stop_requested'] or not _credit(value, window)):
                raise ArchiveError('Only a drained or budget-ended resolved backlog can renew after discovery.')
            budget = batches.budgets(WINDOW_DEFAULTS | dict(window_budgets or {}))
            if not budget['archive_only'] or budget['refresh_lists']:
                raise ArchiveError('Discovery continuation uses archive-only windows.')
            value.update(status='running', code=None, until_drained=True, submit_before=None,
                         limits=limits(value['limits'], until_drained=True), window_budgets=budget)
            # Publish intent first. If interrupted, an unchanged drained window
            # remains drained; a changed window is renewed by the normal tick.
            _save(root, value)
            window['status'] = 'budget_reached'
            batches._save(root, window)
            return _public(value)
    return configure(session, library, until_drained=True, window_budgets=window_budgets)


def _credit(value,window):
    if window is None:
        if value['windows']:value.update(status='paused',code='window_changed');return False
        return True
    if not value['windows'] or window['started_at']!=value['windows'][-1]['started_at']:
        value.update(status='paused',code='window_changed');return False
    record=value['windows'][-1]
    if window.get('network_recovery_attempts',0)<record['network_recovery_attempts']:
        # The window was given its network recoveries back, by progress or by the person resuming it:
        # what earlier windows spent no longer counts against the plan either.
        for earlier in value['windows']:earlier['network_recovery_attempts']=0
    if any(window.get(key,0)<record[key] for key in METRICS):
        value.update(status='paused',code='accounting_changed');return False
    record.update({key:window.get(key,0) for key in METRICS});return True


def _daily_waiting(root):
    from .daily import _load as load_daily
    daily=load_daily(root)
    return bool(daily and daily['enabled'] and not daily['pause_reason'] and (
        daily['pending'] is not None or batches._instant(daily['next_due'])<=batches._now()))


def _waiting(window):
    return bool(window['network_retry_after'] or window['source_recovery_wait'])


def _hold_reason(window,totals,cap):
    if window['code']=='network_unavailable' and totals['network_recovery_attempts']>=cap['max_network_recoveries']:
        return 'network_recovery_limit'
    if window['code']=='source_refused' and totals['source_recovery_attempts']>=cap['max_source_recoveries']:
        return 'source_recovery_limit'
    return window['code']


def _expired(value):
    return value['submit_before'] is not None and batches._now()>=batches._instant(value['submit_before'])


def _advance(root,session,library,value):
    window=batches._load(root)
    reserved=value['reserved_window']
    if reserved:
        if (_expired(value)
                and (window is None or window['started_at']!=reserved['started_at'])):
            value.update(status='budget_reached',code=None,reserved_window=None);return
        if window is None or window['started_at']!=reserved['started_at']:
            if window:
                if not _credit(value,window):return
                if (window['pending'] is not None or window['status']!='budget_reached'
                        or window['code'] is not None or window['stop_requested']):
                    value.update(status='paused',code=window['code'] or 'window_changed');return
            # Persisted timestamp makes publication idempotent, even if the
            # process died after replacing backlog.json but before our ledger.
            with LibraryLock(root/'.backlog-state'):
                window=batches._start(root,reserved['budgets'],False,
                    now=batches._instant(reserved['started_at']))
        value['windows'].append(_record(window));value['reserved_window']=None;_save(root,value)
    if not _credit(value,window):return
    totals=_totals(value);cap=value['limits']
    if window and (window['status']=='paused' and not _waiting(window)
                   or window['status']=='budget_reached' and window['code'] is not None):
        code=_hold_reason(window,totals,cap)
        value.update(status='paused',code=code);return
    exhausted=(_expired(value)
        or cap['max_pages'] is not None and totals['downloaded_pages']+totals['unreported_reserved_pages']>=cap['max_pages']
        or cap['max_bytes'] is not None and totals['downloaded_bytes']+totals['unreported_reserved_bytes']>=cap['max_bytes'])
    if exhausted:
        value.update(status='budget_reached',code=None)
        if window and window['status']=='running':batches.request_stop(session,library)
        if window and window['pending'] is not None:
            batches.step(session,library,observe_only=True);_credit(value,batches._load(root))
        return
    if window:
        if window['stop_requested'] or window['status'] in {'stopped','interrupted'}:
            value.update(status='stopped',code=None);return
        remaining_source=cap['max_source_recoveries']-totals['source_recovery_attempts']
        source_limit=min(window['budgets']['source_recovery_limit'],window['source_recovery_attempts']+max(0,remaining_source))
        if source_limit!=window['budgets']['source_recovery_limit']:
            with LibraryLock(root/'.backlog-state'):
                window=batches._load(root);window['budgets']['source_recovery_limit']=source_limit;batches._save(root,window)
        remaining_recoveries=cap['max_network_recoveries']-totals['network_recovery_attempts']
        local_limit=min(window['budgets']['network_recovery_limit'],
                        window['network_recovery_attempts']+max(0,remaining_recoveries))
        if local_limit!=window['budgets']['network_recovery_limit'] or remaining_recoveries<=0 and window['budgets']['recover_network']:
            with LibraryLock(root/'.backlog-state'):
                window=batches._load(root)
                window['budgets']['network_recovery_limit']=local_limit
                if remaining_recoveries<=0:
                    window['budgets']['recover_network']=False;window['network_retry_after']=None
                batches._save(root,window)
        if window['status']=='known_backlog_drained':
            value.update(status='known_backlog_drained',code=None);return
        if window['status']=='paused' and not _waiting(window):
            code=_hold_reason(window,totals,cap)
            value.update(status='paused',code=code);return
        if window['status']=='budget_reached':
            if window['pending'] is not None:
                batches.step(session,library,observe_only=True);_credit(value,batches._load(root));return
            # A pause that spent its last allocation is still a hold. A new
            # window never resets refusal/network retry limits.
            if window['code'] is not None:
                value.update(status='paused',code=window['code']);return
        elif window['status']=='running' or _waiting(window):
            if not _daily_waiting(root):batches.step(session,library)
            _credit(value,batches._load(root));return
    if cap['max_windows'] is not None and len(value['windows'])>=cap['max_windows']:
        value.update(status='budget_reached',code=None);return
    if _daily_waiting(root):return
    config=dict(value['window_budgets'])
    for key,metric,reservation in (('max_pages','downloaded_pages','unreported_reserved_pages'),
                                   ('max_bytes','downloaded_bytes','unreported_reserved_bytes')):
        if cap[key] is not None:config[key]=min(config[key],cap[key]-totals[metric]-totals[reservation])
    if value['submit_before'] is not None:
        config['max_seconds']=min(config['max_seconds'],int((batches._instant(value['submit_before'])-batches._now()).total_seconds()))
    config['recover_network']=config['recover_network'] and totals['network_recovery_attempts']<cap['max_network_recoveries']
    config['network_recovery_limit']=min(config['network_recovery_limit'],
        max(0,cap['max_network_recoveries']-totals['network_recovery_attempts']))
    config['source_recovery_limit']=min(config['source_recovery_limit'],
        max(0,cap['max_source_recoveries']-totals['source_recovery_attempts']))
    if config['max_seconds']<1:value.update(status='budget_reached',code=None);return
    value['reserved_window']={'started_at':batches._now().isoformat(),'budgets':batches.budgets(config)}
    _save(root,value)


def tick(session,library=Path('Library')):
    root=batches._root(session,library)
    if _load(root) is None:return _public(None)
    with LibraryLock(root/'.completion-state'):
        value=_load(root)
        if value['status']!='running':return _public(value)
        try:
            with LibraryLock(root/'.backlog-runtime'):_advance(root,session,library,value)
        except LibraryBusy:
            # An existing foreground controller owns this window. Observe its
            # monotonic totals, but neither publish nor replace its task.
            _credit(value,batches._load(root))
        _save(root,value);return _public(value)


def worker_tick(session,library=Path('Library')):
    """A malformed plan holds itself, without taking down the browser worker."""
    try:return tick(session,library)
    except LibraryBusy:return None
    except (ArchiveError,OSError):
        try:
            root=batches._root(session,library)
            with LibraryLock(root/'.completion-state'):
                value=_load(root)
                if value:value.update(status='paused',code='controller_failed');_save(root,value)
        except (ArchiveError,OSError):pass
        return None
