"""Optional public metadata enrichment; original episode archives stay portable."""
from .archive import verify_series
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
            raise MetadataError('Refreshed cover metadata changed series identity.') from None
        series=fresh
        cover=store.cover(series,retrieval,refresh)
    folder=store.write_series_metadata(series,cover=cover,enriched=True)
    if verify_series(folder/'series.json'):
        raise ArchiveError('Enriched series metadata/cover did not verify.')
    return {'source_series_id':series.platform_id,'title':series.title,'folder':str(folder),'creator_count':len(series.creators),
            'description_character_count':len(series.description or ''),'credits_character_count':len(series.credits or ''),
            'cover_archived':cover is not None,'reader_or_comic_pages_requested':False}


