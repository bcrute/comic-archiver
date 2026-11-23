"""Persistent supplemental-content work, independent of archived comic/novel files."""
from contextlib import ExitStack
import hashlib
import os
import re
import stat

from . import extras, sites
from .osdep import NOFOLLOW, NONBLOCK

MAX_ATTEMPTS = 3


def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS extras_collections (
        episode_id TEXT NOT NULL, kind TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (episode_id, kind))''')


def records(db, episode_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='extras_collections' AND type='table'").fetchone():
        return {}
    query = 'SELECT episode_id,kind,done,attempts FROM extras_collections'
    if episode_id is not None:
        query += ' WHERE episode_id=?'
    return {(row[0], row[1]): (row[2], row[3]) for row in db.execute(
        query, () if episode_id is None else (episode_id,))}


def wanted(db, site, episode_id, known=None, chosen=None):
    known = records(db, episode_id) if known is None else known
    chosen = extras.read(db) if chosen is None else chosen
    return [kind for kind in extras.offered(site) if chosen[kind]
            and not known.get((episode_id, kind), (0, 0))[0]
            and known.get((episode_id, kind), (0, 0))[1] < MAX_ATTEMPTS]


def record(db, episode_id, kinds, done):
    if not kinds:
        return
    ensure(db)
    with db:
        db.executemany('''INSERT INTO extras_collections VALUES (?,?,?,?)
            ON CONFLICT(episode_id,kind) DO UPDATE SET done=excluded.done,
            attempts=extras_collections.attempts+excluded.attempts''',
                       [(episode_id, kind, int(done), int(not done)) for kind in kinds])


def pending(inventory, free_only=False):
    chosen = extras.read(inventory.db)
    if not any(chosen.values()):
        return []
    known = records(inventory.db)
    selected = inventory.selected_episode_ids()
    rows = []
    for row in inventory.db.execute("SELECT * FROM episodes WHERE status='archived' ORDER BY series_id,episode_order,id"):
        if selected is not None and row['id'] not in selected:
            continue
        site = next((site for site in sites.SITES if row['access_state'].startswith(site.key + '_')), None)
        if site is None or free_only and row['access_state'] != site.free:
            continue
        if wanted(inventory.db, site.key, row['id'], known, chosen):
            rows.append(dict(row, extras_only=True, access_expires_at=None))
    return rows


def _legacy_music(folder, episode_id, tracks, check):
    if not isinstance(tracks, list) or not 0 < len(tracks) <= extras.MAX_TRACKS:
        return False
    filename = re.compile(re.escape(episode_id) + r'\.music-([1-9][0-9]{0,2})\.(?:mp3|m4a|ogg|flac|wav)')
    identities = set()
    inputs = []
    remaining = extras.MAX_TRACKS * extras.MUSIC_BYTES
    with ExitStack() as stack:
        # Validate the entire selection before reading any track's contents.
        for track in tracks:
            check()
            if not isinstance(track, dict) or not isinstance(track.get('file'), str):
                return False
            match = filename.fullmatch(track['file'])
            if not match or int(match[1]) > extras.MAX_TRACKS:
                return False
            path = folder / track['file']
            try:
                before = path.lstat()
                if (not stat.S_ISREG(before.st_mode)
                        or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    return False
                fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK | getattr(os, 'O_BINARY', 0))
            except OSError:
                return False
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            opened = os.fstat(stream.fileno())
            identity = (opened.st_dev, opened.st_ino)
            if (not stat.S_ISREG(opened.st_mode) or not opened.st_ino or identity in identities
                    or (before.st_ino and identity != (before.st_dev, before.st_ino))
                    or not 0 < opened.st_size <= min(extras.MUSIC_BYTES, remaining)):
                return False
            identities.add(identity)
            remaining -= opened.st_size
            inputs.append((path, stream, opened, track.get('sha256')))

        for path, stream, opened, expected in inputs:
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                check()
                chunk = stream.read(min(remaining, 64 * 1024))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(stream.fileno())
            if ((after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)
                    or digest.hexdigest() != expected):
                return False
            try:
                current = path.lstat()
            except OSError:
                return False
            if (not stat.S_ISREG(current.st_mode)
                    or getattr(current, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or not os.path.samestat(after, current)):
                return False
    return True


def adopt_existing(inventory, site, row, kinds, check):
    """Recognize extras written by versions which had no per-kind completion receipts."""

    value = extras.saved_value(inventory.root, site, row['series_id'], row['id'])
    known = records(inventory.db, row['id'])
    fields = dict(notes='creator_note', music='music', stats='stats', comments='comments')
    kept = [kind for kind in kinds if (row['id'], kind) not in known and fields[kind] in value]
    if 'music' in kept and not _legacy_music(extras.folder(inventory.root, site, row['series_id']),
                                              row['id'], value['music'], check):
        kept.remove('music')
    check()
    record(inventory.db, row['id'], kept, True)


def collect(inventory, adapter, budget, row, free_only=False):
    """Read the authorized episode details; never ask the publication store to save its pages."""
    from .errors import AccessDenied, MetadataError, NetworkUnavailable
    from .models import ArchiveError
    from .support_log import describe
    import logging

    site = next(site for site in sites.SITES if row['access_state'].startswith(site.key + '_'))
    kinds = wanted(inventory.db, site.key, row['id'])
    if not kinds:
        return
    try:
        budget.check()
        budget.access_check()
        adopt_existing(inventory, site.key, row, kinds, budget.check)
        kinds = wanted(inventory.db, site.key, row['id'])
        if not kinds:
            return
        manifest = adapter.capture_automatic(adapter.episode_url(row['id']))
        if (manifest.series.source != site.key or manifest.series.platform_id != row['series_id']
                or len(manifest.episodes) != 1 or manifest.episodes[0].id != row['id']):
            raise MetadataError('Episode details do not match the saved supplemental-content task.')
        episode = manifest.episodes[0]
        if episode.access not in sites.ACCESS or free_only and episode.access != site.free:
            raise AccessDenied('This episode is no longer available for supplemental-content collection.')
        extras.save_episode(inventory.root, inventory.db, budget, manifest.series, episode)
    except NetworkUnavailable:
        raise
    except (ArchiveError, OSError) as error:
        if extras._stops(error) or getattr(error, 'source_http_status', None) == 403:
            raise
        record(inventory.db, row['id'], kinds, False)
        logging.getLogger('comic_archive.download').info(
            'download extras: episode=%s collection_failed=%s saved_archive_retained=true', row['id'], describe(error))
