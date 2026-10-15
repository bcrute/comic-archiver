import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import zlib
from dataclasses import replace

import zstandard

from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive.archive import Store, digest, sidecar_path, verify, verify_episode, verify_episode_contents
from comic_archive.backup import backup_library
from comic_archive.inventory import Inventory
from comic_archive.errors import LibraryBusy
from comic_archive.jobs import archive_discovered
from comic_archive.locking import LibraryLock
from comic_archive.migration import migrate_exports
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import (PublicationStore, StrictZstdReader, comic_pages, import_metadata,
                                       metadata_hash, public_metadata, validate_public_metadata,
                                       verify_compressed_comic, write_cbz, write_compressed_comic)
from comic_archive.storage import configure
from test_archive import MemoryAdapter, png
from test_jobs import AccountAdapter


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library'
        self.series=Series('424242','Series',('Author',),source_url='https://tapas.io/series/424242/info',
                           credits='Tapas credit',description='https://tapas.io/private',platform_id='424242')
        self.episode=Episode('818181',self.series.id,'Chapter',1,(Page(1,'https://cdn.tapas.io/a',False),),
                             'tapas_unlocked','https://tapas.io/episode/818181')

    def publish(self):
        store=PublicationStore(self.root)
        try: return store.archive(Manifest(self.series,(self.episode,)),MemoryAdapter(),progress=lambda _:None)
        finally: store.close()

    def test_public_exports_have_original_pages_and_no_importer_source_fields(self):
        folder=self.publish();archive=next(folder.glob('*.cbz'))
        self.assertEqual(list(folder.glob('*.tar.zst')),[])
        for path in folder.iterdir():
            self.assertNotIn('[424242]',path.name);self.assertNotIn('[818181]',path.name)
            if path.suffix=='.json':
                value=json.loads(path.read_text());self.assertEqual(value['schema_version'],2)
                self.assertNotIn('tapas',path.read_text().lower())
                for key in ('source','series_id','source_episode_id','source_url','source_series_id','access_state','credits','description'):
                    self.assertNotIn(key,value)
        with zipfile.ZipFile(archive) as comic:
            self.assertEqual(comic.namelist(),['0001.png']);self.assertEqual(comic.read('0001.png'),png(1))
            member=comic.getinfo('0001.png')
            self.assertEqual(member.date_time,(1980,1,1,0,0,0));self.assertEqual(member.compress_type,zipfile.ZIP_STORED)
        self.assertEqual(verify(self.root),[])
        private=import_metadata(sidecar_path(archive))
        self.assertEqual(private['source_episode_id'],'818181');self.assertEqual(private['source'],'tapas')
        self.assertEqual((self.root/'.archive-state/provenance').stat().st_mode&0o777,PRIVATE_FOLDER)
        self.assertTrue(all(p.stat().st_mode&0o777==PRIVATE_FILE for p in (self.root/'.archive-state/provenance').glob('*.json')))

    def test_where_whole_paths_are_limited_long_titles_are_cut_in_the_names_and_kept_in_the_metadata(self):
        from comic_archive import osdep
        from comic_archive.publication import AFTER_TITLE, EPISODE_ROOM
        title='Z'+'\U0001F600'*40  # Windows counts an emoji as two.
        series=replace(self.series,title=title);episode=replace(self.episode,title=title)
        def publish(root,limit):
            with patch('comic_archive.osdep.PATH_LIMIT',limit):
                store=PublicationStore(root)
                try: folder=store.archive(Manifest(series,(episode,)),MemoryAdapter(),progress=lambda _:None)
                finally: store.close()
            return folder,next(folder.glob('*.cbz'))
        # A library in a short folder is named as if its folder were as deep as it may later be moved to.
        limit=osdep.LIBRARY_PATH_ROOM+1+20+AFTER_TITLE+EPISODE_ROOM  # Twenty units for the series title.
        self.assertLess(osdep.units(self.root),osdep.LIBRARY_PATH_ROOM)
        folder,archive=publish(self.root,limit)
        self.assertEqual(folder.name.split(' [')[0],'Z'+'\U0001F600'*9)  # Nineteen units: half an emoji is not kept.
        moved=osdep.LIBRARY_PATH_ROOM+1+osdep.units(folder.name)+1+osdep.units(archive.with_suffix('.tar.zst.tmp').name)
        self.assertLessEqual(moved,limit)
        self.assertGreaterEqual(osdep.units(archive.name.split(' - ')[1].split(' [')[0]),48)
        self.assertEqual((json.loads((folder/'series.json').read_text())['title'],json.loads(sidecar_path(archive).read_text())['title']),(title,title))
        self.assertEqual(verify(self.root),[])
        # A library already in a deeper folder is measured by that folder.
        deep=self.root.parent/('d'*(osdep.LIBRARY_PATH_ROOM+30-osdep.units(self.root.parent)-1))
        folder,archive=publish(deep,limit+30)
        self.assertEqual(folder.name.split(' [')[0],'Z'+'\U0001F600'*9)
        self.assertLessEqual(osdep.units(archive.with_suffix('.tar.zst.tmp')),limit+30)

    def test_restart_reuses_names_cached_bytes_and_import_time(self):
        folder=self.publish();archive=next(folder.glob('*.cbz'))
        before=(archive.read_bytes(),sidecar_path(archive).read_bytes(),archive.name)
        self.publish()
        self.assertEqual((archive.read_bytes(),sidecar_path(archive).read_bytes(),archive.name),before)
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))),1)

    def test_existing_neutral_ids_recover_before_slug_rename_without_rewriting_episode(self):
        original=replace(self.series,id='original-slug',slug='original-slug')
        episode=replace(self.episode,series_id=original.id)
        store=PublicationStore(self.root)
        try:
            folder=store.archive(Manifest(original,(episode,)),MemoryAdapter(),progress=lambda _:None)
            archive=next(folder.glob('*.cbz'))
            before=(archive.read_bytes(),sidecar_path(archive).read_bytes(),json.loads((folder/'series.json').read_text())['export_id'])
            store.db.execute('DROP TABLE export_series_keys');store.db.commit()
        finally:store.close()
        renamed=replace(original,id='renamed-slug',slug='renamed-slug',title='Renamed title')
        store=PublicationStore(self.root)
        try:
            after_folder=store.archive(Manifest(renamed,(replace(episode,series_id=renamed.id),)),MemoryAdapter(),progress=lambda _:None)
            self.assertEqual(after_folder,folder)
            self.assertEqual((archive.read_bytes(),sidecar_path(archive).read_bytes(),json.loads((folder/'series.json').read_text())['export_id']),before)
            self.assertEqual(store.db.execute('SELECT series_id FROM export_series_keys WHERE source=? AND source_series_id=?',('tapas','424242')).fetchone()[0],'original-slug')
        finally:store.close()
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))),1)
        self.assertEqual(verify(self.root),[])

    def test_tar_terminator_crossing_a_record_boundary_survives_publication_and_offline_restore(self):
        # A complete PNG with a valid ancillary text chunk makes a 9,216-byte page.
        # Header + page fill 9,728 bytes; the two TAR end blocks need another record.
        base=png(1);payload=b'Comment\0'+b'a'*(9216-len(base)-12-8)
        chunk=b'tEXt'+payload
        image=base[:-12]+len(payload).to_bytes(4,'big')+chunk+zlib.crc32(chunk).to_bytes(4,'big')+base[-12:]
        self.assertEqual(len(image),9216)
        adapter=MemoryAdapter();adapter.retrieve=lambda page:image
        store=PublicationStore(self.root)
        try:
            folder=store.archive(Manifest(self.series,(self.episode,)),adapter,progress=lambda _:None)
            cbz=next(folder.glob('*.cbz'));old=cbz.with_suffix('.tar.zst')
            metadata=import_metadata(sidecar_path(cbz))|{'format':'tar.zst'}
            from comic_archive.publication import write_compressed_comic
            write_compressed_comic(old,metadata,lambda page:io.BytesIO(image))
            cbz.unlink();store.publish_metadata(sidecar_path(old),metadata)
        finally: store.close()
        self.assertEqual(verify(self.root),[])
        destination=self.root.parent/'Restored'
        script=Path(__file__).resolve().parents[1]/'scripts/offline_restore.py'
        result=subprocess.run([sys.executable,str(script),str(self.root),'--destination',str(destination),'--decode-images'],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['image_count'],1)

    def test_truncated_footer_trailing_frames_wrong_order_and_links_are_rejected(self):
        folder=self.publish();archive=next(folder.glob('*.cbz'))
        meta=json.loads(sidecar_path(archive).read_text()) | {'format':'tar.zst'}
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w',format=tarfile.USTAR_FORMAT) as tar:
            member=tarfile.TarInfo('0001.png');member.size=len(png(1));tar.addfile(member,io.BytesIO(png(1)))
        data=zstandard.ZstdCompressor(write_checksum=True).compress(raw.getvalue())
        for invalid in (data[:-1],data[:-4],data+zstandard.ZstdCompressor().compress(b'EXTRA'),data+b'junk'):
            self.assertTrue(verify_episode_contents(io.BytesIO(invalid),meta))
        for name,kind in [('0002.png',tarfile.REGTYPE),('0001.png',tarfile.SYMTYPE)]:
            raw=io.BytesIO()
            with tarfile.open(fileobj=raw,mode='w',format=tarfile.USTAR_FORMAT) as tar:
                member=tarfile.TarInfo(name);member.type=kind;member.size=len(png(1)) if kind==tarfile.REGTYPE else 0
                tar.addfile(member,io.BytesIO(png(1)) if member.size else None)
            self.assertTrue(verify_episode_contents(io.BytesIO(zstandard.ZstdCompressor().compress(raw.getvalue())),meta))

    def test_production_job_reconciles_private_identity_without_downloads_on_resume(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
        with patch('builtins.print'):
            first=archive_discovered(AccountAdapter(),self.root)
            second=archive_discovered(AccountAdapter(),self.root)
        self.assertEqual(first['archived_episode_ids'],['81']);self.assertEqual(second['archived_episode_ids'],[])
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))),1)
        with Inventory(self.root) as inventory:
            self.assertGreaterEqual(inventory.reconcile(full=True),1)
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1})

    def test_private_provenance_is_required_for_source_identity_and_cannot_be_swapped(self):
        folder=self.publish();archive=next(folder.glob('*.cbz'));sidecar=sidecar_path(archive)
        meta=json.loads(sidecar.read_text());private=self.root/'.archive-state/provenance'/(meta['export_id']+'.json')
        record=json.loads(private.read_text());record['sidecar']='Other/Episode.json';private.write_text(json.dumps(record))
        with self.assertRaises(ValueError): import_metadata(sidecar)
        self.assertEqual(verify_episode(archive),[])  # Portable content remains independently verifiable.
        private.unlink()
        with self.assertRaises(OSError): import_metadata(sidecar)

    def test_novel_uuid_replaces_platform_identifier_inside_epub(self):
        novel=Episode('818181',self.series.id,'Chapter',1,(),'tapas_unlocked',content_type='novel',text_html='<p>Original text</p>')
        store=PublicationStore(self.root)
        try: folder=store.archive(Manifest(self.series,(novel,)),MemoryAdapter(),progress=lambda _:None)
        finally: store.close()
        archive=next(folder.glob('*.epub'))
        with zipfile.ZipFile(archive) as z:
            self.assertIn(b'urn:uuid:',z.read('EPUB/package.opf'));self.assertNotIn(b'tapas:',z.read('EPUB/package.opf'))
        self.assertEqual(verify(self.root),[])

    def test_backup_and_independent_restore_copy_only_neutral_portable_files(self):
        self.publish()
        result=backup_library(self.root,self.root.parent/'Backup',reserve_bytes=0)
        self.assertTrue(result['complete'])
        destination=self.root.parent/'Restored'
        script=Path(__file__).resolve().parents[1]/'scripts/offline_restore.py'
        restored=subprocess.run([sys.executable,str(script),str(self.root),'--destination',str(destination)],capture_output=True,text=True,check=True)
        report=json.loads(restored.stdout);self.assertEqual(report['image_count'],1)
        self.assertFalse(report['application_imported']);self.assertFalse((destination/'.archive-state').exists())
        self.assertEqual(verify_episode(next(destination.glob('*/*.cbz'))),[])

    def legacy(self, count=1, novel=False):
        episodes=tuple(Episode(str(818181+i),self.series.id,'Chapter',i+1,
            () if novel else (Page(1,'https://cdn.tapas.io/a',False),), 'tapas_unlocked',
            content_type='novel' if novel else 'comic',text_html='<p>Original text</p>' if novel else None) for i in range(count))
        store=Store(self.root)
        try: return store.archive(Manifest(self.series,episodes),MemoryAdapter(),progress=lambda _:None)
        finally: store.close()

    def test_bounded_migration_resumes_preserves_originals_and_neutralizes_epub(self):
        self.legacy(count=2)
        first=migrate_exports(self.root,limit=1,min_free_bytes=0)
        self.assertEqual(first['converted_episodes'],1);self.assertEqual(first['remaining_legacy_episodes'],1)
        self.assertEqual(verify(self.root),[])
        second=migrate_exports(self.root,min_free_bytes=0)
        self.assertEqual(second['remaining_legacy_episodes'],0);self.assertEqual(len(list(self.root.glob('*/*.cbz'))),2)
        self.assertEqual(len(list((self.root/'.archive-state/legacy-exports').rglob('*.cbz'))),2)
        self.assertFalse(any('[424242]' in p.name for p in self.root.iterdir() if not p.name.startswith('.')))
        self.assertEqual(verify(self.root),[])
        self.assertEqual(migrate_exports(self.root,min_free_bytes=0)['converted_episodes'],0)
        shutil.rmtree(self.root);self.legacy(novel=True)
        result=migrate_exports(self.root,min_free_bytes=0);self.assertEqual(result['converted_episodes'],1)
        self.assertEqual(verify(self.root),[])
        with zipfile.ZipFile(next(self.root.glob('*/*.epub'))) as z:
            self.assertNotIn(b'tapas:',z.read('EPUB/package.opf'))

    def test_migration_recovers_interruption_between_original_pair_moves(self):
        folder=self.legacy();original_replace=os.replace
        def interrupted(path,target):
            path=Path(path)
            if path.parent==folder and path.suffix=='.json' and path.name!='series.json':
                raise OSError('simulated interruption')
            return original_replace(path,target)
        with patch.object(os,'replace',interrupted),self.assertRaises(OSError): migrate_exports(self.root,min_free_bytes=0)
        self.assertTrue((self.root/'.archive-state/export-retirement.json').is_file())
        self.assertEqual(migrate_exports(self.root,min_free_bytes=0)['remaining_legacy_episodes'],0)
        self.assertEqual(verify(self.root),[])
        self.assertEqual(len(list((self.root/'.archive-state/legacy-exports').rglob('*.cbz'))),1)

    def test_failed_verification_never_retires_original(self):
        folder=self.legacy();archive=next(folder.glob('*.cbz'));archive.write_bytes(b'corrupt')
        with self.assertRaises(ArchiveError): migrate_exports(self.root,min_free_bytes=0)
        self.assertTrue(archive.is_file());self.assertEqual(list(self.root.glob('*/*.cbz')),[archive])

    def pages_manifest(self, *images):
        pages = [{'file': f'{i:04d}.png', 'size': len(data), 'sha256': digest(data)} for i, data in enumerate(images, 1)]
        return {'page_count': len(pages), 'content_hashes': pages}

    def library_is_free_for_other_threads(self):
        result = []
        def attempt():
            try:
                with LibraryLock(self.root): result.append(True)
            except LibraryBusy: result.append(False)
        thread = threading.Thread(target=attempt); thread.start(); thread.join()
        return result == [True]

    def private_record(self, folder_or_sidecar):
        sidecar = Path(folder_or_sidecar)
        export_id = json.loads(sidecar.read_text())['export_id']
        return self.root / '.archive-state/provenance' / (export_id + '.json')

    def test_neutral_metadata_with_an_unknown_format_or_compression_declaration_is_invalid(self):
        folder = self.publish()
        good = json.loads(sidecar_path(next(folder.glob('*.cbz'))).read_text())
        validate_public_metadata(good)
        for change in ({'schema_version': 1}, {'export_id': 'not-a-uuid'}, {'title': ' '},
                       {'source_url': 'https://tapas.io/episode/818181'}, {'source': 'tapas'}):
            with self.assertRaisesRegex(ValueError, 'Invalid neutral export metadata'): validate_public_metadata(good | change)
        with self.assertRaises(ValueError): validate_public_metadata(good | {'format': 'zip'})
        with self.assertRaises(ValueError): validate_public_metadata(good | {'content_type': 'novel'})
        with self.assertRaises(ValueError): validate_public_metadata(good | {'images_recompressed': 'yes', 'image_profile': 'small'})
        with self.assertRaises(ArchiveError): validate_public_metadata(good | {'images_recompressed': False, 'image_profile': 'huge'})

    def test_provenance_is_refused_for_unsafe_unreadable_or_swapped_records(self):
        folder = self.publish(); archive = next(folder.glob('*.cbz')); sidecar = sidecar_path(archive)
        stray = folder / 'stray.json'
        stray.write_text('[1]')
        with self.assertRaisesRegex(ValueError, 'Invalid metadata'): import_metadata(stray)
        with stray.open('wb') as output: output.truncate(16 * 1024 ** 2 + 1)
        with self.assertRaisesRegex(ValueError, 'Unsafe metadata'): import_metadata(stray)
        stray.unlink(); stray.symlink_to(sidecar)
        with self.assertRaisesRegex(ValueError, 'Unsafe metadata'): import_metadata(stray)
        private = self.private_record(sidecar); good = private.read_text()
        copy = self.root.parent / 'provenance-copy.json'; copy.write_text(good)
        private.unlink(); private.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, 'Unsafe private provenance'): import_metadata(sidecar)
        private.unlink(); private.write_text(good)
        record = json.loads(good); record['metadata']['title'] = 'Swapped'
        private.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'differs from private provenance'): import_metadata(sidecar)
        private.write_text(good)
        self.assertEqual(import_metadata(sidecar)['source_episode_id'], '818181')

    def test_zstd_reader_enforces_size_window_and_framing_limits(self):
        frame = zstandard.ZstdCompressor(write_checksum=True).compress(b'x' * 5000)
        with self.assertRaisesRegex(ValueError, 'exceeds its manifest'): StrictZstdReader(io.BytesIO(frame), 1000).read(5000)
        with self.assertRaisesRegex(ValueError, 'Unbounded'): StrictZstdReader(io.BytesIO(frame), 10000).read()
        with self.assertRaisesRegex(ValueError, 'Incomplete'): StrictZstdReader(io.BytesIO(frame[:-6]), 10000).read(65536)
        padded = zstandard.ZstdCompressor(write_checksum=True).compress(b'\0' * 1024 + b'x')
        reader = StrictZstdReader(io.BytesIO(padded), 10000); reader.read(512)
        with self.assertRaisesRegex(ValueError, 'after TAR terminator'): reader.finish()
        clean = StrictZstdReader(io.BytesIO(zstandard.ZstdCompressor(write_checksum=True).compress(b'\0' * 1024)), 10000)
        clean.read(512); clean.finish()
        wide = io.BytesIO()
        parameters = zstandard.ZstdCompressionParameters.from_level(1, window_log=24)
        with zstandard.ZstdCompressor(compression_params=parameters).stream_writer(wide, closefd=False) as writer:
            writer.write(b'x' * 100)
        with self.assertRaisesRegex(ValueError, 'window'): StrictZstdReader(io.BytesIO(wide.getvalue()), 10000).read(100)

    def test_page_manifests_must_be_complete_ordered_and_bounded(self):
        good = {'file': '0001.png', 'size': 10, 'sha256': 'a' * 64}
        self.assertEqual(comic_pages({'page_count': 1, 'content_hashes': [good]}), [good])
        for pages, count in [('text', 1), ([], 0), ([good], 2), ([good], True)]:
            with self.assertRaisesRegex(ValueError, 'Invalid page count'): comic_pages({'page_count': count, 'content_hashes': pages})
        for page in ('text', good | {'size': 0}, good | {'size': 100 * 1024 * 1024 + 1}, good | {'size': '10'},
                     good | {'file': '0002.png'}, good | {'file': '0001.bmp'}, good | {'file': '../0001.png'},
                     good | {'sha256': 'A' * 64}, good | {'sha256': 'a' * 63}):
            with self.assertRaisesRegex(ValueError, 'Invalid page manifest'): comic_pages({'page_count': 1, 'content_hashes': [page]})

    def test_writers_refuse_symlinks_and_never_publish_content_that_fails_their_own_check(self):
        images = [png(1), png(2)]; manifest = self.pages_manifest(*images)
        damaged = bytearray(images[1]); damaged[40] ^= 1
        outside = self.root.parent / 'outside.bin'; outside.write_bytes(b'keep')
        output = self.root.parent / 'out'; output.mkdir()
        def honest(page): return io.BytesIO(images[int(page['file'][:4]) - 1])
        def lying(page): return io.BytesIO(bytes(damaged) if page['file'] == '0002.png' else images[0])
        for writer, name in ((write_cbz, 'one.cbz'), (write_compressed_comic, 'one.tar.zst')):
            with self.subTest(writer.__name__):
                target = output / name
                with self.assertRaisesRegex(ArchiveError, 'failed verification'): writer(target, manifest, lying)
                self.assertEqual(list(output.iterdir()), [])
                target.symlink_to(outside)
                with self.assertRaises(ArchiveError): writer(target, manifest, honest)
                target.unlink()
                (output / (name + '.tmp')).symlink_to(outside)
                with self.assertRaises(ArchiveError): writer(target, manifest, honest)
                (output / (name + '.tmp')).unlink()
                writer(target, manifest, honest)
                self.assertTrue(target.is_file()); self.assertEqual(sorted(p.name for p in output.iterdir()), [name])
                target.unlink()
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_compressed_comic_verification_reports_wrong_content_extra_members_and_undecodable_images(self):
        images = [png(1), png(2)]; manifest = self.pages_manifest(*images)
        path = self.root.parent / 'comic.tar.zst'
        write_compressed_comic(path, manifest, lambda page: io.BytesIO(images[int(page['file'][:4]) - 1]))
        def check(meta, decoder=None):
            with path.open('rb') as source: return verify_compressed_comic(source, meta, image_decoder=decoder)
        self.assertEqual(check(manifest), [])
        wrong = json.loads(json.dumps(manifest)); wrong['content_hashes'][0]['sha256'] = '0' * 64
        self.assertEqual(check(wrong), ['Page content or numbering mismatch'])
        self.assertEqual(check(self.pages_manifest(images[0])), ['Unexpected archive member'])
        seen = []
        self.assertEqual(check(manifest, seen.append), []); self.assertEqual(seen, images)
        def refuse(data): raise ArchiveError('PRIVATE')
        self.assertEqual(check(manifest, refuse), ['Page 1: image pixels could not be decoded'])

    def test_large_incompressible_pages_survive_streaming_compression_and_verification(self):
        base = png(1); payload = b'Comment\0' + random.Random(7).randbytes(30000); chunk = b'tEXt' + payload
        image = base[:-12] + len(payload).to_bytes(4, 'big') + chunk + zlib.crc32(chunk).to_bytes(4, 'big') + base[-12:]
        manifest = self.pages_manifest(image, png(2)); path = self.root.parent / 'large.tar.zst'
        pages = {'0001.png': image, '0002.png': png(2)}
        write_compressed_comic(path, manifest, lambda page: io.BytesIO(pages[page['file']]))
        self.assertGreater(path.stat().st_size, 4096 * 2)
        with path.open('rb') as source: self.assertEqual(verify_compressed_comic(source, manifest), [])

    def test_invalid_storage_settings_stop_the_store_and_release_the_library(self):
        state = self.root / '.archive-state'; state.mkdir(parents=True)
        (state / 'storage-policy.json').write_text('{broken')
        with self.assertRaises(ArchiveError): PublicationStore(self.root)
        self.assertTrue(self.library_is_free_for_other_threads())
        (state / 'storage-policy.json').unlink()
        PublicationStore(self.root).close()

    def test_reopening_a_library_refuses_series_identities_that_disagree_with_private_records(self):
        store = PublicationStore(self.root)
        try:
            store.series_folder(replace(self.series, id='555', platform_id='555'))  # reserved, never published
            store.db.execute('DELETE FROM export_series_keys'); store.db.commit()
        finally: store.close()
        PublicationStore(self.root).close()
        second = Series('999', 'Second', source_url='https://tapas.io/series/999/info', platform_id='999')
        store = PublicationStore(self.root)
        try:
            store.archive(Manifest(self.series, (self.episode,)), MemoryAdapter(), progress=lambda _: None)
            store.archive(Manifest(second, (Episode('777', '999', 'Other', 1, self.episode.pages, 'tapas_unlocked'),)),
                          MemoryAdapter(), progress=lambda _: None)
            folders = {row[0]: row[1] for row in store.db.execute('SELECT series_id,path FROM series_paths')}
            store.db.execute('DELETE FROM export_series_keys'); store.db.commit()
        finally: store.close()
        private = self.private_record(self.root / folders['999'] / 'series.json'); good = private.read_text()
        for field, value, message in (('library_series_id', 'elsewhere', 'differs from its private metadata'),
                                      ('source_series_id', '424242', 'Multiple export identities')):
            record = json.loads(good); record['metadata'][field] = value
            private.write_text(json.dumps(record))
            with self.subTest(field), self.assertRaisesRegex(ArchiveError, message): PublicationStore(self.root)
            self.assertTrue(self.library_is_free_for_other_threads())
        private.write_text(good)
        PublicationStore(self.root).close()

    def test_resume_keeps_cached_pages_when_cache_cleanup_is_disabled(self):
        configure(self.root, purge_completed_cache=False)
        folder = self.publish(); archive = next(folder.glob('*.cbz'))
        blob = self.root / '.archive-cache' / digest(png(1))
        self.assertTrue(blob.is_file())
        before = archive.read_bytes(), sidecar_path(archive).read_bytes()
        self.publish()
        self.assertTrue(blob.is_file())
        self.assertEqual((archive.read_bytes(), sidecar_path(archive).read_bytes()), before)


if __name__=='__main__': unittest.main()
