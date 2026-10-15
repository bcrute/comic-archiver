"""Device-space planning and persistent, explicit storage choices; no network."""
import json
import math
from pathlib import Path
import shutil
from .archive import atomic_json, episode_archives, sidecar_path
from .image_profiles import validate_profile, prerequisites
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError

DEFAULTS = {'schema_version': 1, 'image_profile': 'original', 'purge_completed_cache': True}
# October 10 verified CBZ (11,690) / EPUB (319) measurements; rough fallback only.
COMIC_BYTES = 20_586_261
NOVEL_BYTES = 21_512
RESERVE_BYTES = 1024**3


def safe_root(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ArchiveError('Storage paths must not be symlinked.')
    return root


def policy(root):
    path = safe_root(root) / '.archive-state/storage-policy.json'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Storage settings must not be symlinked.')
    if not path.exists():
        return dict(DEFAULTS)
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text())
        if (set(value) != set(DEFAULTS) or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['purge_completed_cache']) is not bool):
            raise ValueError
        validate_profile(value['image_profile'])
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ArchiveError('Storage settings are invalid; existing files are retained.') from None


def configure(root, *, image_profile='original', purge_completed_cache=True):
    root = safe_root(root)
    prerequisites(image_profile)
    if type(purge_completed_cache) is not bool:
        raise ArchiveError('Cache cleanup must be an explicit boolean.')
    with LibraryLock(root):
        value = dict(DEFAULTS, image_profile=image_profile, purge_completed_cache=purge_completed_cache)
        path = root / '.archive-state/storage-policy.json'
        if path.is_symlink():
            raise ArchiveError('Storage settings must not be symlinked.')
        atomic_json(path, value)
        path.chmod(0o600)
    return value


def storage_plan(root, *, episode_count=None, episode_kind='comic', local_only=False):
    root = safe_root(root)
    if episode_count is not None and (type(episode_count) is not int or not 0 <= episode_count <= 10_000_000):
        raise ArchiveError('Storage estimates accept a nonnegative episode count up to ten million.')
    if not isinstance(episode_kind, str) or episode_kind not in {'comic', 'novel'}:
        raise ArchiveError('Storage estimates require comic or novel content.')
    location = root
    while not location.exists():
        location = location.parent
    free = shutil.disk_usage(location).free
    current = {'comic': {'count': 0, 'bytes': 0}, 'novel': {'count': 0, 'bytes': 0}}
    samples = {'comic': [], 'novel': []}
    formats = {'cbz': 0, 'tar.zst': 0, 'epub': 0}
    series_types = {}
    pending = []
    inventory_path = root / '.archive-state/inventory.sqlite3'
    if inventory_path.is_file():
        with Inventory(root, read_only=True) as inventory:
            pending = inventory.pending()
    if root.is_dir():
        for folder in root.iterdir():
            if not folder.is_dir() or folder.name.startswith('.') or folder.is_symlink():
                continue
            for path in episode_archives(folder):
                sidecar = sidecar_path(path)
                if path.is_symlink() or sidecar.is_symlink():
                    continue
                try:
                    if sidecar.stat().st_size > 16*1024**2:
                        continue
                    value = json.loads(sidecar.read_text())
                    kind = value.get('content_type', 'comic')
                    if kind not in current:
                        continue
                    size = path.stat().st_size
                    current[kind]['count'] += 1
                    current[kind]['bytes'] += size
                    samples[kind].append(size)
                    archive_format = 'tar.zst' if path.name.endswith('.tar.zst') else path.suffix[1:]
                    if archive_format in formats: formats[archive_format] += 1
                    # Only private matching provenance can associate neutral
                    # files with pending source-series observations.
                    from .publication import import_metadata
                    metadata = import_metadata(sidecar)
                    series_types[str(metadata.get('source_series_id', metadata.get('series_id')))] = kind
                except (OSError, ValueError, TypeError, AttributeError, ArchiveError):
                    continue
    means = {kind: (value['bytes'] / value['count'] if value['count'] else fallback)
             for kind, value, fallback in [('comic', current['comic'], COMIC_BYTES), ('novel', current['novel'], NOVEL_BYTES)]}
    pending_count = len(pending) if episode_count is None else episode_count
    estimate = (sum(means[series_types.get(row['series_id'], 'comic')] for row in pending)
                if episode_count is None else pending_count * means[episode_kind])
    estimate = math.ceil(estimate)
    from .cloud import settings as cloud_settings
    cloud = cloud_settings(root)
    to_cloud = not local_only and bool(cloud and any(t['enabled'] for t in cloud['targets'].values()))
    # One episode of originals + a staging archive, with room for a large
    # episode. Compression is not promised before actual samples are encoded.
    staging = max(256*1024**2, math.ceil(means['comic'] * 4)) if pending_count else 0
    additional_local = 0 if to_cloud else estimate
    needed = math.ceil(additional_local * 1.25) + staging + RESERVE_BYTES
    saved_count = sum(v['count'] for v in current.values())
    saved_bytes = sum(v['bytes'] for v in current.values())
    statistics = {}
    for kind, values in samples.items():
        values.sort()
        statistics[kind] = {'sample_count':len(values), 'mean_bytes':round(means[kind]),
            'minimum_bytes':values[0] if values else None,
            'maximum_bytes':values[-1] if values else None,
            'p10_bytes':values[math.floor((len(values)-1)*.1)] if values else None,
            'p90_bytes':values[math.ceil((len(values)-1)*.9)] if values else None,
            'basis':'observed_archives' if values else 'rough_reference_sample'}
    return {'free_bytes': free, 'saved_episode_count': saved_count, 'saved_archive_bytes': saved_bytes,
            'episode_size_statistics':statistics, 'saved_archive_formats':formats,
            'mean_saved_episode_bytes': round(saved_bytes / saved_count) if saved_count else None,
            'mean_comic_bytes': round(means['comic']), 'mean_novel_bytes': round(means['novel']),
            'known_pending_episode_count': pending_count, 'estimated_additional_archive_bytes': estimate,
            'estimated_additional_local_bytes':additional_local, 'storage_destination':'cloud' if to_cloud else 'device',
            'estimated_required_free_bytes': needed, 'staging_bytes': staging, 'reserve_bytes': RESERVE_BYTES,
            'fits_estimate': needed <= free, 'inventory_complete': False,
            'estimate_basis': 'observed_archives' if saved_count else 'rough_reference_sample',
            'compression_savings_verified': False, 'image_profile': policy(root)['image_profile'],
            'choices': ['original', 'balanced', 'small', 'google_drive', 'proton_drive', 'use_pc'],
            'recommendation': 'original' if needed <= free else 'compress_or_use_pc',
            'note': 'Known episodes only. Unseen entitlements and image sizes can change the estimate. Compact images reduce quality; no fixed savings ratio is guaranteed.'}
