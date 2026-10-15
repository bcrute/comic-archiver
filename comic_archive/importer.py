"""Small import orchestration; adapters decide access, Store publishes archives."""
from pathlib import Path
from collections import Counter
from .publication import PublicationStore as Store
from . import sites
from .models import ArchiveError

IMPORTER_VERSION=2


def archive_series(adapter, key, output=Path("Library"), limit=None, refresh=False):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ArchiveError("Episode limit must be a positive integer.")
    from .network import bind_adapter
    bind_adapter(adapter,output)
    inspection = adapter.inspect_series(key,strict_order=False)
    if inspection.get("complete") is not True or inspection.get("authenticated") is not True:
        raise ArchiveError("Series enumeration is incomplete or unauthenticated.")
    allowed = [item for item in inspection["episodes"] if item["access_state"] in sites.ACCESS]
    orders=Counter(item['order'] for item in inspection['episodes'] if type(item.get('order')) is int and item['order']>0)
    ordered=[item for item in allowed if type(item.get('order')) is int and item['order']>0 and orders[item['order']]==1]
    ordered.sort(key=lambda item:(item['order'],item['id']))
    ordered_ids={item['id'] for item in ordered}
    held=[item['id'] for item in allowed if item['id'] not in ordered_ids]
    selected = ordered if limit is None else ordered[:limit]
    store = Store(output)
    imported = []
    try:
        for item in selected:
            # Check current identity/access again before loading each reader.
            manifest = adapter.capture_automatic(item["source_url"])
            if manifest.series.platform_id != inspection["source_series_id"]:
                raise ArchiveError("Episode reader does not belong to the enumerated series.")
            if len(manifest.episodes) != 1 or manifest.episodes[0].id != item["id"]:
                raise ArchiveError("Episode reader does not match the enumerated episode.")
            if manifest.episodes[0].order!=item['order']:
                raise ArchiveError('Episode order changed after enumeration; no images will be retrieved.')
            store.archive(manifest, adapter, refresh=refresh)
            imported.append(item["id"])
    finally:
        store.close()
    return {"source_series_id": inspection["source_series_id"], "title": inspection["title"],
            "episode_count": inspection["episode_count"], "accessible_episode_count": len(allowed),
            "imported_episode_ids": imported, "skipped_unavailable": inspection["episode_count"] - len(allowed),
            'held_ordering_episode_ids':held,
            'ordering_verified':all(type(item.get('order')) is int and item['order']>0 and orders[item['order']]==1 for item in inspection['episodes']),
            'accessible_ordering_verified':not held}
