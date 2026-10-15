"""Read-only status for CLI and future owner-scoped website handlers."""
import json
import re
import shutil
from pathlib import Path

from . import sites
from .inventory import Inventory


def _read(path):
    try:
        if any(p.is_symlink() for p in (path,*path.parents)) or path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,RecursionError):
        return {}


def _progress(value):
    result = {}
    for key in ('completed','downloaded_pages','downloaded_chapters','downloaded_bytes','history_page_count','observed_history_series_count',
                'reading_list_page_count','observed_library_series_count','series_checks_completed',
                'successful_series_checks','held_series_checks','series_check_target_count'):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ('series_id','episode_id'):
        if isinstance(value.get(key),str) and re.fullmatch(r'[1-9][0-9]{0,19}',value[key]):
            result[key] = value[key]
    if isinstance(value.get('phase'),str) and value['phase'] in {'authentication','history','history_series','account_list','scan','metadata'}:
        result['phase'] = value['phase']
    elif 'episode_id' in result:
        result['phase'] = 'archive'
    if isinstance(value.get('category'),str) and value['category'] in sites.LISTS:
        result['category'] = value['category']
    if isinstance(value.get('source_slug'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value['source_slug']):
        result['source_slug'] = value['source_slug']
    if isinstance(value.get('status'),str) and value['status'] in {'running','finished','limited','paused','interrupted','login_required','cancelled'}:
        result['status'] = value['status']
    if type(value.get('cancellation_supported')) is bool:
        result['cancellation_supported']=value['cancellation_supported']
    if value.get('status') == 'limited':
        from .inventory import ASSET_RETRY_COOLDOWN_SECONDS
        wait = value.get('asset_retry_after_seconds')
        if type(wait) is int and 1 <= wait <= ASSET_RETRY_COOLDOWN_SECONDS:
            result['asset_retry_after_seconds'] = wait
    if value.get('status') == 'paused':
        reason = value.get('pause_reason')
        code = value.get('source_http_status')
        if (reason == 'source_refused' and type(code) is int and code in {403,429}
                or reason == 'network_unavailable' and (code is None or type(code) is int and code in {502,503,504})):
            result['pause_reason'] = reason
            if code is not None:
                result['source_http_status'] = code
            if reason == 'source_refused':
                from .source_response import source_response_diagnostics
                result.update(source_response_diagnostics(value))
    return result


def library_status(output,session=None):
    """Caller supplies its authorized workspace; paths never come from a job."""
    root = Path(output)
    with Inventory(root,read_only=True) as inventory:
        result = inventory.status()
    from .estimate import remaining
    result['estimate'] = remaining(result['episodes_by_status'], result['series_count'],
                                   result['series_without_scan_checkpoint_count'], result['recent_pace'], result['saved_bytes'])
    try:
        result['estimate']['free_bytes'] = shutil.disk_usage(root).free
    except OSError:
        result['estimate']['free_bytes'] = None
    value = _read(root/'.archive-state/progress.json')
    last = result['last_run']
    result['progress'] = _progress(value) if last and value.get('run_id')==last['id'] else None
    if result['progress'] and last['status'] in {'paused','limited'}:
        summary = last.get('summary')
        if isinstance(summary, dict):
            hold = _progress(summary)
            for key in ('pause_reason','source_http_status','source_response_kind','asset_retry_after_seconds'):
                if key in hold:
                    result['progress'][key] = hold[key]
    # Stored summaries serve local accounting and diagnostics. The monitor
    # publishes fixed progress fields and run identity, never arbitrary payloads.
    result['last_run'] = {key:last[key] for key in ('id','started_at','finished_at','status')} if last else None
    if result['progress'] and result['progress'].get('cancellation_supported') and last['status']=='running':
        from .cancellation import RunCancellation
        result['progress']['cancellation_requested']=RunCancellation(root,last['id']).requested()
    result['worker'] = None
    if session is not None:
        value = _read(Path(session)/'worker/status.json')
        if isinstance(value.get('state'),str) and value['state'] in {'starting','ready','busy','stopped'}:
            worker = {'reported_state':value['state']}
            task = value.get('task_id')
            if isinstance(task,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',task):
                worker['task_id'] = task
                progress = _read(Path(session)/'worker/progress.json')
                if worker['reported_state']=='busy' and progress.get('task_id')==task:
                    worker['progress'] = _progress(progress)
            result['worker'] = worker
    result['source_recovery'] = None
    if session is not None and (Path(session)/'worker/backlog.json').exists():
        from . import background,batches,completion
        from .models import ArchiveError
        try:
            backlog=batches.status(session,output)
            if backlog.get('source_recovery_wait') and backlog['submission_window_open']:
                folder=Path(session)/'worker'
                plan=completion.status(session,output) if (folder/'completion.json').exists() else {}
                active=(plan.get('status')=='running' or background.running(folder,'.backlog-runtime'))
                result['source_recovery']={'episode_id':backlog['source_recovery_episode'],
                    'retry_at':backlog['source_recovery_wait'],'attempts':backlog['source_recovery_attempts'],
                    'limit':backlog['budgets']['source_recovery_limit'],'learned_delay_seconds':backlog['source_recovery_delay'],
                    'automatic':bool(active and background.status(session)['running'] is True)}
        except (ArchiveError,OSError):pass
    return result
