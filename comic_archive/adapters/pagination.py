"""Explicit observed catalog gaps, separate from authorization/schema failures."""
from ..errors import MetadataError


class EpisodeCountMismatch(MetadataError):
    code='pagination_count_mismatch'

    def __init__(self,series_id,observed_count,reported_count):
        super().__init__('Tapas pagination stopped before the reported episode count.')
        self.series_id=str(series_id)
        self.observed_count=observed_count
        self.reported_count=reported_count
