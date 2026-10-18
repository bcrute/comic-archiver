"""Read-only file catalog from portable metadata and our own upload receipts.

Listing never opens archive contents or contacts a cloud provider. Private
account/configuration data cannot enter the fixed browser projection.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import parse_qs

from .archive import archive_extension, episode_archives, sidecar_path
from .models import ArchiveError
from .osdep import NOFOLLOW, NONBLOCK
from .publication import valid_export_id


def options(query):
    try:
        if len(query) > 2048:
            raise ValueError
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8, errors='strict')
        if set(values) - {'location','q','kind','sort','offset','limit','summary','refresh'} or any(len(v) != 1 for v in values.values()):
            raise ValueError
        values = {k:v[0] for k,v in values.items()}
        import re
        location = values.get('location','all')
        if not re.fullmatch(r'all|device|google|dropbox|proton|b2|(?:google|dropbox|proton|b2)-[a-f0-9]{16}', location):
            raise ValueError
        kind = values.get('kind','all'); sort = values.get('sort','saved_desc')
        if kind not in {'all','episodes','metadata','covers'} or sort not in {'saved_desc','saved_asc','series'}:
            raise ValueError
        query = values.get('q','')
        if len(query) > 200:
            raise ValueError
        for name in ('offset','limit'):
            if name in values and not re.fullmatch(r'[0-9]{1,8}', values[name]):
                raise ValueError
        offset = int(values.get('offset','0')); limit = int(values.get('limit','50'))
        if offset > 10000000 or not 1 <= limit <= 200:
            raise ValueError
        if any(values.get(k,'0') not in {'0','1'} for k in ('summary','refresh')):
            raise ValueError
        return dict(location=location, query=query, kind=kind, sort=sort, offset=offset, limit=limit,
                    summary=values.get('summary') == '1', refresh=values.get('refresh') == '1')
    except (ValueError, UnicodeError):
        raise ArchiveError('Choose supported saved-file filters.') from None


def text(value):
    return value[:2000] if isinstance(value, str) else ''


def date(value, fallback=None):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return datetime.fromtimestamp(fallback, timezone.utc).isoformat() if fallback is not None else None


def episode_order(value, archive):
    """Where an episode comes in its series: the saved number, else the number its file name starts with."""
    number = value.get('episode_number')
    if type(number) is int and number >= 0:
        return number
    match = re.match(r'([0-9]{1,8}) - ', archive.name)
    return int(match.group(1)) if match else float('inf')


def metadata(path, maximum=16*1024**2):
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
        try:
            stream = os.fdopen(fd, 'rb')
        except OSError:
            # A folder under this name opens but cannot be read: the handle is still ours to close.
            os.close(fd)
            return None
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                return None
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


class Catalog:
    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.cache = {}

    def export_id(self, workspace, path):
        relative = path.relative_to(workspace.library).as_posix()
        return hmac.new(self.key, f'export\0{workspace.id}\0{relative}'.encode(), hashlib.sha256).hexdigest()

    def snapshot(self, workspace, local, refresh=False):
        with self.lock:
            cached = self.cache.get(workspace.id)
            if not refresh and cached and time.monotonic() - cached[0] < 15:
                return cached[1]
            value = self.collect(workspace, local)
            self.cache[workspace.id] = (time.monotonic(), value)
            return value

    def collect(self, workspace, local):
        from . import cloud
        root = workspace.library
        rows, groups, episodes = [], {}, set()
        groups['device'] = {'id':'device', 'label':'This device' if local else 'Library server',
                            'connected':True, 'files':0, 'episodes':0, 'bytes':0}
        settings = cloud.settings(root) if local else None
        targets = settings['targets'] if settings else {}
        if local:
            # Cloud storage is listed only once it is set up; copies from an earlier connection are added below.
            for provider, label in cloud.LABELS.items():
                if provider not in targets:
                    continue
                groups[provider] = {'id':provider, 'label':label, 'connected':provider in targets,
                                    'files':0, 'episodes':0, 'bytes':0}

        def add(path, kind, series, title='', saved=None, episode=None, location='device', size=None, downloadable=True,
                order=-1):
            if location == 'device':
                if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
                    return
                try:
                    info = path.stat()
                except FileNotFoundError:
                    return  # An active upload may have just removed this copy.
                size = info.st_size
                timestamp = date(saved, info.st_mtime)
                identifier = self.export_id(workspace, path) if downloadable else None
            else:
                timestamp = date(saved); identifier = None
            # The order only sorts the list; it is taken out of the rows the page receives.
            rows.append({'id':identifier, 'filename':path.name, 'kind':kind, 'series':series,
                         'title':title, 'saved_at':timestamp, 'date_source':'saved' if date(saved) else 'file_modified',
                         'size':size, 'location':location, 'order':order})
            group = groups[location]; group['files'] += 1
            if size is not None: group['bytes'] += size
            if kind in {'cbz', 'epub', 'tar.zst'}:
                group['episodes'] += 1; episodes.add(episode)

        for folder in sorted(root.iterdir()):
            if folder.name.startswith('.') or folder.is_symlink() or not folder.is_dir():
                continue
            series = metadata(folder/'series.json', 256*1024) or {}
            series_title = text(series.get('title')) or folder.name
            archives = {sidecar_path(p):p for p in episode_archives(folder) if not p.is_symlink()}
            # Sidecars survive verified cloud offload; include those entries too.
            sidecars = set(archives)
            if settings:
                sidecars.update(p for p in folder.glob('*.json') if p.name != 'series.json')
            if not sidecars:
                continue
            local_archives = bool(archives)
            add(folder/'series.json', 'series_metadata', series_title, downloadable=local_archives)
            cover = series.get('cover_file')
            if isinstance(cover, str) and cover in {'cover.jpg','cover.png','cover.webp','cover.gif'}:
                add(folder/cover, 'cover', series_title, downloadable=local_archives)
            for sidecar in sorted(sidecars):
                value = metadata(sidecar)
                if not value:
                    continue
                extension = archive_extension(value)
                if extension not in {'.cbz', '.epub', '.tar.zst'}:
                    continue
                archive = archives.get(sidecar, sidecar.with_suffix(extension))
                title = text(value.get('title'))
                episode = archive.relative_to(root).as_posix()
                order = episode_order(value, archive)
                add(archive, extension[1:], series_title, title, value.get('imported_at'), episode, order=order)
                add(sidecar, 'episode_metadata', series_title, title, value.get('imported_at'),
                    downloadable=archive.is_file(), order=order)
                if not settings:
                    continue
                for provider in cloud.PROVIDERS:
                    identifier = value.get('export_id')
                    if not valid_export_id(identifier):
                        continue
                    try:
                        records = cloud.receipts_for(root, archive, provider)
                    except (ArchiveError, OSError, ValueError, TypeError, KeyError):
                        records = []
                    for record in records:
                        location = provider
                        target_id = record.get('target_id')
                        if target_id != targets.get(provider, {}).get('target_id'):
                            opaque = hmac.new(self.key, str(target_id).encode(), hashlib.sha256).hexdigest()[:16]
                            location = provider+'-'+opaque
                            if location not in groups:
                                label = cloud.LABELS[provider]
                                previous = 1+sum(k.startswith(provider+'-') for k in groups)
                                groups[location] = {'id':location, 'label':f'{label} (previous connection {previous})',
                                                    'connected':False, 'files':0, 'episodes':0, 'bytes':0}
                        for item in record['files']:
                            name = item['name']
                            if name.startswith('episode.') and name != 'episode.json':
                                kind = extension[1:]; item_title = title; item_order = order
                            elif name == 'episode.json':
                                kind = 'episode_metadata'; item_title = title; item_order = order
                            elif name == 'series.json':
                                kind = 'series_metadata'; item_title = ''; item_order = -1
                            elif name in {'cover.jpg','cover.png','cover.webp','cover.gif'}:
                                kind = 'cover'; item_title = ''; item_order = -1
                            else:
                                continue
                            add(Path(name), kind, series_title, item_title, record['verified_at'], episode,
                                location, downloadable=False, order=item_order)
        return {'rows':rows, 'locations':list(groups.values()), 'episode_count':len(episodes),
                'file_count':len(rows), 'updated_at':datetime.now(timezone.utc).isoformat()}

    def page(self, workspace, local, *, location='all', query='', kind='all', sort='saved_desc', offset=0, limit=50,
             summary=False, refresh=False):
        value = self.snapshot(workspace, local, refresh)
        if location not in {'all', *(g['id'] for g in value['locations'])}:
            raise ArchiveError('Choose an available storage location.')
        result = {k:value[k] for k in ('locations','episode_count','file_count','updated_at')}
        if summary:
            return result
        needle = query.casefold()
        rows = [r for r in value['rows'] if (location == 'all' or r['location'] == location)
                and (kind == 'all' or kind == 'episodes' and r['kind'] in {'cbz','epub','tar.zst'}
                     or kind == 'metadata' and r['kind'] in {'episode_metadata','series_metadata'}
                     or kind == 'covers' and r['kind'] == 'cover')
                and (not needle or needle in (' '.join((r['series'],r['title'],r['filename']))).casefold())]
        if sort == 'series':
            # Within a series, its own files first, then episodes in their order: Episode 2 before Episode 10.
            rows.sort(key=lambda r:(r['series'].casefold(), r['order'], r['title'].casefold(), r['filename'], r['location']))
        else:
            rows.sort(key=lambda r:(r['saved_at'] or '', r['series'], r['filename'], r['location']), reverse=sort == 'saved_desc')
        total = len(rows)
        offset = min(offset, ((total-1)//limit)*limit if total else 0)
        files = [{k:v for k,v in r.items() if k != 'order'} for r in rows[offset:offset+limit]]
        return result | {'files':files, 'total':total, 'offset':offset, 'limit':limit}
