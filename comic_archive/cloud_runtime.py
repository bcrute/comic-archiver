"""Explicit platform selection; desktop keeps its installed, pinned cloud helper.

Only the embedded application bootstrap enables the native B2 implementation. This
does not enable uploads or change a library's retention/connection policy.
"""
_android = False


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
    return Transfer(provider, target)


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


def offered():
    if _android:
        return True
    from .cloud_tools import offered as desktop_offered
    return desktop_offered()
