"""Small private fixed-operation queue client, reusable by CLI or a web UI."""
import json
import math
import secrets
import re
from pathlib import Path
from . import sites
from .archive import atomic_json
from .archive_paths import directory
from .models import ArchiveError, identifier


def submit_job(session, operation, options=None,*,_task_id=None):
    if operation == 'audit_account':
        if options:
            raise ArchiveError('Account audits accept no paths or source options.')
        return _submit(session, operation, {}, task_id=_task_id)
    if operation not in {"archive_discovered", "sync"}:
        raise ArchiveError("Only fixed archive or sync jobs can be submitted.")
    options = dict(options or {})
    allowed = {"limit", "max_pages", "max_bytes", "max_seconds", "free_only",'request_interval', 'download_workers'}
    if operation == 'archive_discovered':
        allowed.add('retry_episode_id')
        retry_id = options.get('retry_episode_id')
        if retry_id is not None and (not isinstance(retry_id, str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', retry_id) or options.get('limit') != 1):
            raise ArchiveError('Reader recovery probes require one numeric episode identity.')
    from .fast_import import FAST_OPERATIONS, validate_speed
    speed = validate_speed(options.get('speed', 'normal'))
    if operation in FAST_OPERATIONS:
        allowed.add('speed')
    elif speed != 'normal' or 'speed' in options:
        raise ArchiveError('Fast imports save known or chosen series; refresh the collection first.')
    from .downloads import validate_download_workers
    validate_download_workers(options.get('download_workers', 1))
    if operation == "sync":
        allowed.add('metadata_speed')
        validate_speed(options.get('metadata_speed', 'normal'))
        if options.get('metadata_speed') == 'fast' and options.get('scan_only') is not True:
            raise ArchiveError('Fast metadata refresh requires scan-only mode.')
        allowed.add('scan_only')
        allowed.add("categories")
        allowed.add('refresh_lists')
        allowed.add('include_history')
        allowed.add('history_pages')
        allowed.add('history_resume')
        allowed.add('scan_limit')
    if (set(options) - allowed or type(options.get("free_only", False)) is not bool
            or type(options.get('scan_only',False)) is not bool
            or type(options.get('include_history',False)) is not bool
            or type(options.get('history_resume',False)) is not bool
            or (options.get('history_resume') and not options.get('include_history'))
            or type(options.get('refresh_lists',True)) is not bool
            or (not options.get('refresh_lists',True) and (options.get('categories') is not None or options.get('include_history',False)))):
        raise ArchiveError("Unsupported queue job options.")
    for key in ("limit", "max_pages", "max_bytes",'scan_limit'):
        if options.get(key) is not None and (type(options[key]) is not int or options[key] < 1):
            raise ArchiveError("Queued episode/page/byte limits must be positive integers.")
    seconds = options.get("max_seconds")
    if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
        raise ArchiveError("Queued time limits must be finite positive numbers.")
    interval=options.get('request_interval',1.0)
    from .downloads import validate_request_interval
    validate_request_interval(interval)
    categories = options.get("categories")
    history_pages=options.get('history_pages',100)
    if type(history_pages) is not int or not 1 <= history_pages <= 500:
        raise ArchiveError('Queued history page limit must be from 1 to 500.')
    if categories is not None and (not isinstance(categories, list) or not categories or
            any(type(c) is not str or c not in sites.LISTS for c in categories)
            or len(set(categories)) != len(categories)):
        raise ArchiveError("Invalid queued reading-list categories.")
    return _submit(session,operation,{k: v for k, v in options.items() if v is not None},task_id=_task_id)


def submit_inspection(session,operation,options=None):
    options = dict(options or {})
    if operation in {'cloud_test', 'cloud_cleanup'}:
        from .cloud import validate_test_action
        validate_test_action(operation, options)
    elif operation == 'cloud_upload':
        if options:
            raise ArchiveError('Cloud uploads accept no account IDs, URLs, paths or credentials.')
    else:
        raise ArchiveError('Only fixed cloud tasks can be submitted.')
    return _submit(session,operation,options)


def _submit(session,operation,options,*,task_id=None):
    root = Path(session) / "worker"
    if not (root / "requests").is_dir():
        raise ArchiveError("Start the downloader before submitting jobs.")
    requests = directory(root / "requests")
    requests.chmod(0o700)
    reserved=task_id is not None
    if reserved and (not isinstance(task_id,str) or not re.fullmatch(r'job-[a-f0-9]{24}',task_id)):
        raise ArchiveError('A reserved queue task must have a server-generated job identity.')
    task_id = task_id or "job-" + secrets.token_hex(12)
    path = requests / (task_id + ".json")
    if reserved:
        response=root/'responses'/(task_id+'.json')
        if response.exists() or response.is_symlink():
            value=task_status(session,task_id)
            return {'task_id':task_id,'status':value['status'],'output':'Library'}
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.stat().st_size>16384:
                raise ArchiveError('Refusing an unsafe reserved queue request.')
            try:
                existing=json.loads(path.read_text())
            except (OSError,ValueError):
                raise ArchiveError('Reserved queue request is invalid; no replacement was made.') from None
            if existing!={'operation':operation,'args':options}:
                raise ArchiveError('Reserved queue identity already belongs to a different request.')
            return {'task_id':task_id,'status':task_status(session,task_id)['status'],'output':'Library'}
    atomic_json(path, {"operation": operation, "args": options})
    path.chmod(0o600)
    return {"task_id": task_id, "status": "queued", "output": "Library"}


def task_status(session, task_id):
    task_id = identifier(task_id)
    root = Path(session) / "worker"
    response = root / "responses" / (task_id + ".json")
    if response.exists():
        if any(p.is_symlink() for p in (response, *response.parents)) or response.stat().st_size > 2 * 1024 * 1024:
            raise ArchiveError("Refusing an unsafe worker response.")
        try:
            value = json.loads(response.read_text())
            if value.get("task_id") != task_id or value.get("status") not in {"ok", "error"}:
                raise ValueError
            return value
        except (ValueError, AttributeError):
            raise ArchiveError("Worker response has an invalid schema.") from None
    request = root / "requests" / (task_id + ".json")
    if request.is_file():
        status = root / "status.json"
        busy = False
        if status.is_file() and not status.is_symlink():
            try:
                observed = json.loads(status.read_text())
                busy = observed.get("state") == "busy" and observed.get("task_id") == task_id
            except (OSError, ValueError, AttributeError):
                pass
        return {"task_id": task_id, "status": "running" if busy else "queued"}
    raise ArchiveError("No queued job or result exists for that task ID.")
