"""Private SQLite inventory and durable queue; portable exports stay CBZ/EPUB/JSON."""
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from . import sites
from .archive import directory, verify_episode, episode_archives, sidecar_path
from .discovery import observed_id
from .errors import AuthenticationError, MetadataError
from .estimate import pace
from .locking import LibraryLock
from .models import ArchiveError
from .osdep import anchored
INVENTORY_VERSION=22
ASSET_RETRY_COOLDOWN_SECONDS = 300
# An episode a job failed on this many times is held as failed, so the queue goes on past it.
MAX_FAILED_ATTEMPTS = 3
UNSUPPORTED_REASONS=frozenset({'scheduled','novel','temporary_access','order_unverified',
                              'reader_manifest_incomplete','novel_manifest_incomplete','content_unsupported','ordering_unverified'})


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def observation_time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise MetadataError("Access observations require a timestamp with a timezone.") from None


def observation_epoch(value):
    """Nullable chronological key for validated dates in the private index."""
    try:return observation_time(value).timestamp()
    except (MetadataError,OverflowError,OSError):return None


def valid_order(value):
    return type(value) is int and value > 0


def failing(row):
    """An episode a job failed on is tried after the rest of the queue."""
    return row.get('status') == 'failed' and row.get('error_code') == 'job_failed'


def file_signature(path):
    a, b = path.stat(), sidecar_path(path).stat()
    return json.dumps([a.st_size, a.st_mtime_ns, b.st_size, b.st_mtime_ns])


class Inventory:
    def __init__(self, root, read_only=False):
        self.root = Path(root).absolute()
        self.lock = None
        self.db = None
        state = self.root / ".archive-state"
        path = state / "inventory.sqlite3"
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ArchiveError("Refusing a symlink in inventory output.")
        if read_only:
            if not path.is_file():
                raise ArchiveError("Inventory is not initialized; run archive-discovered or sync first.")
            self.db = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=2)
        else:
            self.lock = LibraryLock(self.root)
            self.lock.__enter__()
            try:
                directory(state).chmod(0o700)
                self.db = sqlite3.connect(path, timeout=2)
                path.chmod(0o600)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS series_links (url TEXT PRIMARY KEY,series_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS series_scans (series_id TEXT PRIMARY KEY,observed_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS series_scan_gaps (series_id TEXT PRIMARY KEY,
                        observed_at TEXT NOT NULL,code TEXT NOT NULL,observed_count INTEGER,reported_count INTEGER);
                    CREATE TABLE IF NOT EXISTS series (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL, provenance TEXT NOT NULL,
                        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS episodes (
                        id TEXT PRIMARY KEY, series_id TEXT NOT NULL, title TEXT NOT NULL,
                        episode_order INTEGER, access_state TEXT NOT NULL, observed_at TEXT NOT NULL,
                        status TEXT NOT NULL, error_code TEXT, archive_path TEXT, file_signature TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS episodes_by_series ON episodes(series_id);
                    CREATE TABLE IF NOT EXISTS observations (
                        episode_id TEXT NOT NULL, observed_at TEXT NOT NULL, access_state TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                        status TEXT NOT NULL, summary TEXT);
                    CREATE TABLE IF NOT EXISTS asset_deferrals (
                        episode_id TEXT PRIMARY KEY, retry_after TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS episode_expirations (
                        episode_id TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
                """)
                # Upgrade existing typed deferrals without restarting their wait.
                for key, seen in self.db.execute("SELECT id,observed_at FROM episodes WHERE error_code='asset_read_unavailable'").fetchall():
                    after = observation_time(seen) + timedelta(seconds=ASSET_RETRY_COOLDOWN_SECONDS)
                    self.db.execute('INSERT OR IGNORE INTO asset_deferrals VALUES (?,?)', (key, after.isoformat()))
                self.db.execute("UPDATE episodes SET status='queued', error_code='interrupted' WHERE status='downloading'")
                self.db.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE finished_at IS NULL", (timestamp(),))
                self.db.commit()
            except BaseException:
                self.close()
                raise
        self.db.row_factory = sqlite3.Row
        self.db.create_function('observation_epoch',1,observation_epoch,deterministic=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.__exit__(None, None, None)
            self.lock = None

    def bind_account(self, identity):
        if not isinstance(identity, str) or not identity:
            raise AuthenticationError("No verified account identity; log in before starting a job.")
        salt = self.db.execute("SELECT value FROM settings WHERE key='account_salt'").fetchone()
        if salt is None:
            salt_value = secrets.token_hex(32)
            self.db.execute("INSERT INTO settings VALUES ('account_salt', ?)", (salt_value,))
        else:
            salt_value = salt[0]
        binding = hmac.new(bytes.fromhex(salt_value), identity.encode(), hashlib.sha256).hexdigest()
        old = self.db.execute("SELECT value FROM settings WHERE key='account_binding'").fetchone()
        if old is not None and not hmac.compare_digest(old[0], binding):
            raise AuthenticationError("This library belongs to another account; use its session or a separate output library.")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('account_binding', ?)", (binding,))
        self.db.commit()

    def account_binding_state(self,identity):
        """Check the binding without creating one or changing private state."""
        if not isinstance(identity,str) or not identity:
            raise AuthenticationError('No verified account identity; log in before checking the session.')
        settings=dict(self.db.execute("SELECT key,value FROM settings WHERE key IN ('account_salt','account_binding')"))
        if not settings:
            return 'unbound'
        try:
            salt=bytes.fromhex(settings['account_salt'])
            previous=settings['account_binding']
            if len(salt)!=32 or not re.fullmatch(r'[a-f0-9]{64}',previous):
                raise ValueError
        except (KeyError,ValueError,TypeError):
            raise MetadataError('Account binding is incomplete or invalid; existing state is retained.') from None
        binding=hmac.new(salt,identity.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(previous,binding):
            raise AuthenticationError('This library belongs to another account; use its session or a separate output library.')
        return 'matches'

    def observe(self, series_id, title, episodes, provenance, observed_at=None):
        if not observed_id(series_id) or not isinstance(title, str):
            raise MetadataError("Inventory needs an observed numeric series ID and title.")
        seen = observed_at or timestamp()
        seen_time = observation_time(seen)
        old = self.db.execute("SELECT provenance,last_seen,title FROM series WHERE id=?", (series_id,)).fetchone()
        origins = sorted(set(json.loads(old[0]) if old else []) | {provenance})
        last_seen = seen
        if old and observation_time(old['last_seen']) > seen_time:
            last_seen, title = old['last_seen'], old['title']
        self.db.execute("""INSERT INTO series VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, provenance=excluded.provenance, last_seen=excluded.last_seen""",
            (series_id, title, json.dumps(origins), seen, last_seen))
        seen_ids = set()
        with self.db:
            for item in episodes:
                key = item.get("id")
                access = item.get("access_state")
                order = item.get("order")
                if (not observed_id(key) or key in seen_ids or not isinstance(item.get("title"), str)
                        or access not in sites.ACCESS | {"unavailable_or_unverified"}
                        or (order is not None and not valid_order(order))):
                    raise MetadataError("Inventory received invalid episode metadata.")
                seen_ids.add(key)
                old_episode = self.db.execute("SELECT * FROM episodes WHERE id=?", (key,)).fetchone()
                if old_episode is not None and old_episode["series_id"] != series_id:
                    raise MetadataError("Episode identity changed series during inventory refresh.")
                if old_episode is not None and observation_time(old_episode['observed_at']) > seen_time:
                    continue  # A dated seed must not overwrite a fresher access check.
                status = "queued" if access in sites.ACCESS else "unavailable"
                error = None
                reason=item.get('authorization_reason')
                if access=='unavailable_or_unverified' and isinstance(reason,str):
                    if reason in {'novel','scheduled','temporary_access'}:
                        status,error='unsupported',reason
                    elif reason=='access_denied':
                        error='access_denied'
                if status == "queued" and order is None:
                    status, error = "unsupported", "ordering_unverified"
                if old_episode is not None and old_episode["status"] == "archived":
                    status = "archived"
                elif (old_episode is not None and status == 'queued'
                        and old_episode['access_state'] == access
                        and old_episode['error_code'] == 'asset_read_unavailable'):
                    # An unchanged discovery refresh must not move a failing
                    # original ahead of the remaining healthy candidates.
                    error = 'asset_read_unavailable'
                elif (old_episode is not None and status == 'queued'
                        and old_episode['access_state'] == access and failing(dict(old_episode))):
                    status, error = 'failed', 'job_failed'  # Nor reset its failed tries.
                self.db.execute("""INSERT INTO episodes
                    (id,series_id,title,episode_order,access_state,observed_at,status,error_code)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, episode_order=excluded.episode_order,
                    access_state=excluded.access_state, observed_at=excluded.observed_at,
                    status=excluded.status, error_code=excluded.error_code""",
                    (key, series_id, item["title"], order, access, seen, status, error))
                if 'access_expires_at' in item:
                    from .expiry import expiration
                    expires=expiration(item['access_expires_at'])
                    if expires is not None and access in sites.ACCESS:
                        self.db.execute('INSERT INTO episode_expirations VALUES (?,?) ON CONFLICT(episode_id) DO UPDATE SET expires_at=excluded.expires_at',(key,expires))
                    else:
                        self.db.execute('DELETE FROM episode_expirations WHERE episode_id=?',(key,))
                if old_episode is None or old_episode["access_state"] != access:
                    self.db.execute("INSERT INTO observations VALUES (?,?,?)", (key, seen, access))
                if status in {'archived','unavailable','unsupported'} or (old_episode is not None and old_episode['access_state'] != access):
                    self.db.execute('DELETE FROM asset_deferrals WHERE episode_id=?', (key,))

    def record_access(self, key, access, *, expires_at=None):
        row = self.db.execute('SELECT * FROM episodes WHERE id=?',(key,)).fetchone()
        if row is None:
            raise MetadataError('Access check does not match an inventoried episode.')
        series = self.db.execute('SELECT title FROM series WHERE id=?',(row['series_id'],)).fetchone()
        self.observe(row['series_id'],series['title'],[{'id':key,'title':row['title'],
                     'order':row['episode_order'],'access_state':access,'access_expires_at':expires_at}], 'episode_check')

    def queue_priority(self):
        row=self.db.execute("SELECT value FROM settings WHERE key='queue_priority'").fetchone()
        from .expiry import queue_priority
        return queue_priority(row[0] if row else 'expiring_first')

    def set_queue_priority(self,value):
        from .expiry import queue_priority
        value=queue_priority(value)
        self.db.execute("INSERT INTO settings VALUES ('queue_priority',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))
        self.db.commit()
        return value

    def enable_novels(self):
        """Recheck old content holds without asserting that a chapter is readable."""
        with self.db:
            self.db.execute("UPDATE episodes SET status='queued',error_code='novel_access_unverified' "
                            "WHERE status='unsupported' AND error_code='novel'")
            self.db.execute("DELETE FROM series_scans WHERE series_id IN "
                            "(SELECT series_id FROM series_scan_gaps WHERE code='unsupported_novel_series')")
            self.db.execute("DELETE FROM series_scan_gaps WHERE code='unsupported_novel_series'")

    def enable_temporary_access(self):
        """One-time recheck of old holds; fresh authorization still decides access."""
        if self.db.execute("SELECT 1 FROM settings WHERE key='temporary_access_recheck_enabled'").fetchone(): return
        with self.db:
            self.db.execute("UPDATE episodes SET status='queued',error_code='temporary_access_recheck' WHERE status='unsupported' AND error_code='temporary_access'")
            self.db.execute("INSERT INTO settings VALUES ('temporary_access_recheck_enabled','1')")

    def known_series_link(self,url):
        row=self.db.execute('SELECT s.id,s.title FROM series_links l JOIN series s ON s.id=l.series_id WHERE l.url=?',(url,)).fetchone()
        return {'source_series_id':row['id'],'title':row['title']} if row else None

    def remember_series_link(self,url,key):
        if not observed_id(key):
            raise MetadataError('A series link needs a verified numeric identity.')
        old=self.db.execute('SELECT series_id FROM series_links WHERE url=?',(url,)).fetchone()
        if old and old[0]!=key:
            raise MetadataError('A known public series link changed identity; inspection is required.')
        self.db.execute('INSERT OR IGNORE INTO series_links VALUES (?,?)',(url,key))
        self.db.commit()

    def ingest_report(self, path):
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise MetadataError("Discovery report is unsupported or too large.")
        try:
            value = json.loads(path.read_text())
            if value.get("source") not in sites.KEYS or value.get("schema_version") != 1:
                raise ValueError
            for series in value["series"]:
                self.observe(series["source_series_id"], series["title"], series["accessible_episodes"],
                             "discovery_report", series["checked_at"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise MetadataError("Discovery report has an unsupported schema.") from None

    def mark(self, key, status, error=None, path=None, attempted=False):
        relative = path.relative_to(self.root).as_posix() if path is not None else None
        signature = file_signature(path) if path is not None and path.is_file() and status == "archived" else None
        self.db.execute("""UPDATE episodes SET status=?, error_code=?,
            archive_path=COALESCE(?,archive_path), file_signature=COALESCE(?,file_signature),
            attempts=attempts+? WHERE id=?""", (status, error, relative, signature, int(attempted), key))
        if status == 'queued' and error == 'asset_read_unavailable':
            after = observation_time(timestamp()) + timedelta(seconds=ASSET_RETRY_COOLDOWN_SECONDS)
            self.db.execute('INSERT INTO asset_deferrals VALUES (?,?) ON CONFLICT(episode_id) DO UPDATE SET retry_after=excluded.retry_after',
                            (key, after.isoformat()))
        elif status not in {'queued','failed','downloading'}:
            self.db.execute('DELETE FROM asset_deferrals WHERE episode_id=?', (key,))
        self.db.commit()

    def portable_archive_path(self,relative):
        if not isinstance(relative,str) or '\0' in relative:
            return None
        path=Path(relative)
        if (anchored(path) or len(path.parts)!=2 or not (path.suffix in {'.cbz', '.epub'} or path.name.endswith('.tar.zst'))
                or any(part in {'.','..'} or part.startswith('.') for part in path.parts)):
            return None
        result=self.root/path
        if any(p.is_symlink() for p in (result,*result.parents)):
            return None
        return result

    def archive_metadata(self,path):
        from .publication import import_metadata
        return import_metadata(sidecar_path(path))

    @staticmethod
    def archive_matches_row(metadata,row):
        return (metadata.get('source') in sites.KEYS and metadata.get('source_episode_id')==row['id']
                and str(metadata.get('source_series_id',metadata.get('series_id')))==row['series_id'])

    def reconcile(self, full=False):
        checked = 0
        # Check pinned archives first: malformed sidecars must not leave a row
        # marked archived merely because both files still exist.
        for row in self.db.execute("SELECT * FROM episodes WHERE status='archived'").fetchall():
            path = self.portable_archive_path(row['archive_path'])
            if path is None:
                self.mark(row['id'],'queued','archive_path_invalid')
                continue
            if not path.is_file() or not sidecar_path(path).is_file():
                if self.cloud_archive_matches(path, row):
                    continue
                self.mark(row["id"], "queued", "archive_missing")
                continue
            try:
                metadata=self.archive_metadata(path)
            except (ArchiveError,OSError,ValueError,TypeError):
                self.mark(row['id'],'queued','archive_corrupt')
                continue
            if not self.archive_matches_row(metadata,row):
                self.mark(row['id'],'queued','archive_identity_mismatch')
                continue
            if full or row["file_signature"] != file_signature(path):
                checked += 1
                if verify_episode(path):
                    self.mark(row["id"], "queued", "archive_corrupt")
                else:
                    self.mark(row["id"], "archived", path=path)
        public_series=(folder for folder in self.root.iterdir()
                       if not folder.name.startswith('.') and not folder.is_symlink() and folder.is_dir())
        for path in (archive for folder in public_series for archive in episode_archives(folder)):
            if self.portable_archive_path(path.relative_to(self.root).as_posix()) is None:
                continue
            try:
                meta = self.archive_metadata(path)
                key = meta.get("source_episode_id")
                if not observed_id(key):
                    continue
                row = self.db.execute("SELECT * FROM episodes WHERE id=?", (key,)).fetchone()
                if row is None or not self.archive_matches_row(meta,row):
                    continue
                signature = file_signature(path)
                if not full and row["status"] == "archived" and row["file_signature"] == signature:
                    continue
                checked += 1
                if verify_episode(path):
                    self.mark(key, "queued", "archive_corrupt")
                else:
                    self.mark(key, "archived", path=path)
            except (ArchiveError, OSError, ValueError, TypeError, KeyError):
                continue
        for row in self.db.execute("SELECT * FROM episodes WHERE status='archived'").fetchall():
            path = self.portable_archive_path(row['archive_path'])
            if path is None or not path.is_file() or not sidecar_path(path).is_file():
                if path is not None and self.cloud_archive_matches(path, row):
                    continue
                self.mark(row["id"], "queued", "archive_missing")
        # Publication can be uploaded/evicted before a crash allows the job
        # to mark its row archived. Match only retained private provenance and
        # a verified, journaled cloud deletion to the exact source identity.
        for folder in self.root.iterdir():
            if not folder.is_dir() or folder.name.startswith('.') or folder.is_symlink():continue
            for sidecar in folder.glob('*.json'):
                if sidecar.name == 'series.json':continue
                try:
                    metadata = self.archive_metadata(sidecar)
                    from .archive import archive_from_sidecar
                    archive = archive_from_sidecar(sidecar, metadata)
                    if archive.exists():continue
                    key = metadata.get('source_episode_id')
                    row = self.db.execute('SELECT * FROM episodes WHERE id=?',(key,)).fetchone()
                    if row and row['status'] != 'archived' and self.cloud_archive_matches(archive, row):
                        self.mark(key, 'archived', path=archive)
                except (ArchiveError,OSError,ValueError,TypeError,KeyError):
                    continue
        return checked

    def cloud_archive_matches(self, path, row):
        from .cloud import offloaded
        try:
            return bool(offloaded(self.root, path) and self.archive_matches_row(self.archive_metadata(path), row))
        except (OSError, ValueError, TypeError, ArchiveError):
            return False

    def completed_scan(self):
        self.db.execute("INSERT INTO settings VALUES ('last_completed_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (timestamp(),))
        self.db.commit()

    def scan_candidates(self,limit=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ArchiveError('Series scan limit must be a positive integer.')
        cycle = self.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone()
        if cycle is None:
            started = timestamp()
            self.db.execute("INSERT INTO settings VALUES ('scan_cycle_started',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(started,))
            self.db.commit()
        else:
            started = cycle[0]
            observation_time(started)
        held={gap['source_series_id'] for gap in self.scan_gaps() if gap['observed_at']>=started}
        rows = [dict(row) for row in self.db.execute('''SELECT s.id,s.title FROM series s
            LEFT JOIN series_scans scans ON scans.series_id=s.id
            WHERE scans.observed_at IS NULL OR scans.observed_at < ?
            ORDER BY COALESCE(scans.observed_at,''),s.id''',(started,))
            if row['id'] not in held]
        return rows if limit is None else rows[:limit]

    def scan_gaps(self):
        # Read-only dashboards can open a pre-upgrade inventory while its old
        # worker is active. Its next writer creates this additive table.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='series_scan_gaps'").fetchone():
            return []
        result=[]
        for row in self.db.execute('SELECT * FROM series_scan_gaps ORDER BY series_id'):
            if (observed_id(row['series_id']) and row['code']=='unsupported_novel_series'
                    and row['observed_count'] is None and row['reported_count'] is None):
                observation_time(row['observed_at'])
                result.append({'source_series_id':row['series_id'],'observed_at':row['observed_at'],
                               'code':row['code'],'unsupported_reason':'novel'})
                continue
            if (observed_id(row['series_id']) and row['code'] in {'metadata_http_404','metadata_http_410'}
                    and row['observed_count'] is None and row['reported_count'] is None):
                observation_time(row['observed_at'])
                result.append({'source_series_id':row['series_id'],'observed_at':row['observed_at'],
                    'code':row['code'],'source_http_status':int(row['code'].rsplit('_',1)[1])})
                continue
            if (observed_id(row['series_id']) and row['code']=='pagination_count_mismatch'
                    and type(row['observed_count']) is int and row['observed_count']>=0
                    and type(row['reported_count']) is int and row['reported_count']>=0):
                observation_time(row['observed_at'])
                result.append({'source_series_id':row['series_id'],'observed_at':row['observed_at'],
                    'code':row['code'],'observed_episode_count':row['observed_count'],
                    'reported_episode_count':row['reported_count']})
        return result

    def record_series_unavailable(self,key,http_status):
        if (not observed_id(key) or type(http_status) is not int or http_status not in {404,410}
                or self.db.execute('SELECT 1 FROM series WHERE id=?',(key,)).fetchone() is None):
            raise MetadataError('Cannot hold a series without an observed identity and an exact unavailable status.')
        self.db.execute('INSERT INTO series_scan_gaps VALUES (?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET '
            'observed_at=excluded.observed_at,code=excluded.code,observed_count=excluded.observed_count,reported_count=excluded.reported_count',
            (key,timestamp(),f'metadata_http_{http_status}',None,None))
        self.db.commit()

    def record_novel_series_hold(self,key):
        if (not observed_id(key)
                or self.db.execute('SELECT 1 FROM series WHERE id=?',(key,)).fetchone() is None):
            raise MetadataError('Cannot hold novel content without an observed series identity.')
        self.db.execute('INSERT INTO series_scan_gaps VALUES (?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET '
            'observed_at=excluded.observed_at,code=excluded.code,observed_count=excluded.observed_count,reported_count=excluded.reported_count',
            (key,timestamp(),'unsupported_novel_series',None,None))
        self.db.commit()

    def record_scan_gap(self,key,observed_count,reported_count):
        if (not observed_id(key) or type(observed_count) is not int or observed_count<0
                or type(reported_count) is not int or reported_count<0
                or self.db.execute('SELECT 1 FROM series WHERE id=?',(key,)).fetchone() is None):
            raise MetadataError('Cannot record an unverified series scan gap.')
        self.db.execute('INSERT INTO series_scan_gaps VALUES (?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET '
            'observed_at=excluded.observed_at,code=excluded.code,observed_count=excluded.observed_count,reported_count=excluded.reported_count',
            (key,timestamp(),'pagination_count_mismatch',observed_count,reported_count))
        self.db.commit()

    def record_series_scan(self,key):
        if not observed_id(key):
            raise MetadataError('Cannot record a scan with an invalid series identity.')
        if self.db.execute('SELECT 1 FROM series WHERE id=?',(key,)).fetchone() is None:
            raise MetadataError('Cannot record a scan of an unknown series.')
        self.db.execute('INSERT INTO series_scans VALUES (?,?) ON CONFLICT(series_id) DO UPDATE SET observed_at=excluded.observed_at',(key,timestamp()))
        self.db.execute('DELETE FROM series_scan_gaps WHERE series_id=?',(key,))
        self.db.commit()

    def finish_scan_cycle(self):
        cycle = self.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone()
        if cycle is None:
            return False
        remaining=len(self.scan_candidates())
        if remaining:
            return False
        if self.scan_gaps():
            self.db.execute("INSERT INTO settings VALUES ('last_attempted_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(timestamp(),))
        else:
            self.completed_scan()
        self.db.execute("DELETE FROM settings WHERE key='scan_cycle_started'")
        self.db.commit()
        return True

    def pending(self, free_only=False):
        if free_only:
            # Access observations are appended only on discovery/change.
            # Refreshing an unchanged free episode cannot renew its priority.
            now=observation_time(timestamp()).timestamp()
            rows = [dict(row) for row in self.db.execute('''
                SELECT e.*, COALESCE(recent.last_free >= ? - 86400,0) AS recent_free_candidate
                FROM episodes e LEFT JOIN (
                    SELECT episode_id, MAX(observation_epoch(observed_at)) AS last_free
                    FROM observations WHERE access_state IN '''+sites.sql_list(sites.FREE)+''' AND observation_epoch(observed_at)<=?
                    GROUP BY episode_id
                ) recent ON recent.episode_id=e.id
                WHERE e.status IN ('queued','failed') AND e.access_state IN '''+sites.sql_list(sites.FREE)+'''
                ORDER BY CASE WHEN e.error_code='asset_read_unavailable' THEN 1 ELSE 0 END,
                         recent_free_candidate DESC,
                         CASE WHEN recent.last_free >= ? - 86400 THEN recent.last_free END DESC,
                         e.series_id,e.episode_order,e.id
            ''',(now,now,now))]
        else:
            query = "SELECT * FROM episodes WHERE status IN ('queued','failed')"
        # Bulk preservation saves existing unlocks first. The independent
        # free-only daily policy above still prioritizes recent free changes.
            query += " ORDER BY CASE WHEN error_code='asset_read_unavailable' THEN 1 ELSE 0 END, CASE WHEN access_state IN "+sites.sql_list(sites.UNLOCKED)+" THEN 0 ELSE 1 END, series_id, episode_order, id"
            rows = [dict(row) for row in self.db.execute(query)]
        expirations={}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='episode_expirations'").fetchone():
            expirations=dict(self.db.execute('SELECT episode_id,expires_at FROM episode_expirations'))
            if free_only:
                from .expiry import daily_candidate
                rows += [dict(row) for row in self.db.execute("SELECT * FROM episodes WHERE status IN ('queued','failed') AND access_state IN "+sites.sql_list(sites.UNLOCKED))
                         if row['id'] in expirations and daily_candidate(row['access_state'],expirations[row['id']])]
        from .expiry import now as expiry_now
        current=expiry_now();priority=self.queue_priority()
        def rank(row):
            expiry=row['access_expires_at']
            urgent=expiry is not None and observation_time(expiry)>current
            if priority=='owned_first' and row['access_state'] in sites.UNLOCKED and expiry is None:
                return (0,'')
            if urgent: return (0 if priority=='expiring_first' else 1,expiry)
            return (2,'')
        for row in rows: row['access_expires_at']=expirations.get(row['id'])
        rows=[row for row in rows if not failing(row) or row['attempts']<MAX_FAILED_ATTEMPTS]
        # Stable: retain fair retries and recent-free order within each tier, with failing episodes last.
        return sorted(rows,key=lambda row:(failing(row),row['attempts'] if failing(row) else 0,rank(row)))

    def watched_series(self):
        return [dict(row) for row in self.db.execute("SELECT id,title FROM series ORDER BY id")]

    def archive_selection(self, rows, limit=None):
        """Admit one cooled CDN retry before healthy work; keep cooling rows queued."""
        now = observation_time(timestamp())
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_deferrals'").fetchone():
            waits = {key: observation_time(after) for key, after in self.db.execute('SELECT episode_id,retry_after FROM asset_deferrals')}
        else:
            # A read-only status client may open an inventory before its writer upgrades.
            waits = {row['id']: observation_time(row['observed_at']) + timedelta(seconds=ASSET_RETRY_COOLDOWN_SECONDS)
                     for row in rows if row['error_code'] == 'asset_read_unavailable'}
        healthy = [row for row in rows if row['id'] not in waits]
        eligible = sorted((row for row in rows if row['id'] in waits and waits[row['id']] <= now),
                          key=lambda row: (waits[row['id']], row['id']))
        selected = eligible[:1] + healthy
        if self.queue_priority()=='expiring_first':
            from .expiry import now as expiry_now
            urgent=[row for row in healthy if row.get('access_expires_at') is not None and not failing(row)
                    and observation_time(row['access_expires_at'])>expiry_now()]
            urgent_ids={row['id'] for row in urgent}
            selected=urgent+eligible[:1]+[row for row in healthy if row['id'] not in urgent_ids]
        if limit is not None:
            selected = selected[:limit]
        wait = None
        if rows and not selected:
            wait = max(1, min(ASSET_RETRY_COOLDOWN_SECONDS,
                             math.ceil(min((waits[row['id']] - now).total_seconds() for row in rows))))
        return selected, wait

    def start_run(self):
        run_id = secrets.token_hex(12)
        self.db.execute("INSERT INTO runs VALUES (?,?,NULL,'running',NULL)", (run_id, timestamp()))
        self.db.commit()
        return run_id

    def finish_run(self, run_id, status, summary):
        self.db.execute("UPDATE runs SET finished_at=?,status=?,summary=? WHERE id=?",
                        (timestamp(), status, json.dumps(summary), run_id))
        self.db.commit()

    def recent_runs(self):
        """Finished runs, newest first, as (started, finished, episodes saved) with times in seconds."""
        for row in self.db.execute("SELECT started_at,finished_at,summary FROM runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 500"):
            started, finished = observation_epoch(row[0]), observation_epoch(row[1])
            try:
                saved = json.loads(row[2])['archived_episode_ids']
            except (ValueError, TypeError, KeyError):
                saved = []
            if started is not None and finished is not None:
                yield started, finished, len(saved) if isinstance(saved, list) else 0

    def saved_bytes(self):
        """The room the saved episodes take, from the file sizes on record."""
        # A signature begins with the size of the episode's file: '[1234, ...'.
        return self.db.execute("""SELECT COALESCE(SUM(CAST(substr(file_signature,2) AS INTEGER)),0)
            FROM episodes WHERE status='archived' AND file_signature IS NOT NULL""").fetchone()[0]

    def status(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM episodes GROUP BY status")}
        unsupported = {}
        for code,count in self.db.execute("SELECT error_code,COUNT(*) FROM episodes WHERE status='unsupported' GROUP BY error_code"):
            reason=code if isinstance(code,str) and code in UNSUPPORTED_REASONS else 'content_unsupported'
            unsupported[reason]=unsupported.get(reason,0)+count
        last = self.db.execute("SELECT id,started_at,finished_at,status,summary FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        scan = self.db.execute("SELECT value FROM settings WHERE key='last_completed_scan'").fetchone()
        cycle = self.db.execute("SELECT value FROM settings WHERE key='scan_cycle_started'").fetchone()
        gaps=self.scan_gaps()
        held={gap['source_series_id'] for gap in gaps if cycle and gap['observed_at']>=cycle[0]}
        remaining = sum(row['id'] not in held for row in self.db.execute('''SELECT s.id FROM series s LEFT JOIN series_scans scans ON scans.series_id=s.id
            WHERE scans.observed_at IS NULL OR scans.observed_at < ?''',(cycle[0],))) if cycle else 0
        now=observation_time(timestamp())
        # Missing checkpoints cannot establish when an earlier observation was
        # complete. "never_checked" describes recorded successful checks only.
        freshness={'observed_at':now.isoformat(),'window_hours':24,'checked_within_24h':0,
                   'older_than_24h':0,'never_checked':0,'invalid_timestamp_count':0,
                   'oldest_successful_check':None}
        oldest=None
        for row in self.db.execute('''SELECT scans.observed_at FROM series s
                LEFT JOIN series_scans scans ON scans.series_id=s.id'''):
            if row[0] is None:
                freshness['never_checked']+=1
                continue
            try:
                checked=observation_time(row[0])
                if checked>now:raise MetadataError('A series check cannot occur in the future.')
            except MetadataError:
                freshness['invalid_timestamp_count']+=1
                continue
            oldest=checked if oldest is None else min(oldest,checked)
            freshness['checked_within_24h' if checked>=now-timedelta(hours=24) else 'older_than_24h']+=1
        freshness['oldest_successful_check']=oldest.isoformat() if oldest else None
        return {"inventory_complete": False, "last_completed_scan": scan[0] if scan else None,
                'queue_priority':self.queue_priority(),
                'scan_freshness':freshness,
                'scan_cycle_started':cycle[0] if cycle else None,'scan_remaining_series_count':remaining,
                'series_scan_gap_count':len(gaps),'series_scan_gaps':gaps,
                'series_without_scan_checkpoint_count': self.db.execute('''SELECT COUNT(*) FROM series s
                    LEFT JOIN series_scans scans ON scans.series_id=s.id WHERE scans.series_id IS NULL''').fetchone()[0],
                'series_without_episode_observations_count': self.db.execute('''SELECT COUNT(*) FROM series s
                    WHERE NOT EXISTS (SELECT 1 FROM episodes e WHERE e.series_id=s.id)''').fetchone()[0],
                "series_count": self.db.execute("SELECT COUNT(*) FROM series").fetchone()[0],
                "episode_count": sum(counts.values()), "episodes_by_status": counts, 'unsupported_by_reason':unsupported,
                'recent_pace': pace(self.recent_runs()), 'saved_bytes': self.saved_bytes(),
                "last_run": dict(last) | {"summary": json.loads(last["summary"]) if last["summary"] else None} if last else None}
