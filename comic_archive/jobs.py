"""Reusable jobs for CLI, a private website or a worker; no purchase actions."""
import math
import shutil
import time
import traceback
import re
from pathlib import Path
from contextlib import nullcontext
from urllib.parse import urlsplit
from . import sites
from .publication import PublicationStore as Store
from .archive import atomic_json, verify_episode
from .errors import AccessDenied, AssetReadUnavailable, AuthenticationError, MetadataError, NetworkUnavailable, RunLimit, UnsupportedContent
from .inventory import Inventory, UNSUPPORTED_REASONS
from .models import ArchiveError
from .cancellation import RunCancellation, RunCancelled
from .adapters.pagination import EpisodeCountMismatch


JOBS_VERSION = 57
MAX_CONSECUTIVE_ASSET_FAILURES = 3
MAX_DEFERRED_ASSET_FAILURES = 5


def retain_partial_series(inventory,error):
    """Retain returned candidates while preserving an explicit count gap."""
    partial=getattr(error,'partial_inspection',None)
    if partial is None:return False
    if (not isinstance(partial,dict) or partial.get('authenticated') is not True
            or partial.get('complete') is not False or partial.get('source_series_id')!=error.series_id
            or not isinstance(partial.get('episodes'),list) or len(partial['episodes'])!=error.observed_count
            or type(partial.get('episode_count')) is not int or partial['episode_count']!=error.observed_count
            or not isinstance(partial.get('title'),str)):
        raise MetadataError('The partial episode list does not match its observed series/count gap.')
    inventory.observe(error.series_id,partial['title'],partial['episodes'],'partial_series_list')
    return True


def check_session(adapter,output=Path('Library')):
    """One normal identity check, with no account data in the result."""
    from .network import bind_adapter
    bind_adapter(adapter, output)
    adapter.deadline=time.monotonic()+60
    identity=adapter.account_identity()
    if not isinstance(identity,str) or not identity:
        raise AuthenticationError('Tapas authentication is unverified; log in before checking the session.')
    path=Path(output)/'.archive-state/inventory.sqlite3'
    if any(parent.is_symlink() for parent in (path,*path.parents)):
        raise ArchiveError('Session account binding storage is unavailable.')
    binding='unbound'
    if path.is_file():
        with Inventory(output,read_only=True) as inventory:
            binding=inventory.account_binding_state(identity)
    return {'authenticated':True,'account_binding':binding,'reader_or_comic_pages_requested':False}


def job_progress(root,run_id,**fields):
    path=Path(root)/'.archive-state/progress.json'
    atomic_json(path,{'run_id':run_id,'status':'running','cancellation_supported':True,**fields})
    path.chmod(0o600)


def validated_history_links(links):
    if (not isinstance(links,list) or len(links)>10000 or any(not isinstance(url,str) or
            not re.fullmatch(r'https://tapas\.io/(?:series/[A-Za-z0-9_-]{1,100}(?:/info)?|episode/[1-9][0-9]{0,19})',url)
            for url in links)):
        raise MetadataError('History returned an unexpected public content link; no snapshot will be written.')
    return links


class DownloadBudget:
    def __init__(self, adapter, max_pages=None, max_bytes=None, max_seconds=None):
        for value in (max_pages, max_bytes):
            if value is not None and (type(value) is not int or value < 1):
                raise ArchiveError("Page and byte limits must be positive integers.")
        if max_seconds is not None and (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ArchiveError("Time limit must be a finite positive number.")
        self.adapter = adapter
        self.max_pages, self.max_bytes, self.max_seconds = max_pages, max_bytes, max_seconds
        self.started = time.monotonic()
        self.pages = self.bytes = self.covers = self.chapters = 0
        self.cancellation=None
        self.access_expires_at=None
        self.reserved_pages = self.reserved_bytes = 0
        from .downloads import adapter_profile
        self.download_profile = adapter_profile(adapter)

    def check(self):
        if self.cancellation is not None:
            self.cancellation.check()
        if self.max_seconds is not None and time.monotonic() - self.started >= self.max_seconds:
            raise RunLimit("The job time budget is exhausted; its queue is retained.")

    def access_check(self):
        if self.cancellation is not None: self.cancellation.check()
        from .expiry import check_expiration
        check_expiration(self.access_expires_at)

    def retrieve(self, page):
        self.check()
        self.access_check()
        if self.max_pages is not None and self.pages >= self.max_pages:
            raise RunLimit("The job page budget is exhausted; completed pages remain cached.")
        remaining = self.max_bytes - self.bytes if self.max_bytes is not None else None
        if remaining is not None and remaining < 1:
            raise RunLimit("The job byte budget is exhausted; its queue is retained.")
        data = self.adapter.retrieve(page, max_bytes=remaining) if remaining is not None else self.adapter.retrieve(page)
        self.pages += 1
        self.bytes += len(data)
        return data

    def refresh(self, episode):
        self.check()
        return self.adapter.refresh(episode)

    def consume_novel(self, episode):
        """A text chapter consumes one transfer unit and its preserved UTF-8 bytes."""
        self.check()
        size = len(episode.text_html.encode('utf-8')) + sum(map(len, episode.illustrations))
        units = 1 + len(episode.illustrations)
        if ((self.max_pages is not None and self.pages + units > self.max_pages)
                or (self.max_bytes is not None and self.bytes + size > self.max_bytes)):
            raise RunLimit('The novel chapter exceeds the remaining transfer budget; its queue is retained.')
        self.pages += units
        self.chapters += 1
        self.bytes += size

    def asset_request(self, page):
        """Reserve worst-case bytes before submission; SQLite/checks stay here."""
        from .downloads import AssetDownload, MAX_PAGE_BYTES
        self.check()
        self.access_check()
        prepare = getattr(self.adapter, 'asset_request', None)
        if not callable(prepare):
            return None
        if self.max_pages is not None and self.pages + self.reserved_pages >= self.max_pages:
            return None
        remaining = self.max_bytes - self.bytes - self.reserved_bytes if self.max_bytes is not None else MAX_PAGE_BYTES
        if remaining < 1 or (self.reserved_pages and remaining < MAX_PAGE_BYTES):
            return None
        allowance = min(MAX_PAGE_BYTES, remaining)
        request = prepare(page, max_bytes=allowance)
        if request is None:
            return None
        self.reserved_pages += 1
        self.reserved_bytes += allowance

        def accept(data):
            request.finish(data)
            self.pages += 1
            self.bytes += len(data)

        def release():
            request.cancel()
            self.reserved_pages -= 1
            self.reserved_bytes -= allowance

        return AssetDownload(request.fetch, accept, release)

    def retrieve_cover(self,series):
        self.check()
        remaining=self.max_bytes-self.bytes if self.max_bytes is not None else None
        if remaining is not None and remaining<1:
            raise RunLimit('The download byte budget is exhausted; completed archives are retained.')
        data=self.adapter.retrieve_cover(series,max_bytes=remaining)
        self.bytes+=len(data)
        self.covers+=1
        return data


def archive_discovered(adapter, output=Path("Library"), report=None, limit=None,
                       max_pages=None, max_bytes=None, max_seconds=None, free_only=False,
                       min_free_bytes=256 * 1024 * 1024, _refresh=None, _series_id=None,
                       download_workers=1, _selected_only=False, _pipeline=False, _scan_only=False,
                       retry_episode_id=None):
    from .downloads import adapter_profile, validate_download_workers
    from .network import NetworkPolicyHold, bind_adapter
    bind_adapter(adapter, output)
    profile = adapter_profile(adapter)
    if type(_scan_only) is not bool or (_scan_only and (_refresh is None or _selected_only)):
        raise ArchiveError('Scan-only jobs require an explicit inventory refresh.')
    validate_download_workers(download_workers, profile)
    if type(_pipeline) is not bool:
        raise ArchiveError('Metadata lookahead must be an explicit boolean.')
    if retry_episode_id is not None and (not isinstance(retry_episode_id, str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', retry_episode_id)
            or limit != 1 or _refresh is not None or _series_id is not None or _scan_only):
        raise ArchiveError('Reader recovery probes select one observed pending episode.')
    if profile == 'fast' and not free_only:
        _pipeline = True
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ArchiveError("Episode limit must be a positive integer.")
    if type(free_only) is not bool or type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ArchiveError("Job filtering and disk reserve options are invalid.")
    if _series_id is not None and (not isinstance(_series_id,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',_series_id)):
        raise ArchiveError('A selected series requires its observed numeric source identity.')
    if type(_selected_only) is not bool or (_selected_only and _refresh is None):
        raise ArchiveError('A selected import needs an explicit series refresh.')
    if (type(_pipeline) is not bool or (_pipeline and (free_only
            or not callable(getattr(adapter, 'prefetch_episode', None))))):
        raise ArchiveError('Metadata lookahead requires a compatible browser adapter.')
    budget = DownloadBudget(adapter, max_pages, max_bytes, max_seconds)
    from .transfer_metrics import TransferMetrics, NETWORK_FAILURE_CODES
    from .transport import Transport
    metrics = TransferMetrics()
    previous_asset_metrics = getattr(adapter, 'asset_metrics', None)
    transport = getattr(adapter, 'transport', None)
    assets = getattr(transport, 'assets', transport)
    previous_transport_metrics = assets.metrics if isinstance(assets, Transport) else None
    adapter.deadline = budget.started + max_seconds if max_seconds is not None else None
    output = Path(output).absolute()
    with Inventory(output) as inventory:
        run_id = inventory.start_run()
        budget.cancellation=RunCancellation(output,run_id)
        previous_cancel_check=getattr(adapter,'cancel_check',None)
        adapter.cancel_check=budget.access_check
        adapter.asset_metrics=metrics
        if isinstance(assets,Transport):assets.metrics=metrics
        summary = {"run_id": run_id, "status": "running", "archived_episode_ids": [],
                   "unavailable_episode_ids": [], "unsupported_episode_ids": [],
                   "not_free_episode_ids": [],
                   'deferred_asset_episode_ids':[],
                   'enriched_series_ids':[],'metadata_unavailable_series':[],
                   "downloaded_pages": 0, "downloaded_bytes": 0, "inventory_complete": False,
                   'scan_elapsed_seconds':0.0, 'download_workers':download_workers}
        if retry_episode_id is not None:
            summary.update(reader_probe_episode_id=retry_episode_id, reader_probe_succeeded=False)
        current = None
        metadata_seen=set()
        consecutive_asset_failures = 0

        def pending():
            if _selected_only and _series_id is None:
                return []
            rows = inventory.pending(free_only)
            return rows if _series_id is None else [row for row in rows if row['series_id']==_series_id]

        if _series_id is not None:
            summary['source_series_id'] = _series_id

        def enrich_metadata(series_key,store):
            budget.check()
            metadata_seen.add(series_key)
            job_progress(output,run_id,phase='metadata',series_id=series_key,
                         completed=len(summary['archived_episode_ids']),downloaded_pages=budget.pages,
                         downloaded_bytes=budget.bytes)
            try:
                from .metadata import enrich_with_store
                enrich_with_store(adapter,series_key,store,cover_adapter=budget)
                summary['enriched_series_ids'].append(series_key)
            except (AuthenticationError,NetworkUnavailable,RunLimit,RunCancelled):
                raise
            except ArchiveError as error:
                summary['metadata_unavailable_series'].append({'series_id':series_key,'code':type(error).__name__})
            budget.check()

        def checkpoint(status):
            remaining = pending()
            _, asset_wait = inventory.archive_selection(remaining)
            summary.pop('asset_retry_after_seconds', None)
            if status == 'limited' and asset_wait is not None:
                summary['asset_retry_after_seconds'] = asset_wait
            summary['asset_metrics']=metrics.snapshot()
            summary.update(status=status, downloaded_pages=budget.pages, downloaded_bytes=budget.bytes,
                           downloaded_chapters=budget.chapters,
                           downloaded_covers=budget.covers,
                           metadata_unavailable_series_count=len(summary['metadata_unavailable_series']),
                           pending_episode_count=len(remaining),
                           elapsed_seconds=round(time.monotonic() - budget.started, 3),
                           inventory=inventory.status())
            # Omit the current run's unfinished embedded summary to avoid recursion.
            summary["inventory"].pop("last_run", None)
            inventory.finish_run(run_id, status, summary)
            atomic_json(output / ".archive-state/last-run.json", summary)
            (output / ".archive-state/last-run.json").chmod(0o600)
            atomic_json(output / ".archive-state/progress.json", {
                "run_id": run_id, "status": status, "completed": len(summary["archived_episode_ids"]),
                "downloaded_pages": budget.pages, "downloaded_chapters": budget.chapters, "downloaded_bytes": budget.bytes})
            (output / ".archive-state/progress.json").chmod(0o600)

        boundary = getattr(adapter, 'batch_boundary', None) if _pipeline else None
        try:
            with boundary() if callable(boundary) else nullcontext():
                job_progress(output,run_id,phase='authentication')
                identity = adapter.account_identity()
                inventory.bind_account(identity)
                adapter.expected_account_id = identity
                if getattr(adapter, 'supports_novels', False) is True:
                    inventory.enable_novels()
                if getattr(adapter,'supports_expiring_access',False) is True:
                    inventory.enable_temporary_access()
                if report is not None:
                    inventory.ingest_report(report)
                if _refresh is not None:
                    scan_started=time.monotonic()
                    try:refresh_result = _refresh(inventory, budget, run_id)
                    finally:summary['scan_elapsed_seconds']=round(max(0,time.monotonic()-scan_started),3)
                    if refresh_result is not None:
                        summary['refresh'] = refresh_result
                    if _selected_only:
                        selected = refresh_result.get('source_series_id') if isinstance(refresh_result, dict) else None
                        if (not isinstance(selected, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', selected)
                                or not any(s['id'] == selected for s in inventory.watched_series())):
                            raise MetadataError('The chosen series identity was not verified.')
                        _series_id = selected
                        summary['source_series_id'] = selected
                inventory.reconcile()
                if _scan_only:
                    summary['scan_only'] = True
                    checkpoint('limited' if refresh_result.get('state') == 'limited' else 'finished')
                    return summary
                candidates = pending()
                if free_only:
                    summary['priority_policy']='recent_free'
                    summary['recent_free_candidate_count']=sum(row.get('recent_free_candidate')==1 for row in candidates)
                else:
                    summary['priority_policy']='unlocked_first'
                selected, _ = inventory.archive_selection(candidates, limit)
                if retry_episode_id is not None:
                    # Recheck the exact refused episode, without inventing an
                    # identity or letting healthy work masquerade as recovery.
                    selected = [row for row in candidates if row['id'] == retry_episode_id]
                if free_only:
                    summary['recent_free_selected_count']=sum(row.get('recent_free_candidate')==1 for row in selected)
                with_store = Store(output, min_free_bytes=min_free_bytes,
                                   cache_journal='wal' if profile == 'fast' else None)
                try:
                    for selected_index, current in enumerate(selected):
                        budget.access_expires_at=None
                        budget.check()
                        if shutil.disk_usage(output).free < min_free_bytes:
                            raise RunLimit("The configured free disk reserve was reached; its queue is retained.")
                        key = current["id"]
                        archived_series=None
                        inventory.mark(key, "downloading", attempted=True)
                        print(f"Archiving episode {key}; {len(summary['archived_episode_ids'])} episodes completed in this run.", flush=True)
                        try:
                            if free_only:
                                from .expiry import daily_candidate
                                state = adapter.episode_access(adapter.episode_url(key))
                                access = state['access_state']; expires_at=state.get('access_expires_at')
                                inventory.record_access(key, access, expires_at=expires_at)
                                if not daily_candidate(access,expires_at):
                                    summary['not_free_episode_ids'].append(key)
                                    current = None
                                    continue
                            manifest = adapter.capture_automatic(adapter.episode_url(key))
                            if (manifest.series.platform_id != current["series_id"] or len(manifest.episodes) != 1
                                    or manifest.episodes[0].id != key):
                                raise MetadataError("Captured episode does not match the queued source identities.")
                            episode=manifest.episodes[0]
                            if free_only and not daily_candidate(episode.access,episode.access_expires_at):
                                inventory.record_access(key, episode.access, expires_at=episode.access_expires_at)
                                summary["not_free_episode_ids"].append(key)
                                current = None
                                continue
                            inventory.record_access(key, episode.access, expires_at=episode.access_expires_at)
                            budget.access_expires_at=episode.access_expires_at
                            if retry_episode_id is not None:
                                summary['reader_probe_succeeded'] = True
                            inventory.mark(key, 'downloading')
                            last_progress = [None]
                            def progress(line):
                                now = time.monotonic()
                                force = line.endswith(('archive and JSON saved', 'already archived and verified'))
                                if not force and last_progress[0] is not None and now - last_progress[0] < 0.25:
                                    return
                                last_progress[0] = now
                                print(line, flush=True)
                                atomic_json(output / ".archive-state/progress.json", {
                                    "run_id": run_id, "status": "running", "episode_id": key,
                                    "message": line, "completed": len(summary["archived_episode_ids"]),
                                    "downloaded_pages": budget.pages, "downloaded_bytes": budget.bytes,
                                    'downloaded_chapters':budget.chapters,
                                    'cancellation_supported':True})
                                (output / ".archive-state/progress.json").chmod(0o600)
                            if _pipeline and selected_index + 1 < len(selected):
                                units = (len(manifest.episodes[0].pages) if manifest.episodes[0].content_type == 'comic'
                                         else 1 + len(manifest.episodes[0].illustrations))
                                if budget.max_pages is None or budget.pages + units < budget.max_pages:
                                    budget.check()
                                    adapter.prefetch_episode(adapter.episode_url(selected[selected_index + 1]['id']))
                            with_store.archive(manifest, budget, progress=progress, download_workers=download_workers)
                            path = with_store.episode_path(manifest.series, manifest.episodes[0])
                            if verify_episode(path) and not inventory.cloud_archive_matches(path, {'id':key, 'series_id':current['series_id']}):
                                raise ArchiveError("Newly published archive failed verification; repair is required.")
                            inventory.mark(key, "archived", path=path)
                            summary["archived_episode_ids"].append(key)
                            archived_series=manifest.series
                            consecutive_asset_failures = 0
                        except AssetReadUnavailable as error:
                            # Only a typed CDN read failure is episode-local. Auth,
                            # metadata, HTTP refusals and rate limits remain holds.
                            if getattr(error,'network_failure_code',None) not in {'read_timeout','read_error'}:
                                raise
                            inventory.mark(key,'queued','asset_read_unavailable')
                            summary['deferred_asset_episode_ids'].append(key)
                            consecutive_asset_failures += 1
                            print(f'Episode {key}: image read failed after retries; cached pages retained for a bounded retry after cooldown.',flush=True)
                            if (consecutive_asset_failures >= MAX_CONSECUTIVE_ASSET_FAILURES
                                    or len(summary['deferred_asset_episode_ids']) >= MAX_DEFERRED_ASSET_FAILURES):
                                raise
                        except AccessDenied:
                            inventory.record_access(key, 'unavailable_or_unverified')
                            inventory.mark(key, "unavailable", "access_denied")
                            summary["unavailable_episode_ids"].append(key)
                        except UnsupportedContent as error:
                            code = getattr(error,'code',None)
                            if not isinstance(code,str) or code not in UNSUPPORTED_REASONS:
                                code = 'content_unsupported'
                            inventory.mark(key, "unsupported", code)
                            summary["unsupported_episode_ids"].append(key)
                            if code=='reader_manifest_incomplete':
                                current=None
                                raise MetadataError('Reader manifest is unavailable; this run paused for authorized browser inspection.') from None
                        current = None
                        budget.access_expires_at=None
                        if archived_series is not None and getattr(adapter,'supports_series_metadata',False) is True:
                            series_key=archived_series.platform_id or archived_series.id
                            if series_key not in metadata_seen:
                                if not with_store.metadata_ready(archived_series):
                                    enrich_metadata(series_key,with_store)
                                else:
                                    metadata_seen.add(series_key)
                        # Preserve a readable live checkpoint without finishing the run.
                        atomic_json(output / ".archive-state/progress.json", {
                            "run_id": run_id, "status": "running", "completed": len(summary["archived_episode_ids"]),
                            "downloaded_pages": budget.pages, "downloaded_bytes": budget.bytes,
                            'downloaded_chapters':budget.chapters,
                            'cancellation_supported':True})
                    # An explicit completed-series retry also repairs optional details
                    # and covers, under this run's existing byte/time/cancel budgets.
                    if (not selected and _series_id is not None
                            and getattr(adapter,'supports_series_metadata',False) is True
                            and inventory.db.execute('SELECT 1 FROM episodes WHERE series_id=? AND status=? LIMIT 1',
                                                     (_series_id,'archived')).fetchone()):
                        enrich_metadata(_series_id,with_store)
                    remaining = len(pending())
                    summary["pending_episode_count"] = remaining
                    checkpoint("finished" if remaining == 0 else "limited")
                    return summary
                finally:
                    with_store.close()
        except RunCancelled:
            if current is not None:
                inventory.mark(current['id'],'queued','cancelled')
            checkpoint('cancelled')
            return summary
        except RunLimit:
            if current is not None:
                inventory.mark(current["id"], "queued", "run_limit")
            checkpoint("limited")
            return summary
        except AuthenticationError as error:
            if current is not None:
                inventory.mark(current["id"], "queued", "login_required")
            checkpoint("login_required")
            from .run_receipts import retain_failure_receipt
            retain_failure_receipt(error, summary)
            raise
        except NetworkUnavailable as error:
            if current is not None:
                local_asset_failure=(isinstance(error,AssetReadUnavailable)
                                     and getattr(error,'network_failure_code',None) in {'read_timeout','read_error'})
                inventory.mark(current['id'],'queued','asset_read_unavailable' if local_asset_failure else 'network_unavailable')
            summary['pause_reason']='network_unavailable'
            from .retry_after import MAX_RETRY_AFTER
            delay = getattr(error, 'retry_after_seconds', None)
            if type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER:
                summary['retry_after_seconds'] = delay
            failure_code=getattr(error,'network_failure_code',None)
            if isinstance(failure_code,str) and failure_code in NETWORK_FAILURE_CODES:
                summary['network_failure_code']=failure_code
            failure=getattr(error,'browser_failure_code',None)
            if isinstance(failure,str) and failure in {'timeout','browser_error'}:summary['browser_failure_code']=failure
            source_status=getattr(error,'source_http_status',None)
            if type(source_status) is int and source_status in {502,503,504}:
                summary['source_http_status']=source_status
            checkpoint('paused')
            return summary
        except NetworkPolicyHold:
            summary['pause_reason'] = 'network_policy'
            checkpoint('paused')
            return summary
        except BaseException as error:
            source_status = getattr(error, 'source_http_status', None)
            if type(source_status) is int and source_status in {403, 429}:
                summary.update(pause_reason='source_refused', source_http_status=source_status)
                from .source_response import source_response_diagnostics
                summary.update(source_response_diagnostics(vars(error)))
                retry_id = getattr(error, 'source_retry_episode_id', None)
                if (source_status == 403 and summary.get('source_response_kind') == 'html'
                        and current is not None and retry_id == current['id']):
                    summary['source_retry_episode_id'] = retry_id
                from .retry_after import MAX_RETRY_AFTER
                delay = getattr(error, 'retry_after_seconds', None)
                if type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER:
                    summary['retry_after_seconds'] = delay
            summary["diagnostic"] = {"exception_class": type(error).__name__, "frames": [
                {"module": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)
                if Path(frame.filename).suffix == ".py"]}
            if current is not None:
                code = "interrupted" if isinstance(error, KeyboardInterrupt) else "job_failed"
                inventory.mark(current["id"], "queued" if code == "interrupted" else "failed", code)
            checkpoint("interrupted" if isinstance(error, KeyboardInterrupt) else "paused")
            from .run_receipts import retain_failure_receipt
            retain_failure_receipt(error, summary)
            raise
        finally:
            adapter.cancel_check=previous_cancel_check
            adapter.asset_metrics=previous_asset_metrics
            if isinstance(assets,Transport):assets.metrics=previous_transport_metrics


def sync(adapter, output=Path("Library"), report=None, categories=None, include_history=False,history_pages=100,history_resume=False,scan_limit=None,refresh_lists=True,scan_only=False,metadata_speed='normal', **options):
    """Refresh observed lists and watched series, reserving time to archive.

    Bounded runs spend at most half their time on metadata. Successful series
    checkpoints persist; later runs continue the scan. Every selected episode
    still receives fresh authorization before its reader is requested.
    """
    from .discovery import CATEGORIES
    if type(scan_only) is not bool:
        raise ArchiveError('Scan-only mode must be an explicit boolean.')
    from .fast_import import validate_speed, FAST_METADATA_INTERVAL
    validate_speed(metadata_speed)
    if metadata_speed == 'fast' and not scan_only:
        raise ArchiveError('Fast metadata refresh requires scan-only mode.')
    if type(refresh_lists) is not bool or (not refresh_lists and (categories is not None or include_history)):
        raise ArchiveError('Watched-only sync cannot also request reading-list categories or account history.')
    if type(include_history) is not bool or type(history_resume) is not bool or (history_resume and not include_history):
        raise ArchiveError('History discovery must be an explicit boolean.')
    if type(history_pages) is not int or not 1 <= history_pages <= 500:
        raise ArchiveError('History page limit must be from 1 to 500.')
    if scan_limit is not None and (type(scan_limit) is not int or scan_limit < 1):
        raise ArchiveError('Series scan limit must be a positive integer.')
    categories = list(CATEGORIES) if categories is None else categories
    if (not isinstance(categories, (list, tuple)) or not categories or
            any(not isinstance(c, str) or c not in sites.LISTS for c in categories)
            or len(set(categories)) != len(categories)):
        raise ArchiveError("Use distinct account lists of the site.")
    scan_counts = {'series_checks_completed':0,'successful_series_checks':0,
                   'held_series_checks':0,'series_check_target_count':0}
    reading_lists = []
    def refresh_inventory(inventory, budget, run_id):
        if include_history:
            budget.check()
            job_progress(inventory.root,run_id,phase='history')
            def history_checkpoint(value):
                budget.check()
                validated_history_links(value['content_links'])
                path=inventory.root/'.archive-state/history.json'
                atomic_json(path,{'schema_version':1,'document_type':'tapas_transaction_discovery',
                    'source':'tapas','account_inventory_complete':False,'pages':value.get('pages',[]),
                    'content_links':value['content_links'],'state':'running'})
                path.chmod(0o600)
                job_progress(inventory.root,run_id,phase='history',history_page_count=len(value.get('pages',[])),
                             observed_history_series_count=len(value['content_links']))
            history_options = {'max_pages':history_pages,'on_page':history_checkpoint}
            if history_resume:
                history_options['resume'] = True
            history = adapter.inspect_transaction_history(**history_options)
            if history.get('authenticated') is not True:
                raise AuthenticationError('Account history authentication was not verified.')
            validated_history_links(history['content_links'])
            snapshot=inventory.root/'.archive-state/history.json'
            atomic_json(snapshot,{'schema_version':1,'document_type':'tapas_transaction_discovery',
                'source':'tapas','account_inventory_complete':False,'pages':history.get('pages',[]),
                'content_links':history['content_links'],'blocker':history.get('blocker'),
                'unresolved_episode_links':[u for u in history['content_links'] if '/episode/' in u]})
            snapshot.chmod(0o600)
            for url in history['content_links']:
                budget.check()
                match = re.fullmatch(r'/series/([A-Za-z0-9_-]{1,100})(?:/info)?',urlsplit(url).path)
                if '/episode/' in url:
                    continue  # Retain the explicit association gap in the snapshot.
                if not match or urlsplit(url).hostname!='tapas.io' or urlsplit(url).scheme!='https':
                    raise MetadataError('History returned an unexpected content link.')
                job_progress(inventory.root,run_id,phase='history_series',source_slug=match[1])
                identity = inventory.known_series_link(url) or adapter.series_identity(match[1])
                inventory.observe(identity['source_series_id'],identity['title'],[],'transaction_history')
                inventory.remember_series_link(url,identity['source_series_id'])
        for category in (categories if refresh_lists else []):
            budget.check()
            job_progress(inventory.root,run_id,phase='account_list',category=category)
            library_series = set()
            library_page_count = 0
            def library_checkpoint(value):
                nonlocal library_page_count
                if value.get('authenticated') is not True or value.get('membership_verified') is not True or value.get('scope')!=category:
                    raise MetadataError('The account reading-list checkpoint scope was not verified.')
                for item in value['series']:
                    if item.get('source_series_id') is not None:
                        inventory.observe(item['source_series_id'],item['title'],[],category)
                        library_series.add(item['source_series_id'])
                library_page_count += 1
                job_progress(inventory.root,run_id,phase='account_list',category=category,
                             reading_list_page_count=library_page_count,
                             observed_library_series_count=len(library_series))
                budget.check()
            if getattr(adapter,'supports_library_checkpoint',False) is True:
                library = adapter.inspect_library(category=category,on_page=library_checkpoint)
            else:
                library = adapter.inspect_library(category=category)
            if library.get("authenticated") is not True or library.get("membership_verified") is not True or library.get("scope") != category:
                raise MetadataError("The account reading-list scope was not verified.")
            for item in library["series"]:
                if item.get("source_series_id") is not None:
                    inventory.observe(item["source_series_id"], item["title"], [], category)
            reading_lists.append({'category': category,
                                  'boundary_observed': library.get('boundary_observed') is True,
                                  'complete': library.get('complete') is True,
                                  'observed_series_count': len(library['series'])})
        candidates = inventory.scan_candidates(scan_limit)
        scan_counts['series_check_target_count'] = len(candidates)
        def series_checkpoint(series_id, held=False):
            scan_counts['series_checks_completed'] += 1
            scan_counts['held_series_checks' if held else 'successful_series_checks'] += 1
            job_progress(inventory.root,run_id,phase='scan',series_id=series_id,**scan_counts)
        for series in candidates:
            budget.check()
            job_progress(inventory.root,run_id,phase='scan',series_id=series['id'],**scan_counts)
            try:
                extra={'comic_only':True} if (getattr(adapter,'supports_comic_series_classification',False) is True
                        and getattr(adapter,'supports_novels',False) is not True) else {}
                inspected = adapter.inspect_series(series["id"], strict_order=False,**extra)
            except EpisodeCountMismatch as error:
                if error.series_id!=series['id']:
                    raise MetadataError('The pagination gap does not match the requested series.') from None
                retain_partial_series(inventory,error)
                inventory.record_scan_gap(series['id'],error.observed_count,error.reported_count)
                series_checkpoint(series['id'],held=True)
                continue
            except MetadataError as error:
                status=getattr(error,'source_http_status',None)
                if (type(status) is not int or status not in {404,410}
                        or getattr(error,'source_series_id',None)!=series['id']
                        or getattr(error,'source_route',None)!=f"/series/{series['id']}"):
                    raise
                inventory.record_series_unavailable(series['id'],status)
                series_checkpoint(series['id'],held=True)
                continue
            if inspected.get('episode_enumeration_skipped') is True:
                flags=inspected.get('series_content_flags')
                if (inspected.get('authenticated') is not True or inspected.get('complete') is not False
                        or inspected.get('source_series_id')!=series['id'] or inspected.get('episodes')!=[]
                        or inspected.get('unsupported_series_reason')!='novel'
                        or not isinstance(flags,dict) or flags.get('book') is not True or flags.get('comic') is not False):
                    raise MetadataError('The skipped content classification does not match the watched series.')
                inventory.record_novel_series_hold(series['id'])
                series_checkpoint(series['id'],held=True)
                continue
            if (inspected.get("authenticated") is not True or inspected.get("complete") is not True
                    or inspected.get("source_series_id") != series["id"]):
                raise MetadataError("Watched series refresh is incomplete or inconsistent.")
            inventory.observe(series["id"], inspected["title"], inspected["episodes"], "sync")
            inventory.record_series_scan(series['id'])
            series_checkpoint(series['id'])
        inventory.finish_scan_cycle()
    def refresh(inventory, budget, run_id):
        # Establish the persistent cycle before list pagination can exhaust
        # its window; newly observed series then contribute to remaining work.
        inventory.scan_candidates()
        deadline = adapter.deadline
        scan_seconds = budget.max_seconds if scan_only else (budget.max_seconds / 2 if budget.max_seconds is not None else None)
        scan_deadline = budget.started + scan_seconds if scan_seconds is not None else None

        class MetadataBudget:
            def check(self):
                budget.check()
                if scan_deadline is not None and time.monotonic() >= scan_deadline:
                    raise RunLimit('The metadata window ended; retaining its scan checkpoint for the next run.')

        adapter.deadline = scan_deadline
        result = {'state': 'finished', 'lists_refreshed':refresh_lists,
                  'reading_lists': reading_lists,
                  'time_budget_seconds': scan_seconds}
        try:
            refresh_inventory(inventory, MetadataBudget(), run_id)
        except RunLimit:
            # Only the elapsed metadata window permits continuing. A real
            # overall limit or another premature limit keeps the job stopped.
            budget.check()
            if scan_deadline is None or time.monotonic() < scan_deadline:
                raise
            result['state'] = 'limited'
        finally:
            adapter.deadline = deadline
        scan_status=inventory.status()
        result.update(scan_counts)
        result['remaining_series_count'] = scan_status['scan_remaining_series_count']
        result['series_scan_gaps']=scan_status['series_scan_gaps']
        if result['series_scan_gaps'] and result['state']=='finished':
            result['state']='partial'
        if result['state'] == 'finished' and result['remaining_series_count']:
            result['state'] = 'partial'
        return result
    previous = getattr(adapter, 'metadata_interval', 1.0)
    adapter.metadata_interval = FAST_METADATA_INTERVAL if metadata_speed == 'fast' else 1.0
    try:
        return archive_discovered(adapter, output, report, _refresh=refresh, _scan_only=scan_only, **options)
    finally:
        adapter.metadata_interval = previous


def archive_watched_series(adapter, series_id, output=Path('Library'), **options):
    """Refresh and archive one observed series within the normal job budgets.

    The account binding is checked before metadata. Unknown series IDs cause
    no series/reader request. Episode access is rechecked by the archive job.
    """
    if not isinstance(series_id,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',series_id):
        raise ArchiveError('Select an observed numeric series identity.')

    def refresh(inventory, budget, run_id):
        if not any(series['id']==series_id for series in inventory.watched_series()):
            raise ArchiveError('The selected series has not been observed in this library.')
        return refresh_selected_series(adapter, inventory, budget, run_id, series_id)

    return archive_discovered(adapter,output,_refresh=refresh,_series_id=series_id,**options)


def refresh_selected_series(adapter, inventory, budget, run_id, series_id):
    budget.check()
    job_progress(inventory.root,run_id,phase='scan',series_id=series_id)
    try:
        inspected = adapter.inspect_series(series_id,strict_order=False)
    except EpisodeCountMismatch as error:
        if error.series_id!=series_id:
            raise MetadataError('The pagination gap does not match the selected series.') from None
        if not retain_partial_series(inventory,error):raise
        inventory.record_scan_gap(series_id,error.observed_count,error.reported_count)
        budget.check()
        return {'state':'partial','source_series_id':series_id,'series_scan_gaps':inventory.scan_gaps()}
    if (inspected.get('authenticated') is not True or inspected.get('complete') is not True
            or inspected.get('source_series_id') != series_id):
        raise MetadataError('Selected series refresh is incomplete or inconsistent.')
    inventory.observe(series_id,inspected['title'],inspected['episodes'],'selected_series')
    inventory.record_series_scan(series_id)
    budget.check()
    return {'state':'finished','source_series_id':series_id}


def archive_series_url(adapter, url, output=Path('Library'), **options):
    """Start with a chosen comic/novel, before scanning an account watchlist."""
    from .selection import series_link
    key = series_link(url)

    def refresh(inventory, budget, run_id):
        budget.check()
        identity = adapter.series_identity(key)
        if (not isinstance(identity, dict) or not isinstance(identity.get('source_series_id'), str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', identity['source_series_id'])
                or not isinstance(identity.get('title'), str) or not identity['title'].strip()):
            raise MetadataError('The supplied link has no verified series identity.')
        selected = identity['source_series_id']
        inventory.observe(selected, identity['title'], [], 'explicit_series_link')
        return refresh_selected_series(adapter, inventory, budget, run_id, selected)

    return archive_discovered(adapter, output, _refresh=refresh, _selected_only=True, **options)
