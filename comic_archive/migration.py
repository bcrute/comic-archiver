"""Resumable, source-free conversion. Verified originals remain private."""
import json
from pathlib import Path
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager

from .archive import atomic_json, digest, episode_archives, sidecar_path, verify_episode, verify_series
from .archive_paths import directory
from .errors import RunLimit
from .inventory import Inventory
from .models import ArchiveError, Episode, Page, Series
from .novels import DC, OPF
from .osdep import replace_file, sync_file
from .publication import PublicationStore, import_metadata, write_cbz


def _series(metadata):
    return Series(metadata.get('library_series_id', metadata['source_series_id']), metadata['title'],
                  tuple(metadata.get('creators', [])), metadata.get('description'), metadata.get('source_url'),
                  metadata['source'], metadata.get('source_series_id') if metadata.get('library_series_id') else None,
                  metadata.get('source_series_slug'), metadata.get('credits'), metadata.get('cover_url'))


def _retire(store, retained, record):
    root = store.root
    old = root/record['original']; new = root/record['converted']
    for path in (old, new):
        relative = path.relative_to(root)
        if len(relative.parts)!=2 or any(part.startswith('.') for part in relative.parts) or any(p.is_symlink() for p in (path,*path.parents)):
            raise ArchiveError('Conversion checkpoint path is unsafe; originals retained.')
    if verify_episode(new): raise ArchiveError('Converted export failed verification; originals retained.')
    meta=import_metadata(sidecar_path(new))
    inventory=store.migration_inventory
    row=inventory.db.execute('SELECT * FROM episodes WHERE id=?',(meta['source_episode_id'],)).fetchone()
    if row is not None:
        if not inventory.archive_matches_row(meta,row): raise ArchiveError('Conversion identity differs from inventory; originals retained.')
        inventory.mark(row['id'],'archived',path=new)
    original_folder=directory(retained/old.parent.name)
    original_folder.chmod(0o700)
    for original in (old,sidecar_path(old)):
        destination=original_folder/original.name
        if destination.is_symlink(): raise ArchiveError('Unsafe original retention path.')
        if original.exists():
            if destination.exists() and digest(destination.read_bytes())!=digest(original.read_bytes()):
                raise ArchiveError('Retained original differs; both copies retained for review.')
            replace_file(original,destination);destination.chmod(0o600)
        elif not destination.is_file():
            raise ArchiveError('Original is absent from its retention path; checkpoint retained.')
    series_meta=record['series'];series=_series(series_meta)
    cover=None
    if series_meta.get('cover_file'):
        cover=(old.parent/series_meta['cover_file']).read_bytes()
    store.write_series_metadata(series,cover=cover,enriched=series_meta.get('metadata_source')=='authenticated_series_page')
    series_path=store.series_folder(series)/'series.json'
    full=store.read_metadata(series_path)
    for key in ('metadata_source','metadata_imported_at'):
        if key in series_meta: full[key]=series_meta[key]
        else: full.pop(key,None)
    store.publish_metadata(series_path,full)
    remaining=dict(series_meta);remaining['episode_count']=len(episode_archives(old.parent))
    atomic_json(old.parent/'series.json',remaining)


def migrate_exports(output, *, limit=None, min_free_bytes=256*1024*1024, progress=None):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ArchiveError('Conversion limit must be a positive integer.')
    root = Path(output).absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ArchiveError('Conversion requires an existing library without symlinked paths.')
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ArchiveError('Free-space reserve must be a nonnegative integer.')
    report = {'converted_episodes': 0, 'retained_originals': 0, 'source_requests': 0, 'remaining_legacy_episodes': 0}
    with _store_context(root, min_free_bytes) as store:
        retained = directory(root / '.archive-state/legacy-exports')
        retained.chmod(0o700)
        retirement=root/'.archive-state/export-retirement.json'
        if retirement.exists():
            if retirement.is_symlink() or retirement.stat().st_size>256*1024: raise ArchiveError('Unsafe conversion checkpoint.')
            _retire(store,retained,json.loads(retirement.read_text()));retirement.unlink()
            report['converted_episodes']+=1;report['retained_originals']+=1
        folders = sorted(p for p in root.iterdir() if not p.name.startswith('.') and p.is_dir() and not p.is_symlink())
        for old_folder in folders:
            paths = [p for p in episode_archives(old_folder)
                     if p.suffix in {'.cbz','.epub'} and json.loads(sidecar_path(p).read_text()).get('schema_version') != 2]
            series_path = old_folder/'series.json'
            if not series_path.is_file() or json.loads(series_path.read_text()).get('schema_version')==2: continue
            if verify_series(series_path): raise ArchiveError('Original series metadata failed verification; retained.')
            old_series = import_metadata(series_path)
            original_folder=directory(retained/old_folder.name)
            original_folder.chmod(0o700)
            original_series=original_folder/'series.original.json'
            if not original_series.exists():
                atomic_json(original_series,old_series);original_series.chmod(0o600)
            series = _series(old_series)
            new_folder = store.series_folder(series)
            completed_folder = True
            for old_path in paths:
                if limit is not None and report['converted_episodes'] >= limit:
                    completed_folder = False; break
                if verify_episode(old_path): raise ArchiveError('Original episode failed verification; retained.')
                meta = import_metadata(sidecar_path(old_path))
                if (meta.get('source') != series.source or meta.get('series_id') != series.id
                        or not isinstance(meta.get('source_episode_id'), str)):
                    raise ArchiveError('Original episode identity differs from its series; retained.')
                novel = old_path.suffix == '.epub'
                episode = Episode(meta['source_episode_id'], series.id, meta['title'], meta['episode_number'],
                                  () if novel else (Page(1, 'conversion'),), meta['access_state'],
                                  meta.get('source_url'), meta.get('published_at'), 'novel' if novel else 'comic',
                                  '<p>conversion</p>' if novel else None)
                path = store.episode_path(series, episode, new_folder)
                if shutil.disk_usage(root).free < old_path.stat().st_size + min_free_bytes + 1024*1024:
                    raise RunLimit('Conversion free-space reserve reached; originals are retained.')
                converted = store.prepare_metadata(dict(meta), series, episode)
                with zipfile.ZipFile(old_path) as archive:
                    if novel:
                        entries = {name: archive.read(name) for name in archive.namelist()}
                        package = ET.fromstring(entries['EPUB/package.opf'])
                        identity = package.find(f'{{{OPF}}}metadata/{{{DC}}}identifier')
                        if identity is None: raise ArchiveError('Original EPUB identifier is missing; retained.')
                        identity.text = 'urn:uuid:' + converted['export_id']
                        # Swapped in place, so the package keeps a fresh export's prefixes; ElementTree would write ns0.
                        new_id = identity.text.encode()
                        opf, count = re.subn(rb'(<dc:identifier id="book-id">)[^<]*(</dc:identifier>)',
                                             lambda m, new_id=new_id: m[1] + new_id + m[2], entries['EPUB/package.opf'])
                        entries['EPUB/package.opf'] = opf if count == 1 else ET.tostring(package, encoding='utf-8', xml_declaration=True)
                        converted['content_hashes'] = [{'file': name, 'size': len(data), 'sha256': digest(data)} for name,data in entries.items()]
                        temporary = path.with_name(path.name + '.tmp')
                        if temporary.is_symlink(): raise ArchiveError('Unsafe conversion output.')
                        try:
                            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as target:
                                for name,data in entries.items():
                                    member = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0));member.external_attr=0o644<<16
                                    target.writestr(member,data)
                            sync_file(temporary)
                            replace_file(temporary, path)
                        finally:
                            if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()
                    else:
                        write_cbz(path, converted, lambda page: archive.open(page['file']))
                store.publish_metadata(sidecar_path(path), converted)
                if verify_episode(path): raise ArchiveError('Converted export failed verification; original retained.')
                record={'original':old_path.relative_to(root).as_posix(),
                        'converted':path.relative_to(root).as_posix(),'series':old_series}
                atomic_json(retirement,record);retirement.chmod(0o600)
                _retire(store,retained,record);retirement.unlink()
                report['converted_episodes'] += 1; report['retained_originals'] += 1
                atomic_json(root/'.archive-state/export-conversion.json', report)
                (root/'.archive-state/export-conversion.json').chmod(0o600)
                if progress: progress(dict(report))
            if completed_folder:
                original_folder = directory(retained / old_folder.name)
                for original in old_folder.iterdir():
                    if original.is_symlink() or not original.is_file():
                        raise ArchiveError('Unexpected original series entry; retained for review.')
                    destination=original_folder/original.name
                    if destination.exists(): raise ArchiveError('Original metadata retention conflict; retained for review.')
                    replace_file(original,destination);destination.chmod(0o600)
                old_folder.rmdir()
            else:
                old_series['episode_count'] = len(episode_archives(old_folder))
                atomic_json(series_path, old_series)
                break
        report['remaining_legacy_episodes'] = sum(1 for folder in root.iterdir()
            if folder.is_dir() and not folder.name.startswith('.') and not folder.is_symlink()
            for p in episode_archives(folder) if p.suffix in {'.cbz','.epub'} and
            json.loads(sidecar_path(p).read_text()).get('schema_version')!=2)
        atomic_json(root/'.archive-state/export-conversion.json', report)
        (root/'.archive-state/export-conversion.json').chmod(0o600)
    return report


@contextmanager
def _store_context(root, reserve):
    store = PublicationStore(root, min_free_bytes=reserve)
    try:
        with Inventory(root) as inventory:
            store.migration_inventory=inventory
            yield store
    finally: store.close()
