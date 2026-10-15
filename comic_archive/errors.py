"""Safe, typed failures so jobs can distinguish access changes from outages."""
from .models import ArchiveError


class AuthenticationError(ArchiveError):
    pass


class AccessDenied(ArchiveError):
    pass


class UnsupportedContent(ArchiveError):
    pass


class MetadataError(ArchiveError):
    pass


class LibraryBusy(ArchiveError):
    pass


class DeviceFileOpenUnavailable(ArchiveError):
    """The local device could not hand a saved file to its default app."""


class RunLimit(ArchiveError):
    pass


class NetworkUnavailable(ArchiveError):
    pass


class AssetReadUnavailable(NetworkUnavailable):
    """An original CDN asset exhausted retries without an HTTP refusal."""
