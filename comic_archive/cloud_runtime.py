"""Explicit platform selection; desktop keeps its installed, pinned cloud helper.

Only the embedded application bootstrap enables the native B2 implementation. This
does not enable uploads or change a library's retention/connection policy.
"""
_android = False
# Set by the desktop app and its downloader: only connections this computer knows of take episodes (cloud_trust).
_checked = False


def install_android():
    global _android
    from .b2_native import NativeB2Transfer
    from .transport import protect_http_logs
    if not callable(NativeB2Transfer):
        raise RuntimeError('Native cloud transport is unavailable.')
    protect_http_logs()
    _android = True


def transfer(root, provider, target):
    if _android:
        from .b2_native import NativeB2Transfer
        return NativeB2Transfer(root, provider, target)
    from .cloud import Transfer
    return Transfer(provider, target, root=root)


def ensure(root, cancel):
    if _android:
        from .network import NetworkGuard
        from .models import ArchiveError
        if cancel.is_set():
            raise ArchiveError('Cloud sign-in stopped.')
        NetworkGuard(root).check()
        return
    from .cloud_tools import ensure as desktop_ensure
    return desktop_ensure(root, cancel)


def check_connections():
    global _checked
    _checked = True


def trusted(target):
    """Whether new episodes may go to a connection. Android's library is the phone's own, and so is every
    connection in it."""
    if _android or not _checked:
        return True
    from .cloud_trust import trusted as known
    return known(target)


def trust(target):
    """Note a connection set up here, or that the person said to use."""
    if _checked and not _android:
        from .cloud_trust import add
        add(target)


def note_connections():
    """Before a library folder is first noted as opened: the connections in use before this version are noted,
    and none it brings."""
    if _checked and not _android:
        from .cloud_trust import known
        known()


def offered():
    if _android:
        return True
    from .cloud_tools import offered as desktop_offered
    return desktop_offered()
