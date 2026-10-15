"""Read-only source observations and device estimates before an import."""
from pathlib import Path
import time

from .discovery import observed_id, summarize_series
from .errors import AuthenticationError, MetadataError
from .inventory import Inventory
from .models import ArchiveError
from .network import bind_adapter
from .selection import series_link
from .storage import safe_root, storage_plan


def validate_options(options):
    if (set(options) != {'url', 'scope'} or not isinstance(options['url'], str)
            or len(options['url']) > 500 or not isinstance(options['scope'], str)
            or options['scope'] not in {'one', 'series'}):
        raise ArchiveError('Storage checks accept a public series link and one/series scope.')
    return series_link(options['url'])


def preview_series(adapter, url, root=Path('Library'), *, scope='series'):
    key = validate_options({'url': url, 'scope': scope})
    root = safe_root(root)
    inventory_path = safe_root(root / '.archive-state/inventory.sqlite3')
    bind_adapter(adapter, root)
    previous_deadline = getattr(adapter, 'deadline', None)
    previous_identity = getattr(adapter, 'expected_account_id', None)
    adapter.deadline = time.monotonic() + 300
    try:
        identity = adapter.account_identity()
        if not observed_id(identity):
            raise AuthenticationError('Sign in before checking source access and storage.')
        if inventory_path.is_file():
            with Inventory(root, read_only=True) as inventory:
                inventory.account_binding_state(identity)
        adapter.expected_account_id = identity
        inspection = adapter.inspect_series(key, strict_order=False)
        summary = summarize_series(inspection)
        flags = inspection.get('series_content_flags')
        kind, verified_kind = 'comic', False  # Conservative for older adapters.
        if flags is not None:
            if (not isinstance(flags, dict) or set(flags) != {'book', 'comic'}
                    or any(type(value) is not bool for value in flags.values())
                    or (flags['book'], flags['comic']) not in {(True, False), (False, True)}):
                raise MetadataError('Series content type is unverified; no import was started.')
            kind, verified_kind = ('novel' if flags['book'] else 'comic'), True
        eligible = {row['id'] for row in summary['accessible_episodes']}
        saved = set()
        if inventory_path.is_file():
            with Inventory(root, read_only=True) as inventory:
                saved = {row[0] for row in inventory.db.execute(
                    "SELECT id FROM episodes WHERE series_id=? AND status='archived'",
                    (summary['source_series_id'],))} & eligible
        remaining = len(eligible - saved)
        planned = min(remaining, 1) if scope == 'one' else remaining
        plan = storage_plan(root, episode_count=planned, episode_kind=kind)
        preview = {key: plan[key] for key in ('free_bytes', 'estimated_additional_archive_bytes',
            'estimated_required_free_bytes', 'staging_bytes', 'reserve_bytes',
            'fits_estimate', 'storage_destination', 'image_profile')}
        preview.update(observed_episode_count=summary['episode_count'],
            accessible_episode_count=len(eligible), already_saved_episode_count=len(saved),
            planned_episode_count=planned, mean_episode_bytes=plan['mean_'+kind+'_bytes'],
            sample_count=plan['episode_size_statistics'][kind]['sample_count'],
            content_type=kind, content_type_verified=verified_kind, scope=scope,
            access_snapshot_only=True, reader_or_comic_pages_requested=False)
        return {'storage_preview': preview}
    finally:
        adapter.deadline = previous_deadline
        adapter.expected_account_id = previous_identity


def public_preview(value):
    """Allow fixed metrics only; never project source prose, paths or credentials."""
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ('free_bytes', 'estimated_additional_archive_bytes',
        'estimated_required_free_bytes', 'staging_bytes', 'reserve_bytes', 'observed_episode_count',
        'accessible_episode_count', 'already_saved_episode_count', 'planned_episode_count',
        'mean_episode_bytes', 'sample_count') if type(value.get(key)) is int and value[key] >= 0}
    for key in ('fits_estimate', 'content_type_verified', 'access_snapshot_only', 'reader_or_comic_pages_requested'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key, choices in (('content_type', {'comic', 'novel'}), ('scope', {'one', 'series'}),
            ('storage_destination', {'device', 'cloud'}), ('image_profile', {'original', 'balanced', 'small'})):
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    return result
