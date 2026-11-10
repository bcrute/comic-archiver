"""Paged, read-only episode indicators from the acquisition inventory."""
from pathlib import Path

from .discovery import observed_id
from .inventory import Inventory
from .models import ArchiveError
from .download_selection import access_kind

PAGE_SIZE = 200


def recorded_format(path):
    """Use an existing index path suffix; never open or inspect the file."""
    if not isinstance(path, str):
        return None
    for suffix, name in (('.cbz', 'CBZ'), ('.epub', 'EPUB'), ('.tar.zst', 'TAR.ZST')):
        if path.lower().endswith(suffix):
            return name
    return None


def page(root, series_id=None, offset=0, limit=PAGE_SIZE):
    """No provider calls, directory enumeration, reconciliation or archive reads."""
    if (type(offset) is not int or not 0 <= offset <= 1_000_000
            or type(limit) is not int or not 1 <= limit <= PAGE_SIZE
            or series_id is not None and not observed_id(series_id)):
        raise ArchiveError('Choose a recorded series and a supported episode-list page.')
    root = Path(root)
    database = root / '.archive-state' / 'inventory.sqlite3'
    if not database.is_file():
        return {'initialized': False, 'items': [], 'next_offset': None}
    with Inventory(root, read_only=True) as inventory:
        expirations = {}
        if inventory.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='episode_expirations'").fetchone():
            expirations = dict(inventory.db.execute('SELECT episode_id,expires_at FROM episode_expirations'))
        inventory.db.create_function('recorded_format', 1, recorded_format, deterministic=True)
        inventory.db.create_function('recorded_access', 2,
            lambda mark, key: access_kind(mark, expirations.get(key)) if key is not None else None,
            deterministic=True)
        if series_id is None:
            rows = inventory.db.execute('''SELECT s.id,s.title,COUNT(e.id) AS episodes,
                COALESCE(SUM(e.status='archived'),0) AS saved,
                GROUP_CONCAT(DISTINCT recorded_format(e.archive_path)) AS formats,
                GROUP_CONCAT(DISTINCT recorded_access(e.access_state,e.id)) AS access_kinds FROM series s
                LEFT JOIN episodes e ON e.series_id=s.id GROUP BY s.id
                ORDER BY s.title COLLATE NOCASE,s.id LIMIT ? OFFSET ?''', (limit + 1, offset)).fetchall()
        else:
            if inventory.db.execute('SELECT 1 FROM series WHERE id=?', (series_id,)).fetchone() is None:
                raise ArchiveError('This series is no longer recorded in the library.')
            rows = inventory.db.execute('''SELECT id,title,episode_order AS episode_number,status,access_state,
                recorded_format(archive_path) AS format,recorded_access(access_state,id) AS access_kind
                FROM episodes WHERE series_id=?
                ORDER BY episode_order IS NULL,episode_order,id LIMIT ? OFFSET ?''',
                (series_id, limit + 1, offset)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        if series_id is None:
            for item in items:
                for field in ('formats', 'access_kinds'):
                    item[field] = sorted(item[field].split(',')) if item[field] else []
        return {'initialized': True, 'items': items,
                'next_offset': offset + limit if len(rows) > limit else None}
