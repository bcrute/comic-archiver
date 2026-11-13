"""Shared display data for platform integrations, from one page of the saved-file catalog (saved_library).

Plain data in and out, no Qt: the table's rows, its storage places and its sentences are decided and tested here.
"""
from datetime import datetime
import math
import re

from .dashboard_model import display
from .messages import say

KINDS = (('episodes', say('library.comics_and_novels')), ('all', say('library.all_files')), ('metadata', say('library.data_files')), ('covers', say('library.covers')))
SORTS = (('saved_desc', say('library.newest_saved_first')), ('saved_asc', say('library.oldest_saved_first')), ('series', say('library.series_and_title')))
PAGE_SIZES = (50, 100, 200)
TYPES = {'cbz': say('library.comic'), 'epub': say('library.novel'), 'tar.zst': say('library.compressed_comic'), 'episode_metadata': say('library.data_file'),
         'series_metadata': say('library.data_file'), 'cover': say('library.cover')}
COLUMNS = (say('library.view.series'), say('library.title'), say('library.filename'), say('library.type'), say('library.size'), say('library.date_time_saved'), say('cloud.storage'), say('library.file'))
NOTE = (say('library.times_are_in_your_computers_time'))
LOADING = say('library.loading_saved_files')
UNAVAILABLE = say('app.saved_files_could_not_be_loaded')
OPEN_FAILED = say('app.this_file_could_not_be_opened')
NO_OPENER = say('library.no_app_could_open_this_file')
OPENING = say('library.opening_in_your_devices_default_app')
OPENED = say('library.sent_to_your_devices_default_app')
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
              [{'id': 'all', 'label': say('library.all_storage'), 'files': value.get('file_count'), 'episodes': value.get('episode_count'),
                'connected': True}, *locations])
    return [{'id': g.get('id'), 'label': g.get('label'), 'current': g.get('id') == current,
             'counts': f"{display(g.get('files'))} files · {display(g.get('episodes'))} episodes"
                       + (say('library.not_connected') if g.get('id') != 'all' and not g.get('connected') and not g.get('files') else '')}
            for g in groups]


def has_cloud(value, desktop_only):
    """Where nothing is in cloud storage, every file is on this computer: there is nothing to tell apart."""
    return not desktop_only and any(g.get('id') != 'device' and (g.get('files') or 0) > 0 for g in value.get('locations') or [])


def summary(value, desktop_only):
    device = next((g for g in value.get('locations') or [] if g.get('id') == 'device'), {})
    if desktop_only:
        return say('library.saved_episodes_across_files_on_this', n=display(device.get('episodes')), n2=display(device.get('files')))
    return (say('library.saved_episodes_across_files', n=display(value.get('episode_count')), n2=display(value.get('file_count')))
            + (say('library.and_cloud_copies') if has_cloud(value, desktop_only) else '') + '.')


def rows(value, desktop_only):
    """The table's rows. Only comics and novels on this computer are offered to open: those are what a reading app opens."""
    labels = {g.get('id'): g.get('label') for g in value.get('locations') or []}
    result = []
    for item in value.get('files') or []:
        when = saved_time(item.get('saved_at'))
        local = item.get('location') == 'device'
        opens = (local and item.get('kind') in ('cbz', 'epub') and isinstance(item.get('id'), str)
                 and re.fullmatch(r'[a-f0-9]{64}', item['id']) is not None)
        # Over every site, each file says which site it came from, under its series.
        series = item.get('series') or ''
        result.append({'series': f"{series}\n{item['site']}" if item.get('site') else series, 'site': item.get('site'), 'title': item.get('title') or '—', 'filename': item.get('filename') or '',
                       'type': TYPES.get(item.get('kind'), '—'), 'size': size(item.get('size')),
                       'saved': when[0] if when else '—', 'exact': when[1] if when else '',
                       'file_time': bool(when) and item.get('date_source') == 'file_modified',
                       'storage': labels.get(item.get('location')) or say('library.unknown_storage'),
                       'open': item['id'] if opens else None, 'action': '' if local else say('library.cloud_copy')})
    return result


def pages(value):
    """(this page, how many) for the page box."""
    limit = value.get('limit') or PAGE_SIZES[0]
    return math.floor((value.get('offset') or 0) / limit) + 1, max(1, math.ceil((value.get('total') or 0) / limit))


def range_text(value):
    total, offset = value.get('total') or 0, value.get('offset') or 0
    if total:
        return say('library.showing_of_files', n=display(offset + 1), n2=display(offset + len(value.get('files') or [])), n3=display(total))
    # With nothing saved yet the notice says so; that no file matches would say it twice.
    return say('library.no_saved_files_match_these_filters') if value.get('file_count') else ''


def notice(value):
    if value.get('total'):
        return say('library.saved_files_loaded')
    return (say('library.try_another_location_or_clear_the') if value.get('file_count')
            else say('library.no_files_have_been_saved_yet'))


def title(site):
    """The page's title: one site's library, or None for every site's."""
    return say('library.your_saved_library', site=site) if site else say('library.your_saved_library_all_sites')


def offset_for(page, limit):
    """Where a page asked for by number starts."""
    return (max(1, page) - 1) * limit
