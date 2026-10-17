from contextlib import closing
import io
import json
from pathlib import Path
import shutil
import tempfile
import random
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from support import PRIVATE_FILE
from comic_archive.archive import Store, digest, episode_archives, sidecar_path, verify, verify_episode
from comic_archive.inventory import Inventory
from comic_archive.migration import migrate_exports
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata
from comic_archive.purge import purge_library, archive_page
from comic_archive.storage import DEFAULTS, configure, policy, storage_plan
from comic_archive.image_profiles import compress_image
from test_archive import MemoryAdapter, png


def photo(width=512, height=512):
    # Deterministic, detailed synthetic image with no source artwork.
    image = Image.frombytes('RGB', (width, height), random.Random(21).randbytes(width * height * 3))
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'
        self.series = Series('42', 'Synthetic', source='local')
        self.episode = Episode('81', '42', 'Chapter', 1, (Page(1, 'https://cdn.test/a', False),), 'local_owned')
        self.manifest = Manifest(self.series, (self.episode,))

    def publish(self, cls=PublicationStore, adapter=None):
        store = cls(self.root)
        try:
            folder = store.archive(self.manifest, adapter or MemoryAdapter(), progress=lambda _:None)
            return episode_archives(folder)[0]
        finally:
            store.close()

    def test_original_default_evicts_only_completed_media_and_reuses_archives_on_restart(self):
        archive = self.publish()
        before = archive.read_bytes(), sidecar_path(archive).read_bytes()
        self.assertFalse((self.root / '.archive-cache' / digest(png(1))).exists())
        adapter = Mock()
        self.publish(adapter=adapter)
        adapter.retrieve.assert_not_called()
        self.assertEqual((archive.read_bytes(), sidecar_path(archive).read_bytes()), before)
        self.assertFalse((self.root / '.archive-cache' / digest(png(1))).exists())
        self.assertEqual(verify(self.root), [])

    def test_measured_episode_statistics_separate_formats_and_do_not_assume_compression(self):
        comic=self.publish()
        novel=replace(self.episode,id='82',order=2,pages=(),content_type='novel',text_html='<p>Sample chapter.</p>')
        store=PublicationStore(self.root)
        try:
            folder=store.archive(Manifest(self.series,(novel,)),MemoryAdapter(),progress=lambda _:None)
            epub=store.episode_path(self.series,novel,folder)
        finally:store.close()
        report=storage_plan(self.root,episode_count=3)
        self.assertEqual(report['saved_archive_formats'],{'cbz':1,'tar.zst':0,'epub':1})
        for kind,path in (('comic',comic),('novel',epub)):
            sample=report['episode_size_statistics'][kind]
            self.assertEqual(sample['sample_count'],1)
            self.assertEqual(sample['mean_bytes'],path.stat().st_size)
            self.assertEqual(sample['p10_bytes'],path.stat().st_size)
            self.assertEqual(sample['p90_bytes'],path.stat().st_size)
        self.assertEqual(report['estimated_additional_archive_bytes'],3*comic.stat().st_size)
        self.assertFalse(report['compression_savings_verified'])

    def test_quality_switch_affects_new_episodes_and_preserves_existing_full_quality_exports(self):
        image = photo()
        adapter = MemoryAdapter();adapter.retrieve = Mock(return_value=image)
        archive = self.publish(adapter=adapter)
        before = archive.read_bytes(),sidecar_path(archive).read_bytes()
        configure(self.root,image_profile='small')
        self.publish(adapter=adapter)
        self.assertEqual((archive.read_bytes(),sidecar_path(archive).read_bytes()),before)
        self.assertEqual(json.loads(sidecar_path(archive).read_text())['image_profile'],'original')

    def test_local_episode_ids_reused_in_different_series_keep_independent_quality_choices(self):
        image = photo();adapter = MemoryAdapter();adapter.retrieve = Mock(return_value=image)
        original = self.publish(adapter=adapter)
        configure(self.root,image_profile='small')
        store = PublicationStore(self.root)
        try:
            store.archive(self.manifest,adapter,progress=lambda _:None)
            series = replace(self.series,id='43',title='Another local collection')
            episode = replace(self.episode,series_id='43')
            folder = store.archive(Manifest(series,(episode,)),adapter,progress=lambda _:None)
            compact = episode_archives(folder)[0]
            self.assertEqual(json.loads(sidecar_path(original).read_text())['image_profile'],'original')
            metadata = json.loads(sidecar_path(compact).read_text())
            self.assertEqual(metadata['image_profile'],'small');self.assertTrue(metadata['images_recompressed'])
            self.assertEqual(verify_episode(compact),[])
        finally:store.close()

    def test_purge_preview_and_apply_preserve_partial_media_private_state_and_ids(self):
        configure(self.root, purge_completed_cache=False)
        archive = self.publish()
        incomplete = self.root / '.archive-cache' / digest(png(2))
        incomplete.write_bytes(png(2))
        original = archive.read_bytes(), sidecar_path(archive).read_bytes()
        preview = purge_library(self.root)
        self.assertFalse(preview['applied'])
        self.assertEqual(preview['cache_files'], 1)
        self.assertTrue((self.root / '.archive-cache' / digest(png(1))).is_file())
        result = purge_library(self.root, apply=True)
        self.assertGreater(result['removed_bytes'], 0)
        self.assertTrue(incomplete.is_file())
        self.assertTrue((self.root / '.archive-cache/index.sqlite3').is_file())
        self.assertTrue((self.root / '.archive-state/provenance').is_dir())
        self.assertEqual((archive.read_bytes(), sidecar_path(archive).read_bytes()), original)
        self.assertEqual(purge_library(self.root, apply=True)['cache_files'], 0)

    def test_corrupt_export_stops_before_any_cache_or_legacy_deletion(self):
        configure(self.root, purge_completed_cache=False)
        archive = self.publish()
        blob = self.root / '.archive-cache' / digest(png(1))
        archive.write_bytes(b'corrupt')
        with self.assertRaises(ArchiveError):
            purge_library(self.root, apply=True, include_legacy=True)
        self.assertEqual(blob.read_bytes(), png(1))

    def test_symlinked_cache_and_library_are_rejected_without_deletion(self):
        configure(self.root, purge_completed_cache=False)
        self.publish()
        outside = self.root.parent / 'outside'
        outside.write_bytes(b'keep')
        blob = self.root / '.archive-cache' / digest(png(1))
        blob.unlink(); blob.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            purge_library(self.root, apply=True)
        alias = self.root.parent / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            storage_plan(alias)
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_converted_legacy_is_deleted_only_when_equivalent_export_is_verified(self):
        self.publish(cls=Store)
        migrate_exports(self.root)
        retired = next((self.root / '.archive-state/legacy-exports').glob('*/*.cbz'))
        self.assertTrue(retired.is_file())
        report = purge_library(self.root, include_legacy=True)
        self.assertEqual(report['legacy_files'], 2)
        self.assertTrue(retired.is_file())
        purge_library(self.root, include_legacy=True, apply=True)
        self.assertFalse(retired.exists())
        self.assertEqual(verify(self.root), [])

    def test_unmatched_or_different_legacy_is_retained(self):
        self.publish(cls=Store)
        migrate_exports(self.root)
        retired = next((self.root / '.archive-state/legacy-exports').glob('*/*.cbz'))
        sidecar = sidecar_path(retired)
        metadata = json.loads(sidecar.read_text()); metadata['title'] = 'Different title'
        sidecar.write_text(json.dumps(metadata))
        result = purge_library(self.root, include_legacy=True, apply=True)
        self.assertEqual(result['retained_legacy_episodes'], 1)
        self.assertTrue(retired.is_file())

    def test_evicted_shared_page_remains_recoverable_for_another_incomplete_import(self):
        configure(self.root, purge_completed_cache=False)
        self.publish()
        second = replace(self.episode, id='82')
        store = PublicationStore(self.root)
        try:
            store.remember_page('local', second, second.pages[0], png(1))
        finally:
            store.close()
        purge_library(self.root, apply=True)
        store = PublicationStore(self.root)
        try:
            self.assertEqual(store.cached_page('local', second, second.pages[0]), png(1))
        finally:
            store.close()

    def test_damaged_recovery_archive_cannot_return_corrupt_cached_bytes(self):
        archive = self.publish()
        import_metadata(sidecar_path(archive))
        record = (str(archive.relative_to(self.root)), '0001.png', len(png(1)))
        archive.write_bytes(b'corrupt')
        self.assertIsNone(archive_page(self.root, record, digest(png(1))))
        self.assertIsNone(archive_page(self.root, ('../outside', '0001.png', 1), digest(png(1))))

    def test_single_location_cache_index_migrates_without_losing_verified_recovery(self):
        import sqlite3
        self.publish()
        database = self.root/'.archive-cache/index.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:  # Closed, not only committed: Windows keeps an open file in place.
            record = db.execute('SELECT hash,path,member,size FROM archived_pages').fetchone()
            db.execute('DROP TABLE archived_pages')
            db.execute('CREATE TABLE archived_pages (hash TEXT PRIMARY KEY,path TEXT,member TEXT,size INTEGER)')
            db.execute('INSERT INTO archived_pages VALUES (?,?,?,?)',record)
        store = PublicationStore(self.root)
        try:
            self.assertEqual(store.cached_page('local',self.episode,self.episode.pages[0]),png(1))
            self.assertEqual([r[1] for r in store.db.execute('PRAGMA table_info(archived_pages)') if r[5]],['hash','path','member'])
        finally:store.close()

    def test_fresh_estimate_is_read_only_and_never_claims_unseen_inventory_complete(self):
        with patch('comic_archive.storage.shutil.disk_usage', return_value=Mock(free=10*1024**3)):
            plan = storage_plan(self.root, episode_count=100)
        self.assertFalse(self.root.exists())
        self.assertFalse(plan['inventory_complete'])
        self.assertEqual(plan['estimate_basis'], 'rough_reference_sample')
        self.assertEqual(plan['estimated_additional_archive_bytes'], 100*20_586_261)
        self.assertTrue(plan['fits_estimate'])
        with patch('comic_archive.storage.shutil.disk_usage', return_value=Mock(free=1024)):
            self.assertEqual(storage_plan(self.root, episode_count=100)['recommendation'], 'compress_or_use_pc')
        for value in (True, -1, 10_000_001):
            with self.assertRaises(ArchiveError):storage_plan(self.root, episode_count=value)

    def test_estimate_uses_current_archive_bytes_and_known_pending_only(self):
        archive = self.publish()
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Synthetic', [{'id':'82','title':'Next','order':2,'access_state':'tapas_free'}], 'test')
        plan = storage_plan(self.root)
        self.assertEqual(plan['saved_episode_count'], 1)
        self.assertEqual(plan['saved_archive_bytes'], archive.stat().st_size)
        self.assertEqual(plan['known_pending_episode_count'], 1)
        self.assertEqual(plan['estimated_additional_archive_bytes'], archive.stat().st_size)

    def test_quality_preferences_are_explicit_and_private_and_bad_settings_hold(self):
        self.assertEqual(policy(self.root)['image_profile'], 'original')
        configure(self.root, image_profile='small')
        self.assertEqual(policy(self.root)['image_profile'], 'small')
        settings = self.root / '.archive-state/storage-policy.json'
        self.assertEqual(settings.stat().st_mode & 0o777, PRIVATE_FILE)
        for value in ('unknown', {}, None):
            with self.assertRaises(ArchiveError):configure(self.root, image_profile=value)
        with self.assertRaises(ArchiveError):configure(self.root, purge_completed_cache='true')
        settings.write_text('{broken')
        with self.assertRaises(ArchiveError):policy(self.root)

    def test_compact_import_changes_images_only_after_explicit_choice_and_verifies_new_hashes(self):
        data = photo()
        adapter = Mock(); adapter.retrieve.return_value = data
        configure(self.root, image_profile='small')
        archive = self.publish(adapter=adapter)
        meta = import_metadata(sidecar_path(archive))
        self.assertTrue(meta['images_recompressed'])
        self.assertEqual(meta['image_profile'], 'small')
        self.assertLess(meta['content_hashes'][0]['size'], len(data))
        self.assertEqual(meta['original_content_hashes'][0]['sha256'], digest(data))
        self.assertFalse((self.root / '.archive-cache' / digest(data)).exists())
        self.assertEqual(verify(self.root, decode_images=True), [])

    def test_original_animation_tiny_pages_and_colour_modes_remain_intact(self):
        data = photo(64, 64)
        self.assertEqual(compress_image(data, 'original'), data)
        self.assertEqual(compress_image(png(1), 'small'), png(1))
        animated = io.BytesIO()
        Image.new('RGB',(10,10),'red').save(animated,'GIF',save_all=True,append_images=[Image.new('RGB',(10,10),'blue')])
        self.assertEqual(compress_image(animated.getvalue(), 'balanced'), animated.getvalue())
        with self.assertRaises(ArchiveError):compress_image(b'not an image','small')

    def test_smaller_images_preserve_long_strip_shape_and_transparency(self):
        data = photo(1800, 40)
        compressed = compress_image(data, 'balanced')
        with Image.open(io.BytesIO(compressed)) as image:
            self.assertEqual(image.size, (1600, 36))
        transparent = io.BytesIO()
        Image.new('RGBA', (256,256),(40,70,80,120)).save(transparent,'PNG')
        compressed = compress_image(transparent.getvalue(),'small')
        with Image.open(io.BytesIO(compressed)) as image:
            self.assertIn('A',image.getbands())

    def test_symlinked_oversized_or_malformed_settings_hold_and_are_never_rewritten(self):
        state = self.root / '.archive-state'
        state.mkdir(parents=True)
        path = state / 'storage-policy.json'
        target = self.root.parent / 'real-policy.json'
        target.write_text(json.dumps(DEFAULTS))
        path.symlink_to(target)
        with self.assertRaises(ArchiveError): policy(self.root)
        with self.assertRaises(ArchiveError): configure(self.root, image_profile='small')
        self.assertEqual(json.loads(target.read_text()), DEFAULTS)
        path.unlink()
        path.write_text(json.dumps(DEFAULTS) + ' ' * 4096)
        with self.assertRaises(ArchiveError): policy(self.root)
        for value in ({'schema_version': 1, 'image_profile': 'original'}, DEFAULTS | {'schema_version': 2},
                      DEFAULTS | {'schema_version': True}, DEFAULTS | {'purge_completed_cache': 'yes'},
                      DEFAULTS | {'image_profile': 'huge'}, [], 5):
            path.write_text(json.dumps(value))
            with self.subTest(value), self.assertRaises(ArchiveError): policy(self.root)
        path.write_text(json.dumps(DEFAULTS))
        self.assertEqual(policy(self.root), DEFAULTS)

    def test_plan_counts_only_readable_archives_inside_real_series_folders(self):
        archive = self.publish()
        folder = archive.parent
        sidecar = sidecar_path(archive)
        (folder / 'garbled.cbz').write_bytes(b'x' * 10)
        (folder / 'garbled.json').write_text('{nope')
        (folder / 'listed.cbz').write_bytes(b'x' * 10)
        (folder / 'listed.json').write_text('[1]')
        (folder / 'audio.cbz').write_bytes(b'x' * 10)
        (folder / 'audio.json').write_text(json.dumps({'content_type': 'audio'}))
        (folder / 'huge.cbz').write_bytes(b'x' * 10)
        with (folder / 'huge.json').open('wb') as output:
            output.truncate(16 * 1024 ** 2 + 1)
        (folder / 'linked.cbz').symlink_to(archive)
        shutil.copy(sidecar, folder / 'linked.json')
        (folder / 'linked-sidecar.cbz').write_bytes(b'x' * 10)
        (folder / 'linked-sidecar.json').symlink_to(sidecar)
        (self.root / 'alias').symlink_to(folder, target_is_directory=True)
        plan = storage_plan(self.root)
        self.assertEqual(plan['saved_episode_count'], 1)
        self.assertEqual(plan['saved_archive_bytes'], archive.stat().st_size)
        self.assertEqual(plan['saved_archive_formats'], {'cbz': 1, 'tar.zst': 0, 'epub': 0})
        self.assertEqual(plan['episode_size_statistics']['comic']['sample_count'], 1)
        for kind in ('audio', None):
            with self.assertRaises(ArchiveError): storage_plan(self.root, episode_kind=kind)
