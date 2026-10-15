"""Optional public metadata enrichment; original episode archives stay portable."""
from pathlib import Path
from .archive import verify_series
from .publication import PublicationStore as Store
from .inventory import Inventory
from .errors import MetadataError
from .models import ArchiveError
from .transport import ExpiredAsset


def enrich_with_store(adapter,key,store,refresh=False,cover_adapter=None):
    from .network import bind_adapter
    bind_adapter(adapter,store.root)
    if cover_adapter is not None: bind_adapter(cover_adapter,store.root)
    series=adapter.series_metadata(key)
    if str(key).isdigit() and series.platform_id!=str(key):
        raise MetadataError('Enrichment returned a different numeric series identity.')
    retrieval=cover_adapter or adapter
    try:
        cover=store.cover(series,retrieval,refresh)
    except ExpiredAsset:
        fresh=adapter.series_metadata(key)
        if fresh.platform_id!=series.platform_id:
            raise MetadataError('Refreshed cover metadata changed series identity.')
        series=fresh
        cover=store.cover(series,retrieval,refresh)
    folder=store.write_series_metadata(series,cover=cover,enriched=True)
    if verify_series(folder/'series.json'):
        raise ArchiveError('Enriched series metadata/cover did not verify.')
    return {'source_series_id':series.platform_id,'title':series.title,'folder':str(folder),'creator_count':len(series.creators),
            'description_character_count':len(series.description or ''),'credits_character_count':len(series.credits or ''),
            'cover_archived':cover is not None,'reader_or_comic_pages_requested':False}


def enrich_series(adapter,key,output=Path('Library'),refresh=False):
    if type(refresh) is not bool:
        raise ArchiveError('Metadata refresh must be an explicit boolean.')
    from .network import bind_adapter
    bind_adapter(adapter,output)
    with Inventory(output) as inventory:
        identity=adapter.account_identity()
        inventory.bind_account(identity)
        adapter.expected_account_id=identity
        store=Store(output)
        try:
            result=enrich_with_store(adapter,key,store,refresh)
            # A public series link is an explicit watch entry, never access proof.
            inventory.observe(result['source_series_id'],result['title'],[],'metadata')
            return result
        finally:
            store.close()
