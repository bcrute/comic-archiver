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


class LibraryMoved(ArchiveError):
    """The open library folder was renamed, moved or deleted. It is never made again in its old place."""

    def __init__(self, message='The library folder was renamed, moved or deleted, so nothing more is saved. '
                               'Choose the folder again where it is now.'):
        super().__init__(message)
