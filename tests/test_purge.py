from contextlib import closing
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from comic_archive.archive import Store, digest, episode_archives, sidecar_path, verify_episode
from comic_archive.migration import migrate_exports
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata, write_compressed_comic
from comic_archive.purge import archive_page, evict_episode, purge_library
from comic_archive.storage import configure
from test_archive import MemoryAdapter, png


class PurgeTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.work = Path(work.name); self.count = 0
        self.series = Series('42', 'Synthetic', source='local')
        self.episode = Episode('81', '42', 'Chapter', 1, (Page(1, 'https://cdn.test/a', False),), 'local_owned')
        self.new_library()

    def new_library(self):
        self.count += 1
        self.root = self.work/f'Library{self.count}'
        configure(self.root, purge_completed_cache=False)

    def publish(self, cls=PublicationStore, episode=None):
        store = cls(self.root)
        try:
            folder = store.archive(Manifest(self.series, (episode or self.episode,)), MemoryAdapter(), progress=lambda _: None)
            return episode_archives(folder)[0]
        finally:
            store.close()

    @property
    def blob(self):
        return self.root/'.archive-cache'/digest(png(1))

    def assert_untouched(self):
        self.assertTrue(self.blob.is_file())
        self.assertFalse((self.root/'.archive-state/purge.json').exists())

    def test_unclear_arguments_are_refused_before_anything_is_read_or_created(self):
        self.publish()
        for options in ({'apply': 'yes'}, {'apply': 1}, {'include_legacy': 1}):
            with self.subTest(options=options), self.assertRaises(ArchiveError):
                purge_library(self.root, **options)
        self.assert_untouched()
        absent = self.work/'absent'
        with self.assertRaises(ArchiveError): purge_library(absent, apply=True)
        self.assertFalse(absent.exists())

    def test_an_unverifiable_or_ambiguous_library_stops_before_any_deletion(self):
        archive = self.publish()
        series = archive.parent/'series.json'; good = series.read_bytes()
        series.write_text('{broken')
        with self.assertRaisesRegex(ArchiveError, 'Series verification'): purge_library(self.root, apply=True)
        self.assert_untouched(); series.write_bytes(good)
        moved = self.work/'moved'; archive.parent.rename(moved); archive.parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, 'Series verification'): purge_library(self.root, apply=True)
        self.assert_untouched()
        self.new_library()
        original = self.publish(Store)
        duplicate = original.with_name('duplicate.cbz')
        shutil.copy2(original, duplicate); shutil.copy2(sidecar_path(original), sidecar_path(duplicate))
        with self.assertRaisesRegex(ArchiveError, 'Duplicate export identity'): purge_library(self.root, apply=True)
        self.assertTrue(original.is_file()); self.assertTrue(duplicate.is_file())
        self.assertFalse((self.root/'.archive-state/purge.json').exists())

    def test_symlinked_cache_or_retained_export_directories_are_refused(self):
        self.publish()
        outside = self.work/'outside'; outside.mkdir(); (outside/'keep').write_bytes(b'keep')
        cache = self.root/'.archive-cache'; moved = self.work/'cache'
        cache.rename(moved); cache.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, 'Cache directory'): purge_library(self.root, apply=True)
        self.assertTrue((moved/digest(png(1))).is_file())
        cache.unlink(); moved.rename(cache)
        legacy = self.root/'.archive-state/legacy-exports'; legacy.symlink_to(outside, target_is_directory=True)
        for include in (False, True):
            with self.subTest(include_legacy=include), self.assertRaisesRegex(ArchiveError, 'Retained export directories'):
                purge_library(self.root, apply=True, include_legacy=include)
        self.assertEqual([p.name for p in outside.iterdir()], ['keep'])
        self.assert_untouched()

    def test_unexpected_entry_among_retained_exports_stops_legacy_cleanup_only_when_requested(self):
        self.publish()
        legacy = self.root/'.archive-state/legacy-exports'; legacy.mkdir()
        (legacy/'stray.txt').write_text('keep')
        purge_library(self.root)
        with self.assertRaisesRegex(ArchiveError, 'Unsafe retained export entry'):
            purge_library(self.root, apply=True, include_legacy=True)
        self.assertEqual((legacy/'stray.txt').read_text(), 'keep'); self.assert_untouched()

    def test_corrupt_retained_legacy_copy_is_kept_and_counted(self):
        self.publish(Store); migrate_exports(self.root)
        retired = next((self.root/'.archive-state/legacy-exports').glob('*/*.cbz'))
        retired.write_bytes(b'broken')
        report = purge_library(self.root, include_legacy=True, apply=True)
        self.assertEqual((report['retained_legacy_episodes'], report['legacy_files']), (1, 0))
        self.assertEqual(retired.read_bytes(), b'broken'); self.assertTrue(sidecar_path(retired).is_file())

    def test_retained_legacy_novel_is_deleted_only_for_the_matching_converted_export(self):
        novel = replace(self.episode, pages=(), content_type='novel', text_html='<p>Sample chapter.</p>')
        self.publish(Store, novel); migrate_exports(self.root)
        retired = next((self.root/'.archive-state/legacy-exports').glob('*/*.epub'))
        sidecar = sidecar_path(retired); original, original_sidecar = retired.read_bytes(), sidecar.read_bytes()
        # The same episode saved elsewhere with different (same-length) text: a complete, valid legacy export of its own.
        mine = self.root; self.new_library()
        other = self.publish(Store, replace(novel, text_html='<p>Sample chapter!</p>'))
        self.root = mine
        shutil.copy2(other, retired); shutil.copy2(sidecar_path(other), sidecar)
        self.assertEqual(verify_episode(retired), [])
        self.assertEqual(purge_library(self.root, include_legacy=True, apply=True)['retained_legacy_episodes'], 1)
        self.assertTrue(retired.is_file()); self.assertTrue(sidecar.is_file())
        retired.write_bytes(original); sidecar.write_bytes(original_sidecar)
        report = purge_library(self.root, include_legacy=True, apply=True)
        self.assertEqual(report['legacy_files'], 2); self.assertFalse(retired.exists())

    def test_every_page_of_a_compressed_archive_can_be_recovered_by_its_own_name_and_hash(self):
        two_pages = replace(self.episode, pages=(Page(1, 'https://cdn.test/a', False), Page(2, 'https://cdn.test/b', False)))
        cbz = self.publish(episode=two_pages); metadata = import_metadata(sidecar_path(cbz)) | {'format': 'tar.zst'}
        compressed = cbz.with_suffix('.tar.zst')
        with zipfile.ZipFile(cbz) as comic: write_compressed_comic(compressed, metadata, lambda page: comic.open(page['file']))
        cbz.unlink()
        with closing(PublicationStore(self.root)) as store: store.publish_metadata(sidecar_path(compressed), metadata)
        relative = compressed.relative_to(self.root).as_posix()
        for item, expected in zip(metadata['content_hashes'], (png(1), png(2)), strict=True):
            with self.subTest(page=item['file']):
                self.assertEqual(archive_page(self.root, (relative, item['file'], item['size']), item['sha256']), expected)
        first, second = metadata['content_hashes']
        self.assertIsNone(archive_page(self.root, (relative, second['file'], second['size']), first['sha256']))

    def test_recovery_of_an_original_page_requires_the_exact_recorded_bytes(self):
        archive = self.publish(); meta = import_metadata(sidecar_path(archive))
        item = meta['content_hashes'][0]; relative = archive.relative_to(self.root).as_posix()
        record = (relative, item['file'], item['size'])
        self.assertEqual(digest(archive_page(self.root, record, item['sha256'])), item['sha256'])
        self.assertIsNone(archive_page(self.root, record, '0'*64))
        self.assertIsNone(archive_page(self.root, (relative, item['file'], item['size']+1), item['sha256']))
        for unsafe in ('../x.cbz', f'.hidden/{archive.name}', archive.name):
            self.assertIsNone(archive_page(self.root, (unsafe, item['file'], item['size']), item['sha256']))
        link = archive.with_name('link.cbz'); link.symlink_to(archive)
        for missing in (archive.with_name('absent.cbz').relative_to(self.root).as_posix(), link.relative_to(self.root).as_posix()):
            self.assertIsNone(archive_page(self.root, (missing, item['file'], item['size']), item['sha256']))
        sidecar = sidecar_path(archive); moved = self.work/'sidecar.json'; sidecar.rename(moved)
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        moved.rename(sidecar)
        good = archive.read_bytes()
        def rewrite(data):
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(self.work/'rewritten.cbz', 'w') as target:
                for member in source.infolist():
                    target.writestr(member.filename, data if member.filename == item['file'] else source.read(member))
            archive.write_bytes((self.work/'rewritten.cbz').read_bytes())
        rewrite(png(1)+b'extra')
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        rewrite(b'\0'*item['size'])
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        archive.write_bytes(good)
        self.assertIsNotNone(archive_page(self.root, record, item['sha256']))

    def test_eviction_refuses_an_archive_that_no_longer_verifies_and_keeps_the_cached_original(self):
        archive = self.publish(); meta = import_metadata(sidecar_path(archive))
        archive.write_bytes(b'corrupt')
        store = PublicationStore(self.root)
        try:
            with self.assertRaisesRegex(ArchiveError, 'verification failed'): evict_episode(store, archive, meta)
        finally:
            store.close()
        self.assertTrue(self.blob.is_file())


if __name__ == '__main__':
    unittest.main()
