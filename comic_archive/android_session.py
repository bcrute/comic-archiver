"""The Android sign-in record: the site's name only; the cookies never leave the WebView store.

Desktop session storage keeps a browser storage state in the session folder. Android keeps no
cookie copy at all — WebView's own app-private store is the authority, as android_transport
reads it — so what this module writes is the smallest record that lets the existing core
learn whose sign-in exists: the same 'site' file session.store_session writes, plus an
android.json marker saying where the cookies live. No cookie name, value or attribute is
ever written here, so nothing to reconstruct or leak is persisted.
"""
import json
import os
from pathlib import Path
from . import sites
from .models import ArchiveError
from .osdep import open_exclusive, replace_file, sync_directory

MARKER = 'android.json'
# What the marker says: schema 1, the cookies live in the app's WebView store. Nothing else.
DESCRIPTOR = {'schema_version': 1, 'platform': 'android', 'cookies': 'webview-store'}
_EMPTY_STATE = '{"cookies": [], "origins": []}'


def _private_write(path, text):
    with os.fdopen(open_exclusive(path), "w") as f:
        f.write(text)
        # On disk before it takes the old file's place, so a crash leaves one or the other whole.
        f.flush()
        os.fsync(f.fileno())


def _guard(directory):
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ArchiveError("Refusing a symlink in the session directory.")


def _read_marker(directory: Path):
    try:
        marker = json.loads((directory / MARKER).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ArchiveError('The saved sign-in is unreadable; sign in again.') from None
    if marker != DESCRIPTOR:
        raise ArchiveError('The Android sign-in record has an unexpected shape; sign in again.')
    return marker


def mark_signed_in(directory: Path, site=sites.DEFAULT, compatibility_state=False):
    """Record that the site's sign-in lives in the app's WebView cookie store.

    Write after the existing core verified the session (the who-am-i check through the
    injected transport), not when the page merely looks signed in. With compatibility_state,
    also write an empty storage state so unchanged desktop paths that validate a saved
    session (background.start) accept the folder; it holds no cookie data because none is
    ever copied out of the store.
    """
    _guard(directory)
    for name in ('site.tmp', 'site', 'android.tmp', MARKER, 'state.tmp', 'state.json'):
        if (directory / name).is_symlink():
            raise ArchiveError("Refusing a symlink in session storage.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # The site first: the downloader never finds a sign-in without knowing whose it is.
    _private_write(directory / 'site.tmp', site.key)
    replace_file(directory / 'site.tmp', directory / 'site')
    sync_directory(directory)
    _private_write(directory / 'android.tmp', json.dumps(DESCRIPTOR))
    replace_file(directory / 'android.tmp', directory / MARKER)
    sync_directory(directory)
    if compatibility_state:
        # Last, as the completing write: a torn record has a site but never a bare state.
        _private_write(directory / 'state.tmp', _EMPTY_STATE)
        replace_file(directory / 'state.tmp', directory / 'state.json')
        sync_directory(directory)


def is_signed_in(directory: Path):
    """Whether an Android sign-in record exists and is whole: marker, and the site's name."""
    if _read_marker(directory) is None:
        return False
    return (directory / 'site').is_file()


def signed_in_site(directory: Path):
    """The site an Android sign-in belongs to; a torn record asks for a fresh sign-in."""
    if _read_marker(directory) is None:
        return None
    if not (directory / 'site').is_file():
        raise ArchiveError('The saved sign-in is incomplete; sign in again.')
    return sites.of_session(directory)


def forget(directory: Path):
    """Remove the Android sign-in record. The app clears the WebView store itself
    (WebViewCookieStore.clearAll) once the downloader has stopped; this only removes files.
    A link goes, never what it points to."""
    _guard(directory)
    for name in (MARKER, 'android.tmp', 'site', 'site.tmp', 'state.json', 'state.tmp'):
        (directory / name).unlink(missing_ok=True)
    if directory.is_dir():
        sync_directory(directory)
