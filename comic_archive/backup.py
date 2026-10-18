"""Incremental, verified copies of portable exports; no source or session access."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .archive import atomic_json, verify_episode, verify_series, episode_archives, offloaded_archive, sidecar_path
from .archive_paths import directory
from .locking import LibraryLock, wait_for_library
from .models import ArchiveError
from .osdep import private_file, replace_file, sync_directory


def _regular(path):
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ArchiveError('Backup requires ordinary portable files without symlinks.')
    return path


def _hash(path):
    with _regular(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _checkpoint(path, value):
    atomic_json(path, value)
    path.chmod(0o600)
    sync_directory(path.parent)


def _series_files(folder):
    path = _regular(folder / 'series.json')
    if path.stat().st_size > 256 * 1024 or verify_series(path):
        raise ArchiveError('Source series metadata or cover failed verification; backup retained.')
    metadata = json.loads(path.read_text())
    archives = episode_archives(folder)
    local = {sidecar_path(archive) for archive in archives}
    # Uploaded, then removed here: only the sidecar is copied, describing the archive kept in the cloud.
    remote = [p for p in sorted(folder.glob('*.json'))
              if p.name != 'series.json' and p not in local and offloaded_archive(folder.parent, p)]
    if (type(metadata.get('episode_count')) is not int
            or metadata['episode_count'] != len(archives) + len(remote)):
        raise ArchiveError('Source series episode count differs from its exports; backup retained.')
    files = []
    for archive in archives:
        _regular(archive); _regular(sidecar_path(archive))
        if verify_episode(archive):
            raise ArchiveError('Source episode failed verification; backup retained.')
        files.extend((archive, sidecar_path(archive)))
    files.extend(_regular(p) for p in remote)
    cover = metadata.get('cover_file')
    if cover is not None:
        files.append(_regular(folder / cover))
    # Publish series metadata after its covers/episodes, never before them.
    return files + [path], len(archives), len(remote)


def backup_library(source, destination, *, wait_seconds=0, reserve_bytes=1024**3, progress=None):
    raw_paths = (Path(source).absolute(), Path(destination).absolute())
    if any(p.is_symlink() for path in raw_paths for p in (path, *path.parents)):
        raise ArchiveError('Choose a separate backup directory without symlinked paths.')
    source, destination = (Path(os.path.abspath(path)) for path in raw_paths)
    if (not source.is_dir() or destination == source or source in destination.parents
            or destination in source.parents
            or any(p.is_symlink() for path in (source, destination) for p in (path, *path.parents))):
        raise ArchiveError('Choose a separate backup directory without symlinked paths.')
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ArchiveError('Backup free-space reserve must be a nonnegative integer.')
    marker = destination / '.archive-state/backup.json'
    if destination.exists() and not marker.exists() and any(destination.iterdir()):
        raise ArchiveError('Backup destination is not an empty directory or a recognized backup.')
    with wait_for_library(source, wait_seconds), LibraryLock(destination):
        destination.chmod(0o700)
        previous = None
        if marker.exists():
            try:
                _regular(marker)
                if marker.stat().st_size > 64 * 1024**2:
                    raise ValueError
                previous = json.loads(marker.read_text())
                if (previous.get('document_type') != 'portable_archive_backup'
                        or previous.get('schema_version') != 1
                        or previous.get('source_identity') != hashlib.sha256(str(source).encode()).hexdigest()):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ArchiveError('Backup checkpoint is invalid or belongs to another library.') from None
        else:
            # Recognize even an interrupted first attempt without adopting an
            # unrelated nonempty directory on a later retry.
            _checkpoint(marker, {'schema_version': 1, 'document_type': 'portable_archive_backup',
                                'source_identity': hashlib.sha256(str(source).encode()).hexdigest(),
                                'complete': False, 'files': []})
        folders = sorted(p for p in source.iterdir() if not p.name.startswith('.') and p.is_dir())
        selection, episodes, offloaded, series = [], 0, 0, 0
        for folder in folders:
            if folder.is_symlink():
                raise ArchiveError('Refusing a symlinked source series.')
            if not (folder / 'series.json').exists():
                if episode_archives(folder):
                    raise ArchiveError('A source series is missing portable metadata.')
                continue
            files, count, remote = _series_files(folder)
            if not count and not remote:
                continue
            selection.extend(files); episodes += count; offloaded += remote; series += 1
        if not episodes and not offloaded:
            raise ArchiveError('No verified portable episode exports were found.')
        expected = {p.relative_to(source).as_posix() for p in selection}
        existing = {p.relative_to(destination).as_posix() for folder in destination.iterdir()
                    if folder.is_dir() and not folder.name.startswith('.')
                    for p in folder.iterdir()}
        if existing - expected:
            raise ArchiveError('Backup contains files absent from the source; nothing was deleted. Choose a new snapshot directory.')
        records, changed = [], []
        for path in selection:
            relative = path.relative_to(source)
            target = destination / relative
            signature = {'file': relative.as_posix(), 'size': path.stat().st_size, 'sha256': _hash(path)}
            records.append(signature)
            if target.exists() or target.is_symlink():
                _regular(target)
            if not target.exists() or target.stat().st_size != signature['size'] or _hash(target) != signature['sha256']:
                changed.append((path, target, signature))
        bytes_needed = sum(record['size'] for _, _, record in changed)
        if shutil.disk_usage(destination).free < bytes_needed + reserve_bytes:
            raise ArchiveError('Backup has insufficient free space; existing files were retained.')
        report = {'schema_version': 1, 'document_type': 'portable_archive_backup',
                  'source_identity': hashlib.sha256(str(source).encode()).hexdigest(),
                  'checked_at': datetime.now(timezone.utc).isoformat(), 'complete': False,
                  'archive_count': episodes, 'offloaded_episode_count': offloaded, 'series_count': series,
                  'file_count': len(records), 'copied_files': 0, 'copied_bytes': 0,
                  'reused_files': len(records) - len(changed),
                  'total_portable_bytes': sum(r['size'] for r in records),
                  'different_filesystem': source.stat().st_dev != destination.stat().st_dev,
                  'cache_or_session_copied': False, 'files': records}
        _checkpoint(marker, report)
        staging = directory(destination / '.archive-state/backup-staging')
        try:
            for path, target, record in changed:
                directory(target.parent)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=staging, delete=False) as stream:
                        temporary = Path(stream.name)
                        private_file(stream.fileno())
                        with path.open('rb') as incoming:
                            shutil.copyfileobj(incoming, stream, length=1024**2)
                        stream.flush(); os.fsync(stream.fileno())
                    if temporary.stat().st_size != record['size'] or _hash(temporary) != record['sha256']:
                        raise ArchiveError('Copied bytes failed verification; existing backup retained.')
                    # Check again before replacing a path, including its ancestors.
                    if any(p.is_symlink() for p in (target, *target.parents)):
                        raise ArchiveError('Refusing a symlinked backup destination.')
                    replace_file(temporary, target)
                    sync_directory(target.parent)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                report['copied_files'] += 1; report['copied_bytes'] += record['size']
                if progress and (report['copied_files'] % 100 == 0 or report['copied_files'] == len(changed)):
                    progress({'copied_files': report['copied_files'], 'copied_bytes': report['copied_bytes'],
                              'remaining_files': len(changed) - report['copied_files']})
            report['complete'] = True
            _checkpoint(marker, report)
            return {key: value for key, value in report.items() if key not in {'files', 'source_identity'}}
        except (OSError, ArchiveError):
            _checkpoint(marker, report)
            raise ArchiveError('Backup interrupted; verified files retained. Run the same command to resume.') from None
