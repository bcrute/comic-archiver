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

QUEUE_CLIENT_VERSION = 16

def submit_job(session, operation, options=None,*,_task_id=None):
    if operation == 'audit_account':
        if options:
            raise ArchiveError('Account audits accept no paths or source options.')
        return _submit(session, operation, {}, task_id=_task_id)
    if operation=='enrich_series':
        options=dict(options or {})
        if (set(options)-{'id','refresh'} or not isinstance(options.get('id'),str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',options['id']) or type(options.get('refresh',False)) is not bool):
            raise ArchiveError('Series enrichment accepts an observed ID/slug and a refresh boolean.')
        return _submit(session,operation,options,task_id=_task_id)
    if operation not in {"archive_discovered", "sync", "archive_watched_series", "archive_series_url"}:
        raise ArchiveError("Only fixed archive, selected-series, sync or metadata-enrichment jobs can be submitted.")
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
    if operation == 'archive_series_url':
        allowed.add('url')
        from .selection import series_link
        series_link(options.get('url'))
    if operation == 'archive_watched_series':
        allowed.add('id')
        if not isinstance(options.get('id'),str) or not re.fullmatch(r'[1-9][0-9]{0,19}',options['id']):
            raise ArchiveError('Selected-series jobs require an observed numeric source identity.')
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
    if operation == 'preview_series':
        from .preflight import validate_options
        validate_options(options)
    elif operation in {'cloud_test', 'cloud_cleanup'}:
        from .cloud import validate_test_action
        validate_test_action(operation, options)
    elif operation in {'check_session', 'purge_cache', 'cloud_upload'}:
        if options:
            raise ArchiveError('Session checks accept no account IDs, URLs, paths or credentials.')
    elif operation=='configure_priority':
        from .expiry import queue_priority
        if set(options)!={'priority'}: raise ArchiveError('Queue order accepts only a priority choice.')
        queue_priority(options['priority'])
    elif operation=='inspect_account_navigation':
        if options:
            raise ArchiveError('Account navigation inspection accepts no URLs, account IDs or actions.')
    elif operation=='inspect_episode_page_size':
        if set(options)-{'id','max_limit'} or not isinstance(options.get('id'),str) or not re.fullmatch(r'[1-9][0-9]{0,19}',options['id']) or type(options.get('max_limit',50)) is not int or options.get('max_limit',50) not in {20,50,100}:
            raise ArchiveError('Page-size inspection accepts an observed numeric series ID and size 20, 50 or 100.')
    elif operation=='diagnose_headless':
        if set(options)!={'url'} or not isinstance(options.get('url'),str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',options['url']):
            raise ArchiveError('Background test requires an exact observed episode URL.')
    elif operation=='inspect_episode':
        if set(options)-{'url','details'} or not isinstance(options.get('url'),str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',options['url']) or type(options.get('details',False)) is not bool:
            raise ArchiveError('Episode inspection accepts an observed exact URL and a details boolean.')
    elif operation in {'inspect_reader_identity','inspect_episode_access'}:
        if set(options)!={'url'} or not isinstance(options['url'],str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',options['url']):
            raise ArchiveError('Reader identity inspection requires an observed exact episode URL.')
    elif operation=='inspect_library_pages':
        if set(options)-{'category','max_pages'} or options.get('category','subscribed') not in {'subscribed','recent','updated'} or type(options.get('max_pages',3)) is not int or not 1<=options.get('max_pages',3)<=200:
            raise ArchiveError('Reading-list pagination accepts only a known category and page limit from 1 to 200.')
    elif operation=='configure_daily':
        from .daily import validate_configuration
        options=validate_configuration(options)
    elif operation=='inspect_library_filter':
        if set(options)-{'category','content_type'} or type(options.get('category','subscribed')) is not str or type(options.get('content_type','comics')) is not str or options.get('category','subscribed') not in {'subscribed','recent','updated'} or options.get('content_type','comics') not in {'all','comics','novels'}:
            raise ArchiveError('Library filter inspection accepts only a known category and content type.')
    elif operation=='inspect_free_access':
        if options:
            raise ArchiveError('Free Access inspection accepts no URLs, menu IDs or actions.')
    elif operation == 'inspect_transaction_history':
        if (set(options)-{'max_pages','resume'} or type(options.get('max_pages',3)) is not int
                or not 1 <= options.get('max_pages',3) <= 500 or type(options.get('resume',False)) is not bool):
            raise ArchiveError('History inspection accepts a page limit from 1 to 500 and an explicit resume boolean.')
    elif operation == 'inspect_library':
        if (set(options)-{'category','details'} or options.get('category','subscribed') not in {'subscribed','recent','updated'}
                or type(options.get('details',False)) is not bool):
            raise ArchiveError('Library inspection accepts a known category and a boolean details option.')
    elif operation=='inspect_series_metadata':
        if (set(options)!={'id'} or not isinstance(options['id'],str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',options['id'])):
            raise ArchiveError('Series metadata inspection requires an observed series ID/slug.')
    elif operation=='inspect_series':
        if (set(options)-{'id','strict_order'} or not isinstance(options.get('id'),str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',options['id'])
                or type(options.get('strict_order',True)) is not bool):
            raise ArchiveError('Series inspection accepts an observed ID/slug and an optional boolean ordering check.')
    else:
        raise ArchiveError('Only fixed source inspections can be submitted.')
    return _submit(session,operation,options)


def _submit(session,operation,options,*,task_id=None):
    root = Path(session) / "worker"
    if not (root / "requests").is_dir():
        raise ArchiveError("Start browser-worker from a normal terminal before submitting jobs.")
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
