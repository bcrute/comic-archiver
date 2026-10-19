from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
import random
from dataclasses import replace
import unittest
from unittest.mock import Mock

from PIL import Image

from comic_archive.archive import digest, episode_archives, sidecar_path, verify, verify_episode
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata
from comic_archive.purge import archive_page, evict_episode
from comic_archive.storage import DEFAULTS, policy
from comic_archive.image_profiles import compress_image
from test_archive import MemoryAdapter, png


def photo(width=512, height=512):
    # Deterministic, detailed synthetic image with no source artwork.
    image = Image.frombytes('RGB', (width, height), random.Random(21).randbytes(width * height * 3))
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def set_policy(root, **changes):
    # Nothing in the app writes these settings any more; a library may still hold them.
    path = Path(root) / '.archive-state/storage-policy.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULTS | changes))


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

    def test_quality_switch_affects_new_episodes_and_preserves_existing_full_quality_exports(self):
        image = photo()
        adapter = MemoryAdapter();adapter.retrieve = Mock(return_value=image)
        archive = self.publish(adapter=adapter)
        before = archive.read_bytes(),sidecar_path(archive).read_bytes()
        set_policy(self.root,image_profile='small')
        self.publish(adapter=adapter)
        self.assertEqual((archive.read_bytes(),sidecar_path(archive).read_bytes()),before)
        self.assertEqual(json.loads(sidecar_path(archive).read_text())['image_profile'],'original')

    def test_local_episode_ids_reused_in_different_series_keep_independent_quality_choices(self):
        image = photo();adapter = MemoryAdapter();adapter.retrieve = Mock(return_value=image)
        original = self.publish(adapter=adapter)
        set_policy(self.root,image_profile='small')
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

    def test_evicted_shared_page_remains_recoverable_for_another_incomplete_import(self):
        set_policy(self.root, purge_completed_cache=False)
        archive = self.publish()
        second = replace(self.episode, id='82')
        store = PublicationStore(self.root)
        try:
            store.remember_page('local', second, second.pages[0], png(1))
            evict_episode(store, archive, import_metadata(sidecar_path(archive)))
        finally:
            store.close()
        self.assertFalse((self.root / '.archive-cache' / digest(png(1))).exists())
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

    def test_quality_preferences_are_read_from_saved_settings_and_bad_settings_hold(self):
        self.assertEqual(policy(self.root)['image_profile'], 'original')
        set_policy(self.root, image_profile='small')
        self.assertEqual(policy(self.root)['image_profile'], 'small')
        settings = self.root / '.archive-state/storage-policy.json'
        for value in ('unknown', {}, None):
            set_policy(self.root, image_profile=value)
            with self.subTest(value), self.assertRaises(ArchiveError):policy(self.root)
        set_policy(self.root, purge_completed_cache='true')
        with self.assertRaises(ArchiveError):policy(self.root)
        settings.write_text('{broken')
        with self.assertRaises(ArchiveError):policy(self.root)

    def test_compact_import_changes_images_only_after_explicit_choice_and_verifies_new_hashes(self):
        data = photo()
        adapter = Mock(); adapter.retrieve.return_value = data
        set_policy(self.root, image_profile='small')
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

