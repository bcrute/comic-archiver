"""Read-only reconciliation of recorded access with portable archive files.

This is a dated local audit, not fresh remote authorization or proof of complete
account discovery. It deliberately does not export private settings/run data.
"""
import json
from pathlib import Path

from .archive import atomic_json, verify_episode, verify_series, episode_archives, LIBRARY_REPORT_MAX_BYTES, sidecar_path
from . import sites
from .archive_paths import directory
from .discovery import observed_id
from .inventory import Inventory, timestamp
from .locking import LibraryLock
from .models import ArchiveError
from .osdep import anchored


SUPPORTED = sites.ACCESS
ACCESS = SUPPORTED | {'unavailable_or_unverified'}
STATUSES = {'queued', 'failed', 'downloading', 'archived', 'unavailable', 'unsupported'}
REASONS = {'scheduled', 'novel', 'temporary_access', 'order_unverified', 'reader_manifest_incomplete',
           'content_unsupported', 'ordering_unverified', 'access_denied', 'archive_corrupt',
           'archive_missing', 'login_required', 'interrupted', 'job_failed'}
REASONS |= {'archive_path_invalid','archive_identity_mismatch'}
REASONS |= {'novel_manifest_incomplete','novel_access_unverified'}
PROVENANCE = {'subscribed', 'recent', 'updated', 'transaction_history', 'sync',
              'selected_series', 'episode_check', 'discovery_report','free_episodes'}


def _portable_path(root, relative):
    if not isinstance(relative, str) or '\0' in relative:
        return None
    path = Path(relative)
    if (anchored(path) or len(path.parts) != 2 or not (path.suffix in {'.cbz', '.epub'} or path.name.endswith('.tar.zst'))
            or any(part in {'.', '..'} or part.startswith('.') for part in path.parts)):
        return None
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents)):
        return None
    return target


def _metadata(path, maximum=16 * 1024 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError
    return value


def _report_target(report):
    if report is None:
        return None
    target = Path(report)
    if target.suffix != '.json' or any(p.is_symlink() for p in (target, *target.parents)):
        raise ArchiveError('Audit reports require a JSON path without symlinked parents.')
    if target.exists():
        try:
            previous = _metadata(target, maximum=LIBRARY_REPORT_MAX_BYTES)
            if (previous.get('document_type') != 'comic_archive_reconciliation' or previous.get('schema_version') != 1
                    or 'content_hashes' in previous or 'source_episode_id' in previous):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ArchiveError('The report path contains another file; choose a new JSON filename.') from None
    return target


def _source_series(meta):
    return str(meta.get('source_series_id', meta.get('series_id')))


def audit_library(output, report=None):
    target = _report_target(report)
    root = Path(output).absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ArchiveError('Audit requires an existing library without symlinked parents.')
    with LibraryLock(root), Inventory(root, read_only=True) as inventory:
        checked_at = timestamp()
        status = inventory.status()
        rows = [dict(row) for row in inventory.db.execute('SELECT * FROM episodes ORDER BY series_id,episode_order,id')]
        series_rows = [dict(row) for row in inventory.db.execute('''SELECT s.*,scans.observed_at AS last_episode_scan
            FROM series s LEFT JOIN series_scans scans ON scans.series_id=s.id ORDER BY s.id''')]
        if (any(not observed_id(row['id']) or not observed_id(row['series_id']) for row in rows)
                or any(not observed_id(row['id']) for row in series_rows)):
            raise ArchiveError('Inventory source identities are invalid; repair the index before auditing.')
        portable = {}
        folder_problems = []
        # The portable layout is one public series folder per library. Do not
        # traverse private directories or links while searching for exports.
        for folder in sorted(root.iterdir()):
            if folder.name.startswith('.') or folder.is_symlink() or not folder.is_dir():
                continue
            paths = [p for p in episode_archives(folder) if not p.name.startswith('.')]
            if not paths:
                continue
            series_meta = None
            try:
                from .publication import import_metadata
                series_meta = import_metadata(folder / 'series.json')
                if verify_series(folder / 'series.json'):
                    raise ValueError
            except (OSError, ValueError, TypeError, ArchiveError):
                folder_problems.append({'series_folder': folder.name, 'reason': 'series_metadata_or_cover_invalid'})
            for path in paths:
                relative = path.relative_to(root).as_posix()
                reason, meta = None, None
                if _portable_path(root, relative) is None:
                    reason = 'archive_path_invalid'
                else:
                    try:
                        meta = import_metadata(sidecar_path(path))
                        if verify_episode(path):
                            reason = 'archive_integrity_failed'
                        elif meta.get('source') in sites.KEYS and (not observed_id(meta.get('source_episode_id'))
                                or not observed_id(_source_series(meta))):
                            reason = 'archive_metadata_identity_invalid'
                        elif series_meta is not None and (meta.get('source') != series_meta.get('source')
                                or _source_series(meta) != str(series_meta.get('source_series_id'))):
                            reason = 'series_identity_mismatch'
                    except (OSError, ValueError, TypeError, ArchiveError):
                        reason = 'archive_integrity_failed'
                portable[relative] = {'reason': reason, 'metadata': meta}

        episodes = []
        referenced = set()
        verified = 0
        cloud_uploaded = 0
        unresolved_supported = 0
        integrity_problems = []
        per_series = {}
        identities = {}
        index_problems = []
        for relative, entry in portable.items():
            meta = entry['metadata']
            if not entry['reason'] and meta is not None and meta.get('source') in sites.KEYS and observed_id(meta.get('source_episode_id')):
                identities.setdefault((meta.get('source_episode_id'), _source_series(meta)), []).append(relative)
        for row in rows:
            access = row['access_state'] if row['access_state'] in ACCESS else 'unavailable_or_unverified'
            stored_status = row['status'] if row['status'] in STATUSES else 'unknown'
            episode = {'source_episode_id': row['id'], 'source_series_id': row['series_id'],
                       'title': row['title'], 'episode_order': row['episode_order'],
                       'access_state': access, 'access_observed_at': row['observed_at'],
                       'recorded_status': stored_status, 'archive_verified': False,
                       'cloud_upload_verified':False,
                       'reason': row['error_code'] if row['error_code'] in REASONS else None}
            relative = row['archive_path']
            if relative is None:
                found = identities.get((row['id'], row['series_id']), [])
                if len(found) == 1:
                    relative = found[0]
            path = _portable_path(root, relative)
            entry = portable.get(path.relative_to(root).as_posix()) if path is not None else None
            if relative is not None:
                if path is None:
                    problem = 'archive_path_invalid'
                elif entry is None:
                    from .cloud import offloaded
                    try:
                        receipt = offloaded(root, path)
                        meta = import_metadata(sidecar_path(path)) if receipt else None
                        cloud_match = bool(meta and meta.get('source') in sites.KEYS and meta.get('source_episode_id') == row['id']
                                           and _source_series(meta) == row['series_id'])
                    except (ArchiveError, OSError, ValueError, TypeError):
                        cloud_match = False
                    if cloud_match:
                        episode['cloud_upload_verified'] = True
                        episode['cloud_verified_at'] = receipt['verified_at']
                        cloud_uploaded += 1
                    problem = None if cloud_match else 'archive_missing'
                elif entry['reason']:
                    problem = entry['reason']
                else:
                    meta = entry['metadata']
                    problem = None if (meta.get('source') in sites.KEYS and meta.get('source_episode_id') == row['id']
                                       and _source_series(meta) == row['series_id']) else 'archive_identity_mismatch'
                if path is not None:
                    referenced.add(path.relative_to(root).as_posix())
                if problem is None and episode['cloud_upload_verified']:
                    episode['archive_path'] = path.relative_to(root).as_posix()
                    if stored_status != 'archived':
                        index_problems.append({'source_episode_id':row['id'],'reason':'inventory_index_out_of_sync'})
                elif problem is None:
                    episode['archive_verified'] = True
                    episode['archive_path'] = path.relative_to(root).as_posix()
                    verified += 1
                    if stored_status != 'archived' or row['archive_path'] != relative:
                        index_problems.append({'source_episode_id': row['id'], 'reason': 'inventory_index_out_of_sync'})
                else:
                    episode['reason'] = problem
                    integrity_problems.append({'source_episode_id': row['id'], 'reason': problem})
            elif stored_status == 'archived':
                episode['reason'] = 'archive_missing'
                integrity_problems.append({'source_episode_id': row['id'], 'reason': 'archive_missing'})
            if access in SUPPORTED and not (episode['archive_verified'] or episode['cloud_upload_verified']):
                unresolved_supported += 1
                if episode['reason'] is None:
                    episode['reason'] = {'queued': 'awaiting_archive', 'failed': 'retry_required',
                                         'downloading': 'interrupted_or_active', 'unsupported': 'content_unsupported'}.get(stored_status, 'record_requires_review')
            if access not in SUPPORTED and not episode['archive_verified'] and episode['reason'] is None:
                episode['reason'] = 'unavailable_or_unverified'
            episodes.append(episode)
            counts = per_series.setdefault(row['series_id'], {'verified': 0, 'missing_recorded_accessible': 0, 'by_status': {}})
            counts['verified'] += int(episode['archive_verified'])
            counts['missing_recorded_accessible'] += int(access in SUPPORTED and not (episode['archive_verified'] or episode['cloud_upload_verified']))
            counts['by_status'][stored_status] = counts['by_status'].get(stored_status, 0) + 1

        unindexed = []
        for relative, entry in portable.items():
            if relative not in referenced:
                unindexed.append({'archive_path': relative, 'reason': entry['reason'] or 'archive_not_reconciled_to_inventory'})
                if entry['reason']:
                    integrity_problems.append({'archive_path': relative, 'reason': entry['reason']})
        series = []
        for row in series_rows:
            try:
                origins = json.loads(row['provenance'])
            except (ValueError, TypeError):
                origins = []
            if not isinstance(origins, list): origins = []
            series.append({'source_series_id': row['id'], 'title': row['title'],
                           'provenance': sorted({item for item in origins if isinstance(item, str) and item in PROVENANCE}),
                           'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
                           'last_episode_scan': row['last_episode_scan'],
                           'episodes': per_series.get(row['id'], {'verified': 0, 'missing_recorded_accessible': 0, 'by_status': {}})})
        scanned = bool(series) and not status['series_without_scan_checkpoint_count'] and not status['scan_remaining_series_count'] and not status['series_scan_gap_count']
        valid_files = not integrity_problems and not folder_problems
        value = {'schema_version': 1, 'document_type': 'comic_archive_reconciliation', 'source': 'tapas',
                 'audited_at': checked_at, 'access_snapshot_only': True, 'remote_authorization_checked': False,
                 'account_inventory_complete': False, 'account_archive_complete': False,
                 'coverage_blockers': ['independent_purchase_inventory_unverified'],
                 'last_completed_scan': status['last_completed_scan'],
                 'scan_remaining_series_count': status['scan_remaining_series_count'],
                 'series_scan_gap_count':status['series_scan_gap_count'],'series_scan_gaps':status['series_scan_gaps'],
                 'series_without_scan_checkpoint_count': status['series_without_scan_checkpoint_count'],
                 'all_observed_series_have_scan_checkpoints': bool(series) and not status['series_without_scan_checkpoint_count'],
                 'current_scan_cycle_complete': scanned,
                 'recorded_accessible_episode_count': sum(row['access_state'] in SUPPORTED for row in rows),
                 'missing_recorded_accessible_episode_count': unresolved_supported,
                 'verified_inventory_episode_count': verified, 'portable_archive_count': len(portable),
                 'cloud_uploaded_inventory_episode_count':cloud_uploaded, 'cloud_availability_checked':False,
                 'archive_integrity_passed': valid_files,
                 'observed_snapshot_archived': scanned and valid_files and not unresolved_supported and not unindexed and not index_problems,
                 'integrity_problems': integrity_problems, 'series_metadata_problems': folder_problems,
                 'inventory_index_problems': index_problems,
                 'unindexed_archives': unindexed, 'series': series, 'episodes': episodes}
        if not scanned: value['coverage_blockers'].append('watched_series_scan_incomplete')
        if status['series_scan_gap_count']:value['coverage_blockers'].append('series_episode_counts_unreconciled')
        if unresolved_supported: value['coverage_blockers'].append('recorded_accessible_episodes_missing')
        if not valid_files: value['coverage_blockers'].append('portable_files_require_repair')
        if unindexed: value['coverage_blockers'].append('portable_archives_not_reconciled')
        if index_problems: value['coverage_blockers'].append('inventory_index_out_of_sync')
        if target is not None:
            # Revalidate under the lock before publication, without allowing an
            # audit command to replace another kind of metadata/export file.
            _report_target(target)
            directory(target.parent)
            atomic_json(target, value)
        return value
