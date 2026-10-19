"""Finishing a Tapas sign-in: who signed in, and handing their session to the downloader."""

from . import background
from .inventory import Inventory
from .models import ArchiveError


def identity(value):
    if not isinstance(value, dict) or value.get('error_details') is not None:
        return None
    data = value.get('data')
    user = data.get('user') if isinstance(data, dict) else None
    key = user.get('id') if isinstance(user, dict) else None
    return str(key) if type(key) is int and key > 0 else None


def complete(workspace, key, browser, save, cancelled, interval):
    """Replace the downloader's session with a verified sign-in; False when cancelled before saving."""
    with Inventory(workspace.library, read_only=True) as inventory:
        inventory.account_binding_state(key)
    result = background.stop(workspace.session, wait_seconds=35)
    if result.get('running') is not False:
        raise ArchiveError('The previous worker is still stopping.')
    if cancelled():
        return False
    save()
    result = background.start(workspace.session, browser, interval)
    if result.get('startup_pending') and result.get('running') is False:
        # Started, but slow to come up, as the first start after installing can be while Windows checks
        # its files. The sign-in is saved and kept; the downloader goes on starting by itself.
        return True
    if result.get('running') is not True or result.get('start_deferred'):
        raise ArchiveError('Downloader startup is pending.')
    return True
