"""The desktop app's background downloader, fed through a private filesystem queue.

The app starts it as `python -m comic_archive browser-worker`. Operations are
fixed; arbitrary code/URLs are not accepted. Automatic archives verify access;
discovery never unlocks episodes.
"""
import json
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


def _execute(adapter, request, *, fast_session=None, upload_first=True):
    adapter.deadline = None
    operation = request.get("operation")
    args = request.get("args", {})
    if not isinstance(args, dict):
        raise ArchiveError("Worker task arguments must be an object.")
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
    if operation in {"archive_discovered", "sync"}:
        if upload_first:
            from .cloud import upload_pending
            from .network import NetworkPolicyHold
            from .cloud_errors import safe_code
            try:
                upload_pending(Path('Library'), limit=10)
                held = None
            except NetworkPolicyHold:
                # The job meets the same connection rule and pauses for it with a receipt of its own.
                return _execute(adapter, request, fast_session=fast_session, upload_first=False)
            except (ArchiveError, OSError) as error:
                held = safe_code(error)
            if held:
                # A stuck upload, an expired sign-in or a full cloud must not stop saving here; the result says why.
                result = _execute(adapter, request, fast_session=fast_session, upload_first=False)
                return result | {'cloud_upload_error_code': held} if isinstance(result, dict) else result
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
        from .jobs import archive_discovered, sync
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
                    return archive_discovered(fast, Path('Library'), _discovery_report(), **fast_options(options))
            session = getattr(adapter, 'session', None)
            with FastImportAdapter(session, getattr(adapter, 'preferred_browser', 'auto'),
                                   headless=getattr(adapter, 'headless', False)) as fast:
                return archive_discovered(fast, Path('Library'), _discovery_report(), **fast_options(options))
        previous=getattr(adapter,'interval',1.0)
        adapter.interval=interval
        try:
            if operation == "sync":
                return sync(adapter, Path("Library"), _discovery_report(), args.get("categories"), **options)
            return archive_discovered(adapter, Path("Library"), _discovery_report(), **options)
        finally:
            adapter.interval=previous
    raise ArchiveError("Worker accepts only fixed archive, account audit and cloud tasks.")


def _discovery_report():
    path = Path('Library/discovery.json')
    # A new user's inventory does not require a legacy discovery report.
    # Existing and unsafe paths still go through the importer validation.
    return path if path.exists() or path.is_symlink() else None


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


def run_worker(session, preferred="firefox", headless=False, interval=1.0, adapter_factory=None):
    from .locking import LibraryLock
    # One process consumes each private queue; separate user workers remain independent.
    root=directory(Path(session)/'worker')
    with LibraryLock(root/'.runtime'):
        return _run_worker(session,preferred,headless,interval,adapter_factory)


def _run_worker(session, preferred="firefox", headless=False, interval=1.0, adapter_factory=None):
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
        site = sites.of_session(session)
        if adapter_factory is not None:
            # The embedded host supplies one transport-backed adapter; no browser engine and
            # no faster second session exist there, and the factory owns every credential rule.
            adapter_source = nullcontext(adapter_factory())
            fast_source = nullcontext()
        else:
            from .fast_import import FastWorkerSession
            # The faster second engine is Tapas's own; other sites read with the one adapter.
            adapter_source = reader(site)(session, preferred, headless=headless, interval=interval)
            fast_source = (FastWorkerSession(session, preferred, headless=headless)
                           if site == sites.DEFAULT else nullcontext())
        with adapter_source as adapter, fast_source as fast_session:
            publish({"state": "ready"})
            startup_complete = True
            print(f"Browser worker v{WORKER_VERSION} ready. Stop with stop-worker or Ctrl+C.", flush=True)
            print('Task logging is enabled.', flush=True)
            print("Archives check account access and capture HTML page manifests automatically.", flush=True)
            print("Comic images may show broken icons in this browser: originals download separately into compressed archives. No scrolling is needed for automatic imports.", flush=True)
            failing = set()
            while True:
                if drain_requested(root, worker_id):
                    return
                from .completion import worker_tick
                from .account_export import worker_tick as account_tick
                for name, tick in (('account export', account_tick), ('backlog', worker_tick)):
                    # Each controller holds itself on a failure it expects; anything else must not stop the queue.
                    try:
                        tick(session)
                        failing.discard(name)
                    except Exception as error:
                        if name not in failing:
                            failing.add(name)
                            print(f"The {name} controller failed ({type(error).__name__}); queued tasks continue.", flush=True)
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
                    try:
                        private_json(response, value)
                    except (OSError, TypeError, ValueError):
                        # A result that cannot be saved still ends its task, so it is not run again.
                        value = {"task_id": task_id, "status": "error", 'error_code': 'job_failed',
                                 "error": "Worker could not save the task result."}
                        try:
                            private_json(response, value)
                        except OSError:
                            pass
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
        publish({"state": "stopped", **({"last_error_code":last_error} if last_error else {})})
        if drain_requested(root, worker_id):
            (root / 'drain.json').unlink(missing_ok=True)
