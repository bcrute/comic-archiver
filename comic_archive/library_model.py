"""What the window's library page lists and says, from one page of the saved-file catalog (saved_library).

Plain data in and out, no Qt: the table's rows, its storage places and its sentences are decided and tested here.
"""
from datetime import datetime
import math
import re

from .dashboard_model import display

KINDS = (('episodes', 'Comics and novels'), ('all', 'All files'), ('metadata', 'Data files'), ('covers', 'Covers'))
SORTS = (('saved_desc', 'Newest saved first'), ('saved_asc', 'Oldest saved first'), ('series', 'Series and title'))
PAGE_SIZES = (50, 100, 200)
TYPES = {'cbz': 'Comic', 'epub': 'Novel', 'tar.zst': 'Compressed comic', 'episode_metadata': 'Data file',
         'series_metadata': 'Data file', 'cover': 'Cover'}
COLUMNS = ('Series', 'Title', 'Filename', 'Type', 'Size', 'Date/time saved', 'Storage', 'File')
NOTE = ('Times are in your computer’s time zone. “File time” means the day the file was saved is not known, '
        'so the day it last changed is shown.')
LOADING = 'Loading saved files…'
UNAVAILABLE = 'Saved files could not be loaded. Try Refresh files.'
OPEN_FAILED = 'This file could not be opened. Refresh files and try again.'
NO_OPENER = 'No app could open this file. Choose a default app for this file type on your device, then try again.'
OPENING = 'Opening in your device’s default app…'
OPENED = 'Sent to your device’s default app.'
MAX_QUERY = 200


def size(value):
    """Counted in 1024s, as Windows shows file sizes."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return '—'
    units, index = ('B', 'KB', 'MB', 'GB', 'TB'), 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f'{value:.1f} {units[index]}' if index else f'{value} {units[index]}'


def saved_time(text):
    """(shown, exact) for a saved time: the computer's own time zone for the eye, UTC for the tooltip; None if unknown."""
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            return None
    except (TypeError, ValueError):
        return None
    local = moment.astimezone()
    return f'{local.day} {local:%b %Y, %H:%M}', moment.isoformat()


def places(value, desktop_only, current):
    """The storage places to choose between, each with its counts; 'All storage' first where there is cloud storage."""
    locations = value.get('locations') or []
    groups = ([g for g in locations if g.get('id') == 'device'] if desktop_only else
              [{'id': 'all', 'label': 'All storage', 'files': value.get('file_count'), 'episodes': value.get('episode_count'),
                'connected': True}, *locations])
    return [{'id': g.get('id'), 'label': g.get('label'), 'current': g.get('id') == current,
             'counts': f"{display(g.get('files'))} files · {display(g.get('episodes'))} episodes"
                       + (' · Not connected' if g.get('id') != 'all' and not g.get('connected') and not g.get('files') else '')}
            for g in groups]


def has_cloud(value, desktop_only):
    """Where nothing is in cloud storage, every file is on this computer: there is nothing to tell apart."""
    return not desktop_only and any(g.get('id') != 'device' and (g.get('files') or 0) > 0 for g in value.get('locations') or [])


def summary(value, desktop_only):
    device = next((g for g in value.get('locations') or [] if g.get('id') == 'device'), {})
    if desktop_only:
        return f"{display(device.get('episodes'))} saved episodes across {display(device.get('files'))} files on this computer."
    return (f"{display(value.get('episode_count'))} saved episodes across {display(value.get('file_count'))} files"
            + (' and cloud copies' if has_cloud(value, desktop_only) else '') + '.')


def rows(value, desktop_only):
    """The table's rows. Only comics and novels on this computer are offered to open: those are what a reading app opens."""
    labels = {g.get('id'): g.get('label') for g in value.get('locations') or []}
    result = []
    for item in value.get('files') or []:
        when = saved_time(item.get('saved_at'))
        local = item.get('location') == 'device'
        opens = (local and item.get('kind') in ('cbz', 'epub') and isinstance(item.get('id'), str)
                 and re.fullmatch(r'[a-f0-9]{64}', item['id']) is not None)
        result.append({'series': item.get('series') or '', 'title': item.get('title') or '—', 'filename': item.get('filename') or '',
                       'type': TYPES.get(item.get('kind'), '—'), 'size': size(item.get('size')),
                       'saved': when[0] if when else '—', 'exact': when[1] if when else '',
                       'file_time': bool(when) and item.get('date_source') == 'file_modified',
                       'storage': labels.get(item.get('location')) or 'Unknown storage',
                       'open': item['id'] if opens else None, 'action': '' if local else 'Cloud copy'})
    return result


def pages(value):
    """(this page, how many) for the page box."""
    limit = value.get('limit') or PAGE_SIZES[0]
    return math.floor((value.get('offset') or 0) / limit) + 1, max(1, math.ceil((value.get('total') or 0) / limit))


def range_text(value):
    total, offset = value.get('total') or 0, value.get('offset') or 0
    if total:
        return f"Showing {display(offset + 1)}–{display(offset + len(value.get('files') or []))} of {display(total)} files."
    # With nothing saved yet the notice says so; that no file matches would say it twice.
    return 'No saved files match these filters.' if value.get('file_count') else ''


def notice(value):
    if value.get('total'):
        return 'Saved files loaded.'
    return ('Try another location or clear the search.' if value.get('file_count')
            else 'No files have been saved yet. Use Dashboard to start.')


def title(site):
    return f'Your saved {site} library'


def offset_for(page, limit):
    """Where a page asked for by number starts."""
    return (max(1, page) - 1) * limit
