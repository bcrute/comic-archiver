"""Local browser runner using a private filesystem queue, without a network server.

Start this from a normal terminal to allow a sandboxed assistant to inspect the
normal reader through files. Operations are fixed; arbitrary code/URLs are not
accepted. Automatic archives verify access; discovery never unlocks episodes.
"""
import json
import importlib
import re
import time
import traceback
import os
import secrets
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
WORKER_VERSION = 76
ADMISSION_WAIT_SECONDS = 10
from pathlib import Path
from . import sites
from .adapters.tapas import TapasAdapter
from .archive import atomic_json, directory
from .models import ArchiveError, identifier
from .errors import AuthenticationError


def private_json(path, value):
    if path.name == "status.json" and path.parent.name == "worker" and isinstance(value, dict):
        value = value | {"worker_version": WORKER_VERSION,
                         "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(path, value)
    path.chmod(0o600)


def discard_request(path):
    """Remove one consumed request file; a file that cannot be removed must not stop the worker.

    A request left behind is retried after the next restart, where its published response
    (when one exists) has the worker remove it again instead of repeating the task.
    """
    try:
        path.unlink()
    except OSError:
        print(f"Could not remove worker request {path.name}; it is retried after the next restart.", flush=True)


def execute(adapter, request, *, fast_session=None):
    try:
        return _execute(adapter, request, fast_session=fast_session)
    except ArchiveError:
        raise
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        local = [f for f in frames if 'comic_archive' in Path(f.filename).parts]
        locations = ", ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in (local[-4:] or frames[-2:]))
        failure = ArchiveError(f"Worker task failed ({type(error).__name__}; {locations}); credential details are suppressed.")
        from .run_receipts import carry_failure_receipt
        carry_failure_receipt(error, failure)
        raise failure from None


def _execute(adapter, request, *, fast_session=None):
    adapter.deadline = None
    operation = request.get("operation")
    args = request.get("args", {})
    if not isinstance(args, dict):
        raise ArchiveError("Worker task arguments must be an object.")
    if operation == 'preview_series':
        from .preflight import validate_options, preview_series
        validate_options(args)
        return preview_series(adapter, args['url'], Path('Library'), scope=args['scope'])
    if operation in {'cloud_test', 'cloud_cleanup'}:
        from .cloud import validate_test_action, smoke_test, cleanup_test
        validate_test_action(operation, args)
        if operation == 'cloud_test':
            options = {'episodes': 2}
            from .cloud import STREAM_PROVIDERS
            if args['provider'] in STREAM_PROVIDERS:
                options['stream'] = True
            return smoke_test(Path('Library'), args['provider'], **options)
        return cleanup_test(Path('Library'), args['test_id'])
    if operation == 'cloud_upload':
        if args:
            raise ArchiveError('Cloud upload accepts no paths or authentication options.')
        from .cloud import upload_pending
        return upload_pending(Path('Library'), limit=3)
    if operation == 'purge_cache':
        if args:
            raise ArchiveError('Cache purge accepts no paths or deletion options.')
        from .purge import purge_library
        return purge_library(Path('Library'), apply=True)
    if operation == 'audit_account':
        if args:
            raise ArchiveError('Account audits accept no paths or source options.')
        from .audit import audit_library
        report = Path('Library/.archive-state/account-audit.json')
        value = audit_library(Path('Library'), report)
        report.chmod(0o600)
        return {k: value[k] for k in ('audited_at', 'archive_integrity_passed',
                'verified_inventory_episode_count', 'portable_archive_count',
                'missing_recorded_accessible_episode_count', 'coverage_blockers',
                'series_scan_gap_count', 'account_inventory_complete')} | {
                'inventory_index_problem_count': len(value['inventory_index_problems']),
                'unindexed_archive_count': len(value['unindexed_archives'])}
    if operation=='check_session':
        if args:
            raise ArchiveError('Session checks accept no account IDs, URLs, paths or credentials.')
        from .jobs import check_session
        return check_session(adapter,Path('Library'))
    if operation=='configure_priority':
        from .expiry import queue_priority
        from .inventory import Inventory
        if set(args)!={'priority'}: raise ArchiveError('Queue order accepts only a priority choice.')
        value=queue_priority(args['priority'])
        with Inventory(Path('Library')) as inventory:
            inventory.set_queue_priority(value)
        return {'queue_priority':value}
    if operation=='configure_daily':
        from .daily import configure,start_scheduler,status,validate_configuration
        values=validate_configuration(args)
        zone=values.pop('timezone')
        session=getattr(adapter,'session',None)
        if not isinstance(session,Path):raise ArchiveError('The worker session directory is unavailable.')
        configure(session,zone=zone,**values)
        start_scheduler(session)
        return status(session)
    if operation=='inspect_episode_page_size':
        if set(args)-{'id','max_limit'} or not isinstance(args.get('id'),str) or not re.fullmatch(r'[1-9][0-9]{0,19}',args['id']) or type(args.get('max_limit',50)) is not int or args.get('max_limit',50) not in {20,50,100}:
            raise ArchiveError('Page-size inspection accepts an observed numeric series ID and bounded size.')
        from .inventory import Inventory
        with Inventory(Path('Library'),read_only=True) as inventory:
            if inventory.db.execute('SELECT 1 FROM series WHERE id=?',(args['id'],)).fetchone() is None:
                raise ArchiveError('Page-size inspection requires a series already observed in this library.')
        return adapter.inspect_episode_page_size(args['id'],args.get('max_limit',50))
    if operation in {'inspect_reader_identity','inspect_episode_access'}:
        if set(args)!={'url'} or not isinstance(args['url'],str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',args['url']):
            raise ArchiveError('Reader identity inspection requires an observed exact episode URL.')
        return adapter.inspect_reader_identity(args['url']) if operation=='inspect_reader_identity' else adapter.inspect_episode_access(args['url'])
    if operation=='inspect_library_pages':
        if set(args)-{'category','max_pages'}:raise ArchiveError('Only fixed reading-list pagination options are supported.')
        return adapter.inspect_library_pages(args.get('category','subscribed'),args.get('max_pages',3))
    if operation == "inspect_episode":
        if set(args)-{'url','details'} or not isinstance(args.get('url'),str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',args['url']):
            raise ArchiveError('Episode inspection requires an observed exact URL.')
        if "details" in args:
            if type(args["details"]) is not bool:
                raise ArchiveError("Inspection details must be an explicit boolean.")
            return adapter.inspect_episode(args["url"], interactive=False, details=args["details"])
        return adapter.inspect_episode(args["url"], interactive=False)
    if operation == "inspect_series":
        if set(args) - {"id", "strict_order"} or "id" not in args or type(args.get("strict_order", True)) is not bool:
            raise ArchiveError("Series inspection requires an ID/slug and an optional boolean ordering check.")
        return adapter.inspect_series(args["id"], strict_order=args["strict_order"]) if "strict_order" in args else adapter.inspect_series(args["id"])
    if operation=='inspect_series_metadata':
        if set(args)!={'id'}:
            raise ArchiveError('Series metadata inspection requires only an observed series ID/slug.')
        return adapter.inspect_series_metadata(args['id'])
    if operation=='enrich_series':
        if set(args)-{'id','refresh'} or 'id' not in args or type(args.get('refresh',False)) is not bool:
            raise ArchiveError('Series enrichment accepts an observed ID/slug and a refresh boolean.')
        from .metadata import enrich_series
        return enrich_series(adapter,args['id'],Path('Library'),args.get('refresh',False))
    if operation == "inspect_library":
        if args:
            if (set(args) - {"details", "category"} or type(args.get("details", False)) is not bool
                    or args.get("category", "subscribed") not in {"subscribed", "recent", "updated"}):
                raise ArchiveError("Library inspection accepts a boolean details option and a reading-list category.")
            return adapter.inspect_library(details=args.get("details", False), category=args.get("category", "subscribed"))
        return adapter.inspect_library()
    if operation=='inspect_free_access':
        if args:
            raise ArchiveError('Free Access inspection accepts no URLs, menu IDs or actions.')
        return adapter.inspect_free_access()
    if operation=='inspect_library_filter':
        if set(args)-{'category','content_type'} or type(args.get('category','subscribed')) is not str or type(args.get('content_type','comics')) is not str or args.get('category','subscribed') not in {'subscribed','recent','updated'} or args.get('content_type','comics') not in {'all','comics','novels'}:
            raise ArchiveError('Library filter inspection accepts only a known category and content type.')
        return adapter.inspect_library_filter(args.get('category','subscribed'),args.get('content_type','comics'))
    if operation == "inspect_account_navigation":
        if args:
            raise ArchiveError("Account navigation inspection accepts no URLs or actions.")
        return adapter.inspect_account_navigation()
    if operation == "inspect_transaction_history":
        if (set(args) - {'max_pages','resume'} or type(args.get('max_pages',3)) is not int
                or not 1 <= args.get('max_pages',3) <= 500 or type(args.get('resume',False)) is not bool):
            raise ArchiveError('History inspection accepts a page limit from 1 to 500.')
        options = {'resume':True} if args.get('resume') else {}
        session = getattr(adapter,'session',None)
        if isinstance(session,Path):
            def checkpoint(value):
                from .jobs import validated_history_links
                validated_history_links(value['content_links'])
                root = session/'worker'
                try:
                    status = json.loads((root/'status.json').read_text())
                    task = status.get('task_id')
                except (OSError,ValueError,AttributeError):
                    task = None
                if not isinstance(task,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',task):
                    task = None
                private_json(root/'history.json',{'schema_version':1,'source':'tapas',
                    'document_type':'tapas_transaction_discovery','account_inventory_complete':False,
                    'pages':value.get('pages',[]),'content_links':value['content_links'],
                    'blocker':value.get('blocker'),'private_cursor_persisted':False})
                private_json(root/'progress.json',{'task_id':task,'phase':'history',
                    'history_page_count':len(value.get('pages',[])),
                    'observed_history_series_count':len(value['content_links'])})
            options['on_page'] = checkpoint
        result = adapter.inspect_transaction_history(args.get('max_pages',3),**options)
        if 'on_page' in options:
            checkpoint(result)
        return result
    if operation in {"archive_discovered", "sync", "archive_watched_series", "archive_series_url"}:
        from .cloud import upload_pending
        upload_pending(Path('Library'), limit=10)
        allowed = {"limit", "max_pages", "max_bytes", "max_seconds", "free_only",'request_interval', 'download_workers'}
        if operation == 'archive_discovered':
            allowed.add('retry_episode_id')
            retry_id = args.get('retry_episode_id')
            if retry_id is not None and (not isinstance(retry_id, str)
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', retry_id) or args.get('limit') != 1):
                raise ArchiveError('Reader recovery probes require one numeric episode identity.')
        from .fast_import import FAST_OPERATIONS, FastImportAdapter, fast_options, validate_speed
        speed = validate_speed(args.get('speed', 'normal'))
        if operation in FAST_OPERATIONS:
            allowed.add('speed')
        elif speed != 'normal' or 'speed' in args:
            raise ArchiveError('Fast imports save known or chosen series; refresh the collection first.')
        if operation=='archive_series_url':
            allowed.add('url')
            from .selection import series_link
            series_link(args.get('url'))
        if operation=='archive_watched_series':
            allowed.add('id')
            if not isinstance(args.get('id'),str) or not re.fullmatch(r'[1-9][0-9]{0,19}',args['id']):
                raise ArchiveError('Selected-series jobs require an observed numeric source identity.')
        if operation == "sync":
            allowed.add('metadata_speed')
            allowed.add('scan_only')
            allowed.add("categories")
            allowed.add('refresh_lists')
            allowed.add('include_history')
            allowed.add('history_pages')
            allowed.add('history_resume')
            allowed.add('scan_limit')
        if set(args) - allowed or type(args.get("free_only", False)) is not bool:
            raise ArchiveError("Archive jobs accept only fixed limits, a free-only boolean and sync reading-list categories.")
        from .jobs import archive_discovered, archive_watched_series, archive_series_url, sync
        options = {key: value for key, value in args.items() if key != "categories"}
        options.pop('speed', None)
        interval=options.pop('request_interval',getattr(adapter,'interval',1.0))
        from .downloads import validate_request_interval
        validate_request_interval(interval)
        from .downloads import validate_download_workers
        validate_download_workers(options.get('download_workers', 1))
        # The faster engine is Tapas's own; another site's reader saves at its normal pace.
        other_site = isinstance(getattr(adapter, 'site', None), sites.Site) and adapter.site != sites.DEFAULT
        if speed == 'fast' and not other_site:
            if fast_session is not None:
                with fast_session.batch() as fast:
                    return _archive_operation(fast, operation, fast_options(options))
            session = getattr(adapter, 'session', None)
            with FastImportAdapter(session, getattr(adapter, 'preferred_browser', 'auto'),
                                   headless=getattr(adapter, 'headless', False)) as fast:
                return _archive_operation(fast, operation, fast_options(options))
        previous=getattr(adapter,'interval',1.0)
        adapter.interval=interval
        try:
            if operation=='archive_series_url':
                return archive_series_url(adapter,options.pop('url'),Path('Library'),**options)
            if operation=='archive_watched_series':
                return archive_watched_series(adapter,options.pop('id'),Path('Library'),**options)
            if operation == "sync":
                return sync(adapter, Path("Library"), _discovery_report(), args.get("categories"), **options)
            return archive_discovered(adapter, Path("Library"), _discovery_report(), **options)
        finally:
            adapter.interval=previous
    if operation == "diagnose_headless":
        if set(args) != {"url"} or not isinstance(args.get('url'),str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',args['url']):
            raise ArchiveError("Headless diagnostic requires only an exact episode URL.")
        return adapter.diagnose_headless(args["url"])
    raise ArchiveError("Worker accepts only fixed episode/series inspection and episode archive tasks.")


def _discovery_report():
    path = Path('Library/discovery.json')
    # A new user's inventory does not require a legacy discovery report.
    # Existing and unsafe paths still go through the importer validation.
    return path if path.exists() or path.is_symlink() else None


def _archive_operation(adapter, operation, options):
    from .jobs import archive_discovered, archive_watched_series, archive_series_url
    if operation == 'archive_series_url':
        return archive_series_url(adapter, options.pop('url'), Path('Library'), **options)
    if operation == 'archive_watched_series':
        return archive_watched_series(adapter, options.pop('id'), Path('Library'), **options)
    return archive_discovered(adapter, Path('Library'), _discovery_report(), **options)


@contextmanager
def admission(session):
    """The admission lock, waited for: a stop request holds it for a moment, and that must not fail the task."""
    from .errors import LibraryBusy
    from .task_cancellation import admission_lock
    deadline=time.monotonic()+ADMISSION_WAIT_SECONDS
    while True:
        try:
            lock=admission_lock(session)
            lock.__enter__()
            break
        except LibraryBusy:
            if time.monotonic()>=deadline:raise
            time.sleep(.05)
    try:
        yield
    finally:
        lock.__exit__(None,None,None)


def reader(site):
    """The class that reads one site's pages for the downloader."""
    if site.key == sites.DEFAULT.key:
        return TapasAdapter
    if site.key == 'webtoon':
        from .adapters.webtoon import WebtoonAdapter
        return WebtoonAdapter
    if site.key == 'tappytoon':
        from .adapters.tappytoon import TappytoonAdapter
        return TappytoonAdapter
    raise ArchiveError(f'Saving from {site.name} is not available yet.')


def run_worker(session, preferred="firefox", headless=False, interval=1.0,*,reload_adapter=True):
    from .locking import LibraryLock
    # One process consumes each private queue; separate user workers remain independent.
    root=directory(Path(session)/'worker')
    with LibraryLock(root/'.runtime'):
        return _run_worker(session,preferred,headless,interval,reload_adapter=reload_adapter)


def _run_worker(session, preferred="firefox", headless=False, interval=1.0,*,reload_adapter=True):
    if type(reload_adapter) is not bool:
        raise ArchiveError('Worker code reload mode must be an explicit boolean.')
    root = directory(session / "worker")
    root.chmod(0o700)
    requests = directory(root / "requests")
    responses = directory(root / "responses")
    requests.chmod(0o700)
    responses.chmod(0o700)
    status = root / "status.json"
    worker_id = secrets.token_hex(16)
    worker_fields = {"worker_version": WORKER_VERSION, "worker_id": worker_id,
                     "headless": headless, "pid": os.getpid()}
    startup_complete = False
    last_error = None
    def publish(value):
        private_json(status, worker_fields | value)
    from .background import drain_requested
    try:
        publish({"state": "starting"})
        from .fast_import import FastWorkerSession
        site = sites.of_session(session)
        # The faster second engine is Tapas's own; other sites read with the one adapter.
        with reader(site)(session, preferred, headless=headless, interval=interval) as adapter, \
                (FastWorkerSession(session, preferred, headless=headless) if site == sites.DEFAULT else nullcontext()) as fast_session:
            publish({"state": "ready"})
            startup_complete = True
            print(f"Browser worker v{WORKER_VERSION} ready. Stop with stop-worker or Ctrl+C.", flush=True)
            print('Task logging is enabled. '+('Adapter reload is enabled.' if reload_adapter else 'Loaded adapter code is retained until restart.'), flush=True)
            print("Archives check account access and capture HTML page manifests automatically.", flush=True)
            print("Comic images may show broken icons in this browser: originals download separately into compressed archives. No scrolling is needed for automatic imports.", flush=True)
            from .daily import start_scheduler
            start_scheduler(session)
            while True:
                if drain_requested(root, worker_id):
                    return
                from .completion import worker_tick
                from .account_export import worker_tick as account_tick
                account_tick(session)
                worker_tick(session)
                for file in sorted(requests.glob("*.json")):
                    if file.is_symlink():
                        continue
                    # One wrongly named request must not stop the worker or the
                    # tasks behind it; a name is not task content, so it is dropped.
                    try:
                        task_id = identifier(file.stem)
                    except ArchiveError:
                        print(f"Ignoring worker request with an invalid name: {file.name}", flush=True)
                        discard_request(file)
                        continue
                    response = responses / f"{task_id}.json"
                    if response.exists():
                        discard_request(file)
                        continue
                    print(f"Starting task {task_id}", flush=True)
                    try:
                        if file.stat().st_size > 16384:
                            raise ArchiveError("Worker request is too large.")
                        # Read the bytes once and check their actual length: the file may
                        # change between the size check above and the read.
                        data = file.read_bytes()
                        if len(data) > 16384:
                            raise ArchiveError("Worker request is too large.")
                        request = json.loads(data)
                        if not isinstance(request, dict):
                            raise ArchiveError("Worker request must be an object.")
                        from .task_cancellation import queued_stop_requested, cancelled_result
                        with admission(session):
                            if drain_requested(root, worker_id):
                                return
                            stopped=(bool(re.fullmatch(r'job-[a-f0-9]{24}',task_id))
                                     and queued_stop_requested(session,task_id))
                            publish({"state": "busy", "task_id": task_id})
                        # The repository is editable during prototype debugging.
                        # Reload only our fixed adapter implementation between
                        # tasks; no module/code path comes from a queue request.
                        if not stopped and reload_adapter and type(adapter).__module__ == "comic_archive.adapters.tapas":
                            module = importlib.import_module("comic_archive.adapters.tapas")
                            adapter.__class__ = importlib.reload(module).TapasAdapter
                        result = (cancelled_result(Path('Library'),request.get('operation'))
                                  if stopped else execute(adapter, request, fast_session=fast_session))
                        value = {"task_id": task_id, "status": "ok", "result": result}
                    except ArchiveError as error:
                        value = {"task_id": task_id, "status": "error", "error": str(error),
                                 'error_code':'login_required' if isinstance(error,AuthenticationError) else 'job_failed'}
                        from .cloud_errors import CloudTransferError, safe_code
                        if isinstance(error, CloudTransferError):
                            value['error_code'] = safe_code(error)
                        from .run_receipts import failure_result
                        partial = failure_result(error)
                        if partial is not None:
                            value['result'] = partial
                            if partial.get('pause_reason') == 'source_refused':
                                value['error_code'] = 'source_refused'
                    except Exception:
                        value = {"task_id": task_id, "status": "error",
                                 "error": "Worker task failed; raw exceptions suppressed to protect credentials."}
                    private_json(response, value)
                    if value["status"] == "error":
                        print(f"Task {task_id} failed: {value['error']}", flush=True)
                    else:
                        print(f"Task {task_id} completed.", flush=True)
                    discard_request(file)
                    publish({"state": "ready"})
                time.sleep(0.5)
    except ArchiveError:
        if not startup_complete:
            last_error = 'startup_failed'
        raise
    finally:
        from .daily import stop_scheduler
        stop_scheduler(session)
        publish({"state": "stopped", **({"last_error_code":last_error} if last_error else {})})
        if drain_requested(root, worker_id):
            (root / 'drain.json').unlink(missing_ok=True)
