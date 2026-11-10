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


class SpeedChangeError(ArchiveError):
    """A fixed, safe explanation of why a controlled speed change did not complete."""
    def __init__(self, reason):
        messages = {
            'stopping': ('The download is still stopping. The speed was not changed and it remains paused. '
                         'Wait for it to stop, then choose the speed again.'),
            'closing': 'The app is closing. The speed change was stopped and the download was not restarted.',
            'save': ('The new speed could not be saved. The download remains paused if it was interrupted. '
                     'Check that the library folder can be written to, then choose the speed again.'),
            'unavailable': ('The speed change could not finish. The download was not restarted. '
                            'Check its status before continuing.'),
        }
        self.reason = reason if reason in messages else 'unavailable'
        super().__init__(messages[self.reason])


class DownloadSelectionChangeError(ArchiveError):
    """A controlled choice change failed without silently resuming excluded work."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Download choices could not be changed. The download was not restarted. '
                         'Wait for it to stop, then try again.')


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
