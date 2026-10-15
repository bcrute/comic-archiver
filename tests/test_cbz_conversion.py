from contextlib import closing
from dataclasses import replace
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import zstandard
from comic_archive.archive import sidecar_path, verify, verify_episode
from comic_archive.cbz_conversion import convert_comics
from comic_archive.errors import RunLimit
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata, write_compressed_comic
from comic_archive.storage import configure
from test_archive import MemoryAdapter, png


class CbzConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'Library'
        self.series=Series('42','Synthetic',source='tapas')
        self.episode=Episode('81','42','Chapter',1,(Page(1,'https://cdn.test/one',False),),'tapas_free')

    def legacy(self, count=1):
        paths=[]
        with closing(PublicationStore(self.root)) as store:
            for n in range(count):
                episode=replace(self.episode,id=str(81+n),order=n+1,title='Chapter '+str(n))
                folder=store.archive(Manifest(self.series,(episode,)),MemoryAdapter(),progress=lambda _:None)
                cbz=store.episode_path(self.series,episode,folder)
                old=cbz.with_suffix('.tar.zst'); metadata=import_metadata(sidecar_path(cbz))|{'format':'tar.zst'}
                with zipfile.ZipFile(cbz) as archive:
                    write_compressed_comic(old,metadata,lambda page:archive.open(page['file']))
                cbz.unlink();store.publish_metadata(sidecar_path(old),metadata)
                store.db.execute('UPDATE episode_paths SET path=? WHERE path=?',(old.relative_to(self.root).as_posix(),cbz.relative_to(self.root).as_posix()))
                store.db.commit();paths.append(old)
        return paths

    def test_conversion_preserves_exact_pages_uuid_quality_and_novels_and_removes_verified_tars(self):
        old=self.legacy()[0]; before=json.loads(sidecar_path(old).read_text())
        with Inventory(self.root) as inventory:
            inventory.observe('42','Synthetic',[{'id':'81','title':'Chapter 0','order':1,'access_state':'tapas_free'}],'fixture')
            inventory.reconcile(full=True)
        novel=Episode('90','42','Text',4,(),'tapas_free',content_type='novel',text_html='<p>Preserved text.</p>')
        with closing(PublicationStore(self.root)) as store:
            folder=store.archive(Manifest(self.series,(novel,)),MemoryAdapter(),progress=lambda _:None)
            epub=store.episode_path(self.series,novel,folder)
        novel_before=(epub.read_bytes(),sidecar_path(epub).read_bytes())
        result=convert_comics(self.root,remove_originals=True,min_free_bytes=0)
        self.assertEqual(result,{'converted_comics':1,'removed_originals':1,'retained_originals':0,'source_requests':0,'remaining_tar_comics':0})
        new=old.with_name(old.name[:-8]+'.cbz'); self.assertFalse(old.exists())
        self.assertEqual(json.loads(sidecar_path(new).read_text()),before|{'format':'cbz'})
        with zipfile.ZipFile(new) as comic:
            self.assertEqual(comic.namelist(),['0001.png']);self.assertEqual(comic.read('0001.png'),png(1))
            self.assertEqual(comic.getinfo('0001.png').compress_type,zipfile.ZIP_STORED)
        self.assertEqual((epub.read_bytes(),sidecar_path(epub).read_bytes()),novel_before)
        self.assertEqual(verify(self.root),[])
        with Inventory(self.root) as inventory:
            self.assertEqual(Path(inventory.db.execute('SELECT archive_path FROM episodes WHERE id=?',('81',)).fetchone()[0]).name,new.name)
        with closing(PublicationStore(self.root)) as store:
            locations=store.db.execute('SELECT path FROM archived_pages').fetchall()
            self.assertTrue(any(p[0].endswith('.cbz') for p in locations))
            self.assertFalse(any(p[0].endswith('.tar.zst') for p in locations))

    def test_retained_originals_are_independently_verifiable_and_limit_resumes(self):
        self.legacy(2)
        first=convert_comics(self.root,limit=1,min_free_bytes=0)
        self.assertEqual(first['retained_originals'],1); self.assertEqual(first['remaining_tar_comics'],1)
        retained=next((self.root/'.archive-state/legacy-exports').glob('*/*.tar.zst'))
        self.assertEqual(verify_episode(retained),[])
        second=convert_comics(self.root,min_free_bytes=0)
        self.assertEqual(second['converted_comics'],1); self.assertEqual(second['remaining_tar_comics'],0)
        self.assertEqual(convert_comics(self.root)['converted_comics'],0)
        self.assertEqual(verify(self.root),[])

    def test_progress_remaining_count_matches_public_tars_during_bounded_resume(self):
        self.legacy(3)
        observed=[]
        def progress(report):
            remaining=len(list(self.root.glob('*/*.tar.zst')))
            self.assertEqual(report['remaining_tar_comics'],remaining)
            persisted=json.loads((self.root/'.archive-state/cbz-conversion.json').read_text())
            self.assertEqual(persisted,report)
            observed.append(remaining)
        first=convert_comics(self.root,limit=1,remove_originals=True,min_free_bytes=0,progress=progress)
        self.assertEqual(first['remaining_tar_comics'],2)
        second=convert_comics(self.root,remove_originals=True,min_free_bytes=0,progress=progress)
        self.assertEqual(second['remaining_tar_comics'],0)
        self.assertEqual(observed,[2,1,0])

    def test_existing_verified_tar_is_reused_until_explicit_conversion(self):
        old=self.legacy()[0]; before=(old.read_bytes(),sidecar_path(old).read_bytes())
        adapter=MemoryAdapter();adapter.retrieve=Mock(side_effect=AssertionError('No source requests'))
        with closing(PublicationStore(self.root)) as store:
            store.archive(Manifest(self.series,(replace(self.episode,title='Chapter 0'),)),adapter,progress=lambda _:None)
            self.assertEqual(store.episode_path(self.series,self.episode),old)
        adapter.retrieve.assert_not_called()
        self.assertEqual((old.read_bytes(),sidecar_path(old).read_bytes()),before)
        self.assertEqual(list(self.root.glob('*/*.cbz')),[])

    def test_interrupted_removal_resumes_without_rewriting_pages_or_new_export_ids(self):
        old=self.legacy()[0]; unlink=Path.unlink
        def hold(path,*args,**kwargs):
            if path==old: raise OSError('synthetic interruption')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',hold),self.assertRaises(OSError):
            convert_comics(self.root,remove_originals=True,min_free_bytes=0)
        new=old.with_name(old.name[:-8]+'.cbz'); before=(new.read_bytes(),sidecar_path(new).read_bytes())
        result=convert_comics(self.root,remove_originals=True,min_free_bytes=0)
        self.assertEqual(result['converted_comics'],1); self.assertFalse(old.exists())
        self.assertEqual((new.read_bytes(),sidecar_path(new).read_bytes()),before)

    def test_crash_before_final_copy_rebuilds_only_from_retained_original(self):
        old=self.legacy()[0]
        import comic_archive.cbz_conversion as conversion
        original=conversion._convert
        def crash(*args):
            original(*args); raise KeyboardInterrupt
        with patch.object(conversion,'_convert',crash),self.assertRaises(KeyboardInterrupt):
            convert_comics(self.root,min_free_bytes=0)
        old.with_name(old.name[:-8]+'.cbz').unlink()
        result=convert_comics(self.root,min_free_bytes=0)
        self.assertEqual(result['converted_comics'],1); self.assertEqual(verify(self.root),[])

    def test_corrupt_or_trailing_source_data_never_replaces_or_deletes_original(self):
        for data in (b'not zstd', None):
            if self.root.exists():
                import shutil
                shutil.rmtree(self.root)
            old=self.legacy()[0]
            old.write_bytes(data if data else old.read_bytes()+b'unexpected trailing bytes')
            before=old.read_bytes()
            with self.assertRaises(ArchiveError): convert_comics(self.root,remove_originals=True,min_free_bytes=0)
            self.assertEqual(old.read_bytes(),before); self.assertFalse(old.with_name(old.name[:-8]+'.cbz').exists())

    def test_low_space_existing_destination_and_symlink_are_held(self):
        old=self.legacy()[0]
        with patch('comic_archive.cbz_conversion.shutil.disk_usage',return_value=Mock(free=0)),self.assertRaises(RunLimit):
            convert_comics(self.root,remove_originals=True)
        new=old.with_name(old.name[:-8]+'.cbz');new.write_bytes(b'keep')
        with self.assertRaises(ArchiveError): convert_comics(self.root)
        self.assertEqual(new.read_bytes(),b'keep');new.unlink();new.symlink_to(self.root/'outside')
        with self.assertRaises(ArchiveError): convert_comics(self.root)
        self.assertTrue(old.is_file())

    def test_invalid_options_and_modified_checkpoint_are_rejected(self):
        self.legacy()
        for options in ({'limit':0},{'remove_originals':1},{'min_free_bytes':-1}):
            with self.assertRaises(ArchiveError): convert_comics(self.root,**options)
        checkpoint=self.root/'.archive-state/cbz-conversion-pending.json'
        checkpoint.write_text(json.dumps({'original':'../outside'}))
        with self.assertRaises(ArchiveError): convert_comics(self.root)


    def checkpoint(self, old, **changes):
        stat = old.stat(); new = old.with_name(old.name[:-8]+'.cbz')
        record = {'schema_version':1,'original':old.relative_to(self.root).as_posix(),
                  'converted':new.relative_to(self.root).as_posix(),'metadata':import_metadata(sidecar_path(old)),
                  'original_stat':[stat.st_size,stat.st_mtime_ns,stat.st_ino],'remove_originals':False} | changes
        path = self.root/'.archive-state/cbz-conversion-pending.json'
        path.write_text(json.dumps(record))
        return path, new

    def test_tampered_checkpoints_are_refused_and_never_cost_the_original(self):
        old = self.legacy()[0]; before = (old.read_bytes(), sidecar_path(old).read_bytes())
        metadata = import_metadata(sidecar_path(old)); name = old.name; folder = old.parent.name
        unsafe = {'converted outside the folder': {'converted':'Elsewhere/'+name[:-8]+'.cbz'},
                  'original at the library top': {'original':name,'converted':name[:-8]+'.cbz'},
                  'original in a private folder': {'original':'.archive-state/'+name,'converted':'.archive-state/'+name[:-8]+'.cbz'},
                  'wrong converted name': {'converted':folder+'/Other.cbz'},
                  'original is not a tar': {'original':folder+'/'+name[:-8]+'.cbz','converted':folder+'/'+name[:-8]+'.cbz'}}
        for label, change in unsafe.items():
            with self.subTest(label):
                checkpoint, new = self.checkpoint(old, **change)
                with self.assertRaisesRegex(ArchiveError, 'Unsafe CBZ conversion paths'): convert_comics(self.root, min_free_bytes=0)
                self.assertTrue(checkpoint.is_file()); self.assertEqual((old.read_bytes(), sidecar_path(old).read_bytes()), before)
                self.assertEqual(list(self.root.glob('*/*.cbz')), [])
        for label, changed in {'wrong format': metadata|{'format':'cbz'}, 'bad export id': metadata|{'export_id':'x'}}.items():
            with self.subTest(label):
                checkpoint, new = self.checkpoint(old, metadata=changed)
                with self.assertRaisesRegex(ArchiveError, 'Invalid original comic identity'): convert_comics(self.root, min_free_bytes=0)
                self.assertEqual(list(self.root.glob('*/*.cbz')), [])
        checkpoint, new = self.checkpoint(old, original_stat=[0, 0, 0])
        with self.assertRaisesRegex(ArchiveError, 'changed during conversion'): convert_comics(self.root, min_free_bytes=0)
        self.assertTrue(checkpoint.is_file()); self.assertTrue(new.is_file())
        self.assertEqual((old.read_bytes(), sidecar_path(old).read_bytes()), before)

    def test_recovery_refuses_a_damaged_converted_copy_and_a_vanished_original(self):
        old = self.legacy()[0]; before = (old.read_bytes(), sidecar_path(old).read_bytes())
        checkpoint, new = self.checkpoint(old)
        new.write_bytes(b'half written')
        with self.assertRaisesRegex(ArchiveError, 'Converted CBZ failed verification'): convert_comics(self.root, min_free_bytes=0)
        self.assertEqual((old.read_bytes(), sidecar_path(old).read_bytes()), before)
        new.unlink(); old.rename(old.with_name('hidden.bin'))
        with self.assertRaisesRegex(ArchiveError, 'Original is unavailable'): convert_comics(self.root, min_free_bytes=0)
        self.assertTrue(checkpoint.is_file())

    def test_recovery_without_a_retained_original_keeps_the_checkpoint(self):
        old = self.legacy()[0]
        import comic_archive.cbz_conversion as conversion
        original = conversion._convert
        def crash(*args):
            original(*args); raise KeyboardInterrupt
        with patch.object(conversion, '_convert', crash), self.assertRaises(KeyboardInterrupt):
            convert_comics(self.root, min_free_bytes=0)
        old.unlink()
        with self.assertRaisesRegex(ArchiveError, 'Retained original is unavailable'): convert_comics(self.root, min_free_bytes=0)
        self.assertTrue((self.root/'.archive-state/cbz-conversion-pending.json').is_file())
        self.assertTrue(old.with_name(old.name[:-8]+'.cbz').is_file())

    def test_conversion_interrupted_after_retiring_the_original_completes_on_restart(self):
        old = self.legacy()[0]
        checkpoint, new = self.checkpoint(old); pending = checkpoint.read_text()
        first = convert_comics(self.root, min_free_bytes=0)
        self.assertEqual(first['retained_originals'], 1)
        retained = self.root/'.archive-state/legacy-exports'/old.parent.name/old.name
        self.assertTrue(retained.is_file()); self.assertFalse(old.exists())
        saved = (new.read_bytes(), retained.read_bytes())
        checkpoint.write_text(pending)  # the process died before it could clear its checkpoint
        again = convert_comics(self.root, min_free_bytes=0)
        self.assertEqual((again['converted_comics'], again['retained_originals'], again['remaining_tar_comics']), (1, 1, 0))
        self.assertFalse(checkpoint.exists()); self.assertEqual((new.read_bytes(), retained.read_bytes()), saved)
        self.assertEqual(verify(self.root), [])

    def test_conversion_stops_when_inventory_knows_the_episode_under_another_series(self):
        old = self.legacy()[0]; before = old.read_bytes()
        with Inventory(self.root) as inventory:
            inventory.observe('99', 'Other', [{'id':'81','title':'Chapter 0','order':1,'access_state':'tapas_free'}], 'fixture')
        with self.assertRaisesRegex(ArchiveError, 'differs from inventory'): convert_comics(self.root, min_free_bytes=0)
        self.assertEqual(old.read_bytes(), before)
        with Inventory(self.root) as inventory:
            self.assertEqual(inventory.db.execute('SELECT status FROM episodes WHERE id=?', ('81',)).fetchone()[0], 'queued')

    def test_an_existing_retained_copy_is_never_overwritten(self):
        old = self.legacy()[0]; before = old.read_bytes()
        retained = self.root/'.archive-state/legacy-exports'/old.parent.name/old.name
        retained.parent.mkdir(parents=True); retained.write_bytes(b'earlier retained copy')
        with self.assertRaisesRegex(ArchiveError, 'Retained tar already exists'): convert_comics(self.root, min_free_bytes=0)
        self.assertEqual(old.read_bytes(), before); self.assertEqual(retained.read_bytes(), b'earlier retained copy')

    def test_original_tar_that_disagrees_with_its_manifest_is_never_converted(self):
        old = self.legacy()[0]; page = png(1)
        altered = bytearray(page); altered[40] ^= 1
        cases = {'different member name': [('0002.png', page)], 'different member size': [('0001.png', page+b'x')],
                 'different page bytes': [('0001.png', bytes(altered))],
                 'extra page': [('0001.png', page), ('0002.png', png(2))]}
        messages = {'different member name': 'membership differs', 'different member size': 'membership differs',
                    'different page bytes': 'page bytes differ', 'extra page': 'unexpected pages'}
        for label, members in cases.items():
            with self.subTest(label):
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as tar:
                    for member_name, data in members:
                        info = tarfile.TarInfo(member_name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
                old.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(raw.getvalue()))
                before = old.read_bytes()
                with self.assertRaisesRegex(ArchiveError, messages[label]): convert_comics(self.root, remove_originals=True, min_free_bytes=0)
                self.assertEqual(old.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in old.parent.iterdir() if p.name.endswith(('.cbz', '.tmp'))), [])
                self.assertFalse((self.root/'.archive-state/cbz-conversion-pending.json').exists())

    def test_oversized_checkpoint_unverified_series_and_unexpected_format_hold_conversion(self):
        old = self.legacy()[0]; before = (old.read_bytes(), sidecar_path(old).read_bytes())
        checkpoint = self.root/'.archive-state/cbz-conversion-pending.json'
        with checkpoint.open('wb') as output: output.truncate(16*1024**2+1)
        with self.assertRaisesRegex(ArchiveError, 'Oversized'): convert_comics(self.root, min_free_bytes=0)
        checkpoint.unlink()
        series = old.parent/'series.json'; good = series.read_text(); series.write_text('{broken')
        with self.assertRaisesRegex(ArchiveError, 'Series failed verification'): convert_comics(self.root, min_free_bytes=0)
        series.write_text(good)
        self.assertEqual((old.read_bytes(), sidecar_path(old).read_bytes()), before)
        configure(self.root, purge_completed_cache=False)
        with closing(PublicationStore(self.root)) as store:
            store.publish_metadata(sidecar_path(old), import_metadata(sidecar_path(old))|{'format':'cbz'})
        with self.assertRaisesRegex(ArchiveError, 'Unexpected original format'): convert_comics(self.root, min_free_bytes=0)
        self.assertEqual(old.read_bytes(), before[0]); self.assertEqual(list(self.root.glob('*/*.cbz')), [])
