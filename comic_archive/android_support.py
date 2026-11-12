"""The phone's log, and the desktop's Copy details and Save log for support on the phone.

The phone's download runs inside the app, so one private log holds the app's own messages and the download's.
What is copied or saved is put together by the same code as on the desktop: the app's words, codes and counts,
and each site's series and episode numbers, never a title, a folder name or anything of a sign-in.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import osdep, support_details, support_log
from .models import ArchiveError

LOG_NAME = 'phone.log'
APP_LOG = 'comic_archive.android'
_kept = {'handler': None}


def _logs():
    return logging.getLogger(APP_LOG), logging.getLogger(support_log.DOWNLOAD_LOG)


def configure(folder):
    """Write the app's and the download's messages to a private rotating file in folder; whether it could."""
    folder = Path(folder)
    if _kept['handler'] is not None:
        for log in _logs():
            log.removeHandler(_kept['handler'])
        _kept['handler'].close()
        _kept['handler'] = None
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / LOG_NAME
        if osdep.linked(folder) or path.is_symlink():
            raise OSError
        # Opened as the log itself is: privately, appending, never through a link.
        os.close(osdep.open_log(path))
        handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=2, encoding='utf-8')
    except (OSError, ArchiveError):
        # The app still runs; it only cannot explain itself afterwards.
        return False
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    for log in _logs():
        log.setLevel(logging.INFO)
        log.propagate = False
        log.addHandler(handler)
    _kept['handler'] = handler
    support_log.catch_unexpected(APP_LOG)
    return True


def details(version, system, sites):
    """What Copy details for support copies. sites is [(name, state)]: every site the app offers, with what its
    card last read, or None for one not opened."""
    return support_details.text(version, sites, system)


def save(out, log_folder, details_text, opened, folders=(), home=None, now=None):
    """Save log for support: one new file in out; its path. opened is [(name, session, library)] of the open
    sites; folders are further library folders to leave out, and home the app's own, written as ~."""
    records, libraries = [], [str(folder) for folder in folders]
    for name, session, library in opened:
        records.append(support_log.site_records(name, session, library))
        libraries.append(str(Path(library).parent))
    text = support_log.bundle(details_text, records, support_log.app_log(log_folder, LOG_NAME), libraries,
                              now=now, device='phone', home=home)
    return support_log.save(out, text, now)
