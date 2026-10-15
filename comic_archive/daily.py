"""Persistent daily free-only jobs for an open browser worker.

The controller touches only the fixed private filesystem queue. Browser/network
work stays in the worker's main thread and uses normal sync authorization gates.
This is not an installed service: closing the worker stops its scheduler.
"""
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .archive import atomic_json
from .archive_paths import directory
from .locking import LibraryLock
from .errors import LibraryBusy
from .models import ArchiveError
from .queue_client import submit_job, task_status

DEFAULT_OPTIONS={'limit':50,'max_pages':2000,'max_bytes':1024**3,'max_seconds':3600,
                 'request_interval':1.0,'free_only':True}
_controllers={}
_guard=threading.Lock()


def _now(value=None):
    value=datetime.now(timezone.utc) if value is None else value
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise ArchiveError('Scheduler time must be an aware datetime.')
    return value.astimezone(timezone.utc)


def _instant(value):
    try:
        if not isinstance(value,str) or len(value)>50:
            raise ValueError
        return _now(datetime.fromisoformat(value))
    except (ValueError,TypeError,ArchiveError):
        raise ArchiveError('Daily schedule contains an invalid timestamp.') from None


def _settings(at,zone,options=None):
    if not isinstance(at,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',at):
        raise ArchiveError('Daily time must be HH:MM in 24-hour format.')
    if not isinstance(zone,str) or not re.fullmatch(r'[A-Za-z0-9_+/-]{1,80}',zone):
        raise ArchiveError('Use an IANA timezone for the daily schedule.')
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError,ValueError):
        raise ArchiveError('Daily timezone is unavailable.') from None
    if options is not None and (not isinstance(options,dict) or set(options)-set(DEFAULT_OPTIONS)-{'scan_limit'}):
        raise ArchiveError('Daily runs accept only fixed bounded sync options.')
    values=DEFAULT_OPTIONS|dict(options or {})
    for key,maximum in {'limit':200,'max_pages':10000,'max_bytes':8*1024**3,'max_seconds':7200}.items():
        if type(values[key]) is not int or not 0<values[key]<=maximum:
            raise ArchiveError('Daily job budgets must be positive integers within server limits.')
    interval=values['request_interval']
    if type(interval) not in (int,float) or not .5<=interval<=60:
        raise ArchiveError('Daily request spacing must be between 0.5 and 60 seconds.')
    if values['free_only'] is not True:
        raise ArchiveError('The resident daily schedule preserves explicitly free episodes only.')
    if values.get('scan_limit') is not None:
        if type(values['scan_limit']) is not int or not 1<=values['scan_limit']<=1000:
            raise ArchiveError('Daily series scan limit must be from 1 to 1000.')
    else:
        values.pop('scan_limit',None)
    return values


def next_occurrence(now,at,zone):
    """First later local occurrence; folds run once, gaps move forward."""
    _settings(at,zone)
    now=_now(now);tz=ZoneInfo(zone)
    hour,minute=map(int,at.split(':'))
    local=now.astimezone(tz)
    for offset in range(3):
        date=local.date()+timedelta(days=offset)
        candidate=datetime(date.year,date.month,date.day,hour,minute,tzinfo=tz,fold=0).astimezone(timezone.utc)
        if candidate>now:
            return candidate
    raise ArchiveError('Cannot determine the next daily time.')


def _read(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ArchiveError('Refusing a symlinked daily schedule.')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size>16384:
        raise ArchiveError('Daily schedule file is unsafe or too large.')
    try:
        value=json.loads(path.read_text())
        if not isinstance(value,dict):raise ValueError
        return value
    except (OSError,ValueError,RecursionError):
        raise ArchiveError('Daily schedule is invalid; existing files are retained.') from None


def _load(root):
    value=_read(root/'daily.json')
    if value is None:return None
    if type(value.get('schema_version')) is not int or value['schema_version']!=1 or type(value.get('enabled')) is not bool or not isinstance(value.get('options'),dict):
        raise ArchiveError('Daily schedule has an unsupported schema.')
    value['options']=_settings(value.get('at'),value.get('timezone'),value.get('options'))
    _instant(value.get('next_due'))
    if value.get('pause_reason') not in {None,'login_required','job_failed','scheduler_failed'}:
        raise ArchiveError('Daily pause state is invalid.')
    pending=value.get('pending')
    if pending is not None:
        if not isinstance(pending,dict) or not re.fullmatch(r'job-[a-f0-9]{24}',str(pending.get('task_id',''))):
            raise ArchiveError('Daily pending job identity is invalid.')
        if not isinstance(pending.get('options'),dict):raise ArchiveError('Daily pending options are invalid.')
        pending['options']=_settings(value['at'],value['timezone'],pending['options'])
        _instant(pending.get('due'))
    last=value.get('last_result')
    if last is not None:
        if (not isinstance(last,dict) or not isinstance(last.get('task_id'),str) or not re.fullmatch(r'job-[a-f0-9]{24}',last['task_id'])
                or last.get('status') not in {'finished','limited','login_required','job_failed'}):
            raise ArchiveError('Daily result summary is invalid.')
        _instant(last.get('completed_at'))
        for key in ('archived_episode_count','downloaded_pages','downloaded_bytes'):
            if key in last and (type(last[key]) is not int or last[key]<0):raise ArchiveError('Daily result metrics are invalid.')
    return value


def _save(root,value):
    atomic_json(root/'daily.json',value)
    (root/'daily.json').chmod(0o600)


def _public(value):
    if value is None:return {'configured':False,'enabled':False,'scope':'free_only'}
    result={'configured':True,'enabled':value['enabled'],'scope':'free_only',
            'at':value['at'],'timezone':value['timezone'],'next_due':value['next_due'],
            'options':value['options'],'pause_reason':value.get('pause_reason'),
            'pending_task_id':value['pending']['task_id'] if value.get('pending') else None}
    last=value.get('last_result')
    if isinstance(last,dict):
        result['last_result']={k:last[k] for k in ('task_id','completed_at','status','archived_episode_count','downloaded_pages','downloaded_bytes')
                               if k in last and (k in {'task_id','completed_at','status'} or type(last[k]) is int)}
    return result


def status(session):
    """Configuration is distinct from a running controller or healthy session."""
    result=_public(_load(Path(session)/'worker'))
    with _guard:
        record=_controllers.get(str(Path(session).absolute()))
        result['controller_running']=record[0].is_alive() if record else None
    return result


def validate_configuration(args):
    if not isinstance(args,dict) or set(args)-{'enabled','at','timezone','options','run_now'}:
        raise ArchiveError('Daily configuration accepts only schedule choices and fixed budgets.')
    enabled=args.get('enabled',True);run_now=args.get('run_now',False)
    if type(enabled) is not bool or type(run_now) is not bool or (run_now and not enabled):
        raise ArchiveError('Daily enable/run-now choices must be explicit booleans.')
    at=args.get('at','08:00');zone=args.get('timezone','America/Chicago')
    return {'enabled':enabled,'at':at,'timezone':zone,'options':_settings(at,zone,args.get('options')),'run_now':run_now}


def configure(session,*,enabled=True,at='08:00',zone='America/Chicago',options=None,run_now=False,now=None):
    if type(enabled) is not bool or type(run_now) is not bool or (run_now and not enabled):
        raise ArchiveError('Daily enable/run-now choices must be explicit booleans.')
    settings=_settings(at,zone,options);now=_now(now)
    root=directory(Path(session)/'worker');root.chmod(0o700)
    with LibraryLock(root/'.daily-state'):
        old=_load(root) or {}
        value={'schema_version':1,'enabled':enabled,'at':at,'timezone':zone,'options':settings,
               'next_due':(now if run_now else next_occurrence(now,at,zone)).isoformat(),
               'pause_reason':None,'pending':old.get('pending'),'last_result':old.get('last_result')}
        _save(root,value)
    return _public(value)


def tick(session,*,now=None):
    """Reserve before enqueueing; recover the same task after a process crash."""
    root=Path(session)/'worker';now=_now(now)
    if _load(root) is None:return {'configured':False,'enabled':False,'scope':'free_only'}
    with LibraryLock(root/'.daily-state'):
        value=_load(root)
        pending=value.get('pending')
        if pending:
            task=pending['task_id']
            request=root/'requests'/(task+'.json');response=root/'responses'/(task+'.json')
            if not request.exists() and not response.exists() and not request.is_symlink() and not response.is_symlink():
                # Reservation persisted but process ended before queue publication.
                if value['enabled']:
                    submit_job(session,'sync',pending['options'],_task_id=task)
                else:
                    value['pending']=None;_save(root,value)
                return _public(value)
            observed=task_status(session,task)
            if observed['status'] in {'running','queued'}:return _public(value)
            result=observed.get('result',{})
            if not isinstance(result,dict):result={}
            successful=observed['status']=='ok' and result.get('status') in {'finished','limited'}
            outcome=result.get('status') if successful else ('login_required' if observed.get('error_code')=='login_required' else 'job_failed')
            last={'task_id':task,'completed_at':now.isoformat(),'status':outcome}
            if successful:
                ids=result.get('archived_episode_ids',[])
                last['archived_episode_count']=len(ids) if isinstance(ids,list) else 0
                for key in ('downloaded_pages','downloaded_bytes'):
                    if type(result.get(key)) is int and result[key]>=0:last[key]=result[key]
            else:value['pause_reason']=outcome
            value['last_result']=last;value['pending']=None;_save(root,value)
        if not value['enabled'] or value.get('pause_reason') or _instant(value['next_due'])>now:
            return _public(value)
        # Coalesce while manual jobs/inspections are queued or a writer is active.
        worker=_read(root/'status.json') or {}
        if worker.get('state')!='ready' or any((root/'requests').glob('*.json')):
            return _public(value)
        task='job-'+secrets.token_hex(12)
        value['pending']={'task_id':task,'due':value['next_due'],'options':dict(value['options'])}
        value['next_due']=next_occurrence(now,value['at'],value['timezone']).isoformat()
        _save(root,value)
        submit_job(session,'sync',value['pending']['options'],_task_id=task)
        return _public(value)


def start_scheduler(session):
    """Called by the owning browser worker; the thread never touches Playwright."""
    session=Path(session).absolute();root=session/'worker'
    value=_load(root)
    if value is None or not value['enabled']:return False
    key=str(session)
    with _guard:
        previous=_controllers.get(key)
        if previous and previous[0].is_alive():return True
        stop=threading.Event()
        def loop():
            while not stop.is_set():
                try:
                    worker=_read(root/'status.json') or {}
                    if worker.get('state')=='stopped':break
                    tick(session)
                except LibraryBusy:
                    pass  # A configuration writer/controller holds the short state lock.
                except (ArchiveError,OSError):
                    # Fail closed; do not log response/configuration details.
                    try:
                        with LibraryLock(root/'.daily-state'):
                            state=_load(root)
                            if state:
                                state['pause_reason']='scheduler_failed';_save(root,state)
                    except ArchiveError:pass
                    break
                stop.wait(5)
        thread=threading.Thread(target=loop,name='comic-daily',daemon=True)
        _controllers[key]=(thread,stop);thread.start()
    return True


def stop_scheduler(session):
    with _guard:
        record=_controllers.pop(str(Path(session).absolute()),None)
    if record:
        record[1].set();record[0].join(timeout=2)
