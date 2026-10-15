"""Offline, resumable conversion of neutral tar.zst comics back to standard CBZ."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tarfile
import zipfile
import zstandard

from .archive import atomic_json, digest, sidecar_path, verify_episode_contents, verify_series
from .archive_paths import directory
from .errors import RunLimit
from .inventory import Inventory
from .models import ArchiveError
from .osdep import anchored, replace_file, sync_directory
from .publication import (PublicationStore, StrictZstdReader, comic_pages, import_metadata,
                          metadata_hash, public_metadata, validate_public_metadata, valid_export_id)
from .purge import register_archive
from .storage import safe_root


def _write(path, value):
    atomic_json(path, value); path.chmod(0o600)


def _finish(store, record):
    root = store.root
    if (not isinstance(record,dict) or set(record) != {'schema_version','original','converted','metadata','original_stat','remove_originals'}
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or type(record['remove_originals']) is not bool
            or any(not isinstance(record[k],str) or anchored(record[k]) or '..' in Path(record[k]).parts
                   for k in ('original','converted'))):
        raise ArchiveError('Invalid CBZ conversion checkpoint; originals retained.')
    old = safe_root(root / record['original']); new = safe_root(root / record['converted'])
    if (old.parent != new.parent or not old.name.endswith('.tar.zst')
            or new.name != old.name[:-8]+'.cbz' or old.is_symlink() or new.is_symlink()
            or len(old.relative_to(root).parts) != 2 or old.relative_to(root).parts[0].startswith('.')):
        raise ArchiveError('Unsafe CBZ conversion paths; originals retained.')
    original = record['metadata']
    if (not isinstance(original,dict) or original.get('format') != 'tar.zst'
            or not valid_export_id(original.get('export_id'))):
        raise ArchiveError('Invalid original comic identity; originals retained.')
    converted = original | {'format':'cbz'}
    public = public_metadata(converted, converted['export_id'])
    validate_public_metadata(public)
    if not new.exists():
        if not old.is_file(): raise ArchiveError('Original is unavailable for conversion recovery.')
        _convert(old,original,new)
    with new.open('rb') as source:
        if verify_episode_contents(source, public):
            raise ArchiveError('Converted CBZ failed verification; original retained.')
    if old.exists():
        current = old.stat()
        if record['original_stat'] != [current.st_size,current.st_mtime_ns,current.st_ino]:
            raise ArchiveError('Original comic changed during conversion; both copies retained.')
    sidecar = sidecar_path(new)
    private = store.provenance / (converted['export_id']+'.json')
    _write(private, {'schema_version':1,'sidecar':sidecar.relative_to(root).as_posix(),
                     'public_hash':metadata_hash(public),'metadata':converted})
    atomic_json(sidecar,public)
    register_archive(store,new,converted)  # Recovery is durable before removal.
    store.db.execute('UPDATE episode_paths SET path=? WHERE path=?',
                     (new.relative_to(root).as_posix(),old.relative_to(root).as_posix()))
    store.db.execute('DELETE FROM archived_pages WHERE path=?',(old.relative_to(root).as_posix(),))
    store.db.commit()
    with Inventory(root) as inventory:
        row = inventory.db.execute('SELECT * FROM episodes WHERE id=?',(converted['source_episode_id'],)).fetchone()
        if row is not None:
            if not inventory.archive_matches_row(converted,row):
                raise ArchiveError('CBZ identity differs from inventory; originals retained.')
            inventory.mark(row['id'],'archived',path=new)
    if record['remove_originals']:
        if old.exists(): old.unlink()
    else:
        folder = directory(root/'.archive-state/legacy-exports'/old.parent.name); folder.chmod(0o700)
        retained = folder/old.name
        # A full private manifest keeps the retired tar independently verifiable.
        _write(sidecar_path(retained),original)
        if old.exists():
            if retained.exists():
                raise ArchiveError('Retained tar already exists; both copies held for review.')
            replace_file(old, retained); retained.chmod(0o600)
        elif not retained.is_file():
            raise ArchiveError('Retained original is unavailable; checkpoint preserved.')
    sync_directory(new.parent)


def _convert(old, metadata, new):
    pages = comic_pages(metadata)
    maximum = ((sum(512+((p['size']+511)//512)*512 for p in pages)+1024+10239)//10240)*10240
    temporary = new.with_name(new.name+'.tmp')
    safe_root(temporary)
    try:
        with old.open('rb') as source, temporary.open('wb') as output:
            reader = StrictZstdReader(source,maximum)
            with tarfile.open(fileobj=reader,mode='r|',bufsize=512) as archive, zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as comic:
                for page in pages:
                    member = archive.next()
                    if (member is None or not member.isfile() or member.name != page['file'] or member.size != page['size']):
                        raise ArchiveError('Original tar page membership differs; conversion held.')
                    data = archive.extractfile(member).read(page['size']+1)
                    if len(data) != page['size'] or digest(data) != page['sha256']:
                        raise ArchiveError('Original tar page bytes differ; conversion held.')
                    info = zipfile.ZipInfo(page['file'],date_time=(1980,1,1,0,0,0)); info.external_attr=0o644<<16
                    comic.writestr(info,data)
                if archive.next() is not None:
                    raise ArchiveError('Original tar has unexpected pages; conversion held.')
                reader.finish()
            output.flush(); os.fsync(output.fileno())
        with temporary.open('rb') as source:
            if verify_episode_contents(source,metadata|{'format':'cbz'}):
                raise ArchiveError('Converted CBZ differs from original pages; original retained.')
        replace_file(temporary, new)
    except (OSError,ValueError,tarfile.TarError,zstandard.ZstdError):
        raise ArchiveError('Original tar could not be converted; original retained.') from None
    finally:
        if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()


def convert_comics(root, *, limit=None, remove_originals=False, min_free_bytes=256*1024**2, progress=None):
    root = safe_root(root)
    if not root.is_dir() or type(remove_originals) is not bool:
        raise ArchiveError('Conversion requires an existing library and explicit retention choice.')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ArchiveError('Conversion limit must be a positive integer.')
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ArchiveError('Conversion reserve must be a nonnegative integer.')
    report = {'converted_comics':0,'removed_originals':0,'retained_originals':0,'source_requests':0,'remaining_tar_comics':0}
    with closing(PublicationStore(root)) as store:
        checkpoint = root/'.archive-state/cbz-conversion-pending.json'
        if checkpoint.exists():
            safe_root(checkpoint)
            if checkpoint.stat().st_size > 16*1024**2: raise ArchiveError('Oversized CBZ conversion checkpoint.')
            record = json.loads(checkpoint.read_text())
            _finish(store,record); checkpoint.unlink()
            report['converted_comics']+=1
            report['removed_originals' if record['remove_originals'] else 'retained_originals']+=1
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.'))
        report['remaining_tar_comics'] = sum(len(list(folder.glob('*.tar.zst'))) for folder in folders)
        for folder in folders:
            safe_root(folder)
            paths = sorted(folder.glob('*.tar.zst'))
            if paths and verify_series(folder/'series.json'):
                raise ArchiveError('Series failed verification; original comics retained.')
            for old in paths:
                if limit is not None and report['converted_comics'] >= limit: break
                safe_root(old)
                metadata = import_metadata(sidecar_path(old))
                if metadata.get('format') != 'tar.zst' or metadata.get('content_type') != 'comic':
                    raise ArchiveError('Unexpected original format; conversion held.')
                pages = comic_pages(metadata)
                needed = sum(p['size']+256 for p in pages)+1024**2+min_free_bytes
                if shutil.disk_usage(root).free < needed:
                    raise RunLimit('Not enough temporary space for the next CBZ; originals retained.')
                new = old.with_name(old.name[:-8]+'.cbz')
                if new.exists() or new.is_symlink():
                    raise ArchiveError('An uncheckpointed CBZ already exists; both copies retained for review.')
                original_stat = old.stat()
                # Record intent BEFORE creating the final file. An interrupted
                # copy can safely be rebuilt from the original on restart.
                record={'schema_version':1,'original':old.relative_to(root).as_posix(),
                        'converted':new.relative_to(root).as_posix(),'metadata':metadata,
                        'original_stat':[original_stat.st_size,original_stat.st_mtime_ns,original_stat.st_ino],
                        'remove_originals':remove_originals}
                _write(checkpoint,record)
                try:
                    _convert(old,metadata,new)
                    _finish(store,record)
                except BaseException:
                    if not new.exists(): checkpoint.unlink()
                    raise
                checkpoint.unlink()
                report['converted_comics']+=1
                report['removed_originals' if remove_originals else 'retained_originals']+=1
                report['remaining_tar_comics']-=1
                _write(root/'.archive-state/cbz-conversion.json',report)
                if progress: progress(dict(report))
        report['remaining_tar_comics'] = sum(len(list(folder.glob('*.tar.zst'))) for folder in folders)
        _write(root/'.archive-state/cbz-conversion.json',report)
    return report
