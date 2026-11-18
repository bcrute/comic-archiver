"""What a site shows beside an episode: the creator's note, its music, its counts and its readers' comments.

Each is saved only when chosen, and every one starts off. An episode the account can still read can have its
extras collected after its archive was saved, and only what the site shows its reader on that episode's page.

They are kept in the library's own private folder, apart from the portable files:
<library>/.archive-state/extras/<site>/<series>/<episode>.json, with any music beside it. Nothing that lists,
checks, uploads or imports a library looks there, so an older version or the phone reads the library as before.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from . import sites
from .messages import say
from .models import ArchiveError, identifier

KEY = 'download_extras'
KINDS = ('notes', 'music', 'stats', 'comments')
STATS = ('views', 'likes', 'comments')
NOTE_CHARACTERS = 20000
COMMENT_CHARACTERS = 4000
NAME_CHARACTERS = 200
MAX_TRACKS = 4
MAX_COMMENTS = 300
MUSIC_BYTES = 30 * 1024 * 1024
LOG = logging.getLogger('comic_archive.download')


def offered(site_key):
    """The extras this site shows, in order; none for a site that is not known."""
    site = next((site for site in sites.SITES if site.key == site_key), None)
    return tuple(kind for kind in KINDS if site is not None and kind in site.extras)


def label(kind):
    return {'notes': say('extras.notes'), 'music': say('extras.music'), 'stats': say('extras.stats'),
            'comments': say('extras.comments')}[kind]


def help_text(kind):
    return {'notes': say('extras.notes_help'), 'music': say('extras.music_help'), 'stats': say('extras.stats_help'),
            'comments': say('extras.comments_help')}[kind]


def validate(value):
    """Every extra named, each on or off."""
    if not isinstance(value, dict) or set(value) != set(KINDS) or any(type(value[key]) is not bool for key in KINDS):
        raise ArchiveError('Choose which extras to save.')
    return {key: value[key] for key in KINDS}


def read(db):
    """The saved choice; all off when none was made. A choice that cannot be read saves nothing extra."""
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return dict.fromkeys(KINDS, False)
    try:
        return validate(json.loads(row[0]))
    except (ValueError, TypeError, RecursionError, ArchiveError):
        LOG.warning('download extras: saved choice unreadable; nothing extra is saved until it is chosen again')
        return dict.fromkeys(KINDS, False)


def store(db, value):
    """Write the choice inside the caller's transaction."""
    value = validate(value)
    if value != read(db) and db.execute("SELECT 1 FROM sqlite_master WHERE name='extras_collections'").fetchone():
        # A deliberate choice change gives failed extras another bounded chance, keeping successes.
        db.execute('DELETE FROM extras_collections WHERE done=0')
    db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (KEY, json.dumps(value, sort_keys=True)))
    LOG.info('download extras saved: ' + ' '.join(f'{key}=%s' for key in KINDS), *(value[key] for key in KINDS))
    return value


def save(db, value):
    with db:
        return store(db, value)


def text(value, limit):
    """Plain words as the site gave them, with their line breaks; None when there are none."""
    if not isinstance(value, str):
        return None
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value.replace('\r\n', '\n').replace('\r', '\n'))
    value = re.sub(r'\n{3,}', '\n\n', '\n'.join(line.rstrip() for line in value.split('\n'))).strip()
    return value[:limit] or None


def count(value):
    """A count the site shows, or None for anything that is not one."""
    return value if type(value) is int and 0 <= value < 10 ** 12 else None


def counts(**values):
    found = {name: count(values.get(name)) for name in STATS}
    return {name: value for name, value in found.items() if value is not None} or None


def found(note=None, stats=None, music=(), comments=False):
    """What one capture saw, bounded, for save() to keep what was chosen."""
    tracks = []
    for url, title in music:
        if isinstance(url, str) and url.startswith('https://') and len(url) <= 2048 and len(tracks) < MAX_TRACKS:
            tracks.append({'url': url, 'title': text(title, NAME_CHARACTERS)})
    return {'note': text(note, NOTE_CHARACTERS), 'stats': stats if isinstance(stats, dict) and stats else None,
            'music': tracks, 'comments': comments is True}


class ElementText(HTMLParser):
    """The words of the first element carrying one class, with <br> and paragraph breaks as new lines."""
    BREAKS = {'br', 'p', 'div', 'li'}
    EMPTY = {'br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'wbr'}

    def __init__(self, class_name):
        super().__init__(convert_charrefs=True)
        self.class_name, self.depth, self.parts, self.done = class_name, 0, [], False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth:
            if tag in self.BREAKS:
                self.parts.append('\n')
            if tag not in self.EMPTY:
                self.depth += 1
        elif self.class_name in (dict(attrs).get('class') or '').split() and tag not in self.EMPTY:
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and not self.done and tag not in self.EMPTY:
            self.depth -= 1
            if not self.depth:
                self.done = True

    def handle_data(self, data):
        if self.depth and not self.done:
            self.parts.append(data)

    @classmethod
    def of(cls, html, class_name, limit=NOTE_CHARACTERS):
        parser = cls(class_name)
        parser.feed(html)
        parser.close()
        return text(''.join(parser.parts), limit)


def audio_extension(data):
    """The kind of sound file these bytes are, by their first bytes; None for anything else."""
    if data[:3] == b'ID3' or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return '.mp3'
    if data[4:8] == b'ftyp':
        return '.m4a'
    if data[:4] == b'OggS':
        return '.ogg'
    if data[:4] == b'fLaC':
        return '.flac'
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return '.wav'
    return None


def folder(root, source, series_key):
    identifier(series_key)
    if source not in sites.KEYS:
        raise ArchiveError('Extras belong to a known site.')
    return root / '.archive-state' / 'extras' / source / series_key


def _write(path, data):
    from .osdep import open_exclusive, replace_file
    temporary = path.with_name(path.name + '.tmp')
    if path.is_symlink() or temporary.is_symlink():
        raise ArchiveError('Refusing a symlink in extras output.')
    with os.fdopen(open_exclusive(temporary), 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    replace_file(temporary, path)


def _stops(error):
    """A sign-in lost, a limit or pause, the library gone, the connection no longer allowed, or the site asking to
    slow down stops the job. Anything else loses only that extra."""
    from .errors import AuthenticationError, LibraryMoved, RunLimit
    from .network import NetworkPolicyHold
    return (isinstance(error, (RunLimit, AuthenticationError, LibraryMoved, NetworkPolicyHold))
            or getattr(error, 'source_http_status', None) == 429)


def save_episode(root, db, source, series, episode):
    """Keep the chosen extras of an episode just saved; the path written, or None when nothing was chosen or seen.

    source answers episode_extras(id) and, for what was chosen, retrieve_extra(url, max_bytes) and
    episode_comments(id, limit): the job's budget, so music and comment pages count toward its limits.
    An extra that cannot be had is left out and logged; the episode itself is already saved, so only a sign-in
    lost, a limit or pause, or the library gone stops the job."""
    try:
        return _save_episode(root, db, source, series, episode)
    except (ArchiveError, OSError) as error:
        if _stops(error):
            raise
        from .extras_backfill import record, wanted
        record(db, episode.id, wanted(db, series.source, episode.id), False)
        LOG.info('download extras: episode=%s saved=false error=%s', episode.id, type(error).__name__)
        return None


def saved_value(root, source, series_key, episode_key):
    """Read an existing supplemental record only inside its expected episode folder."""
    identifier(episode_key)
    path = folder(root, source, series_key) / f'{episode_key}.json'
    value = {}
    if path.exists() or path.is_symlink():
        if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > 2_000_000:
            raise ArchiveError('The saved extras cannot be safely updated.')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, RecursionError):
            raise ArchiveError('The saved extras could not be read; they are retained.') from None
        if (not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('site') != source
                or value.get('series') != series_key or value.get('episode') != episode_key):
            raise ArchiveError('The saved extras belong to another episode; they are retained.')
    return value


def _save_episode(root, db, source, series, episode):
    from .archive import atomic_json, directory
    from .extras_backfill import record, wanted as missing
    wanted = missing(db, series.source, episode.id)
    if not wanted:
        return None
    seen = getattr(source, 'episode_extras', None)
    seen = seen(episode.id) if callable(seen) else None
    if not isinstance(seen, dict):
        LOG.info('download extras: episode=%s seen=false', episode.id)
        record(db, episode.id, wanted, False)
        return None
    series_key = series.platform_id or series.id
    place = folder(root, series.source, series_key)
    now = datetime.now(timezone.utc).isoformat()
    path = place / f'{episode.id}.json'
    value = saved_value(root, series.source, series_key, episode.id)
    value.update(schema_version=1, site=series.source, series=series_key, episode=episode.id,
                 title=episode.title, source_url=episode.source_url, saved_at=now)
    failed = set()
    if 'notes' in wanted and seen.get('note'):
        value['creator_note'] = seen['note']
    if 'stats' in wanted and seen.get('stats'):
        value['stats'] = dict(seen['stats'], seen_at=now)
    if 'music' in wanted and seen.get('music'):
        tracks = []
        for number, track in enumerate(seen['music'], 1):
            try:
                data = source.retrieve_extra(track['url'], MUSIC_BYTES)
                extension = audio_extension(data or b'')
                if extension is None:
                    raise ArchiveError('The music is not a sound file.')
                directory(place, root)
                name = f'{episode.id}.music-{number}{extension}'
                _write(place / name, data)
                tracks.append({'title': track['title'], 'file': name, 'size': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()})
            except (ArchiveError, OSError) as error:
                if _stops(error):
                    raise
                failed.add('music')
                LOG.info('download extras: episode=%s music=%s saved=false error=%s', episode.id, number, type(error).__name__)
        if tracks:
            value['music'] = tracks
    if 'comments' in wanted and seen.get('comments'):
        try:
            listed = source.episode_comments(episode.id, MAX_COMMENTS)
            if isinstance(listed, dict) and isinstance(listed.get('comments'), list):
                value['comments'] = listed['comments'][:MAX_COMMENTS]
                value['comments_total'] = count(listed.get('total'))
                value['comments_complete'] = listed.get('complete') is True
            else:
                failed.add('comments')
        except ArchiveError as error:
            if _stops(error):
                raise
            failed.add('comments')
            LOG.info('download extras: episode=%s comments saved=false error=%s', episode.id, type(error).__name__)
    kept = [key for key in ('creator_note', 'stats', 'music', 'comments') if key in value]
    if not kept:
        record(db, episode.id, set(wanted) - failed, True)
        record(db, episode.id, failed, False)
        return None
    directory(place, root)
    atomic_json(path, value)
    record(db, episode.id, set(wanted) - failed, True)
    record(db, episode.id, failed, False)
    LOG.info('download extras: episode=%s saved=%s', episode.id, ','.join(kept))
    return path
