from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from comic_archive.archive import Store, digest, episode_archives, sidecar_path, verify
from comic_archive.errors import RunLimit
from comic_archive.inventory import Inventory
from comic_archive.migration import _series, migrate_exports
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata
from test_archive import MemoryAdapter, png


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'Library'
        self.series = Series('424242', 'Series', ('Author',), source_url='https://tapas.io/series/424242/info',
                             credits='Tapas credit', description='Synthetic', platform_id='424242')
        self.retained = self.root/'.archive-state/legacy-exports'

    def legacy(self, count=1, novel=False):
        episodes = tuple(Episode(str(818181+i), self.series.id, 'Chapter', i+1,
                                 () if novel else (Page(1, 'https://cdn.tapas.io/a', False),), 'tapas_unlocked',
                                 content_type='novel' if novel else 'comic', text_html='<p>Original text</p>' if novel else None)
                         for i in range(count))
        store = Store(self.root)
        try: return store.archive(Manifest(self.series, episodes), MemoryAdapter(), progress=lambda _: None)
        finally: store.close()

    def migrate(self, **options):
        return migrate_exports(self.root, min_free_bytes=0, **options)

    def test_unusable_arguments_are_refused_before_the_library_is_touched(self):
        folder = self.legacy()
        for options in ({'limit': 0}, {'limit': True}, {'limit': '1'}, {'min_free_bytes': -1},
                        {'min_free_bytes': True}, {'min_free_bytes': '0'}):
            with self.subTest(options=options), self.assertRaises(ArchiveError):
                migrate_exports(self.root, **({'min_free_bytes': 0} | options))
        alias = self.root.parent/'alias'; alias.symlink_to(self.root, target_is_directory=True)
        for root in (alias, self.root.parent/'absent'):
            with self.subTest(root=root.name), self.assertRaises(ArchiveError): migrate_exports(root, min_free_bytes=0)
        self.assertFalse((self.root/'.archive-state/legacy-exports').exists()); self.assertEqual(len(episode_archives(folder)), 1)

    def test_a_migrated_epub_package_keeps_the_namespace_prefixes_of_a_fresh_export(self):
        self.legacy(novel=True); self.migrate()
        epub = next(self.root.glob('*/*.epub'))
        with zipfile.ZipFile(epub) as archive: package = archive.read('EPUB/package.opf')
        self.assertNotIn(b'ns0:', package); self.assertIn(b'<package xmlns="http://www.idpf.org/2007/opf"', package)
        self.assertIn(('<dc:identifier id="book-id">urn:uuid:' + import_metadata(sidecar_path(epub))['export_id']).encode(), package)
        self.assertEqual(verify(self.root), [])

    def test_conversion_stops_with_originals_in_place_when_free_space_is_short(self):
        folder = self.legacy()
        with patch('comic_archive.migration.shutil.disk_usage', return_value=type('Space', (), {'free': 0})()):
            with self.assertRaises(RunLimit): self.migrate()
        self.assertEqual(len(episode_archives(folder)), 1)
        self.assertEqual(list(self.root.glob('*/*.cbz')), [next(folder.glob('*.cbz'))]); self.assertFalse((self.root/'.archive-state/export-retirement.json').exists())

    def test_progress_reports_each_converted_episode(self):
        self.legacy(count=2); seen = []
        self.migrate(progress=seen.append)
        self.assertEqual([item['converted_episodes'] for item in seen], [1, 2])
        self.assertEqual(verify(self.root), [])

    def test_conversion_updates_the_inventory_row_and_refuses_one_that_belongs_to_another_series(self):
        episode = {'id': '818181', 'title': 'Chapter', 'order': 1, 'access_state': 'tapas_unlocked'}
        with Inventory(self.root) as inventory:
            inventory.observe('424242', 'Series', [episode], 'fixture')
        folder = self.legacy()
        self.migrate()
        converted = next(self.root.glob('*/*.cbz'))
        with Inventory(self.root, read_only=True) as inventory:
            row = inventory.db.execute("SELECT status,archive_path FROM episodes WHERE id='818181'").fetchone()
        self.assertEqual((row[0], row[1]), ('archived', converted.relative_to(self.root).as_posix()))
        shutil.rmtree(self.root); other = self.root.parent/'Other'
        self.root = other
        with Inventory(self.root) as inventory:
            inventory.observe('999', 'Other series', [episode], 'fixture')
        folder = self.legacy(); original = next(folder.glob('*.cbz'))
        with self.assertRaisesRegex(ArchiveError, 'differs from inventory'): self.migrate()
        self.assertTrue(original.is_file()); self.assertTrue(sidecar_path(original).is_file())

    def test_episode_whose_series_identity_disagrees_with_its_folder_is_not_converted(self):
        folder = self.legacy(); sidecar = sidecar_path(next(folder.glob('*.cbz')))
        sidecar.write_text(json.dumps(json.loads(sidecar.read_text()) | {'series_id': '999'}))
        with self.assertRaisesRegex(ArchiveError, 'identity differs'): self.migrate()
        self.assertEqual(len(episode_archives(folder)), 1)

    def test_tampered_retirement_checkpoints_cannot_move_files_outside_the_series_layout(self):
        folder = self.legacy(); archive = next(folder.glob('*.cbz')); checkpoint = self.root/'.archive-state/export-retirement.json'
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        good = archive.relative_to(self.root).as_posix()
        for original in ('../outside.cbz', '.hidden/x.cbz', 'a/b/c.cbz'):
            with self.subTest(original=original):
                checkpoint.write_text(json.dumps({'original': original, 'converted': good, 'series': {}}))
                with self.assertRaisesRegex(ArchiveError, 'unsafe'): self.migrate()
        self.assertTrue(archive.is_file()); self.assertFalse((self.root.parent/'outside.cbz').exists())

    def test_retained_original_that_differs_is_never_overwritten(self):
        folder = self.legacy(); archive = next(folder.glob('*.cbz')); before = archive.read_bytes()
        kept = self.retained/folder.name/archive.name; kept.parent.mkdir(parents=True); kept.write_bytes(b'someone else')
        with self.assertRaisesRegex(ArchiveError, 'Retained original differs'): self.migrate()
        self.assertEqual(archive.read_bytes(), before); self.assertEqual(kept.read_bytes(), b'someone else')

    def test_resume_after_losing_the_original_before_it_was_retained_stops_for_review(self):
        folder = self.legacy(); archive = next(folder.glob('*.cbz')); real = os.replace
        def interrupted(path, target):
            if Path(path) == archive: raise OSError('simulated interruption')
            return real(path, target)
        with patch.object(os, 'replace', interrupted), self.assertRaises(OSError): self.migrate()
        archive.unlink()
        with self.assertRaisesRegex(ArchiveError, 'absent from its retention path'): self.migrate()
        self.assertTrue((self.root/'.archive-state/export-retirement.json').is_file())

    def test_cover_and_enrichment_markers_survive_conversion(self):
        folder = self.legacy(); cover = png(5); (folder/'cover.png').write_bytes(cover)
        path = folder/'series.json'; metadata = json.loads(path.read_text())
        metadata.update(cover_file='cover.png', cover_size=len(cover), cover_sha256=digest(cover),
                        metadata_source='authenticated_series_page', metadata_imported_at='2026-10-01T00:00:00+00:00')
        path.write_text(json.dumps(metadata))
        self.migrate()
        converted = import_metadata(next(self.root.glob('*/series.json')))
        self.assertEqual(converted['metadata_source'], 'authenticated_series_page')
        self.assertEqual(converted['metadata_imported_at'], '2026-10-01T00:00:00+00:00')
        self.assertEqual(next(self.root.glob('*/cover.*')).read_bytes(), cover)
        self.assertEqual(verify(self.root), [])

    def test_unexpected_entry_in_the_old_series_folder_stops_its_final_move(self):
        folder = self.legacy(); (folder/'extras').mkdir()
        with self.assertRaisesRegex(ArchiveError, 'Unexpected original series entry'): self.migrate()
        self.assertTrue((folder/'extras').is_dir())

    def test_planted_temporary_link_for_a_converted_novel_is_not_written_through(self):
        folder = self.legacy(novel=True); original = next(folder.glob('*.epub')); before = original.read_bytes()
        meta = import_metadata(sidecar_path(original)); series = _series(import_metadata(folder/'series.json'))
        episode = Episode(meta['source_episode_id'], series.id, meta['title'], meta['episode_number'], (), meta['access_state'],
                          meta.get('source_url'), meta.get('published_at'), 'novel', '<p>conversion</p>')
        with closing(PublicationStore(self.root)) as store: converted = store.episode_path(series, episode)
        outside = self.root.parent/'outside'; outside.write_bytes(b'keep')
        converted.with_name(converted.name+'.tmp').symlink_to(outside)
        with self.assertRaisesRegex(ArchiveError, 'Unsafe conversion output'): self.migrate()
        self.assertEqual(outside.read_bytes(), b'keep'); self.assertFalse(converted.exists())
        self.assertEqual(original.read_bytes(), before)

    def test_conflicting_retained_series_metadata_is_never_overwritten(self):
        folder = self.legacy()
        conflict = self.retained/folder.name/'series.json'; conflict.parent.mkdir(parents=True); conflict.write_text('{}')
        with self.assertRaisesRegex(ArchiveError, 'retention conflict'): self.migrate()
        self.assertEqual(conflict.read_text(), '{}'); self.assertTrue((folder/'series.json').is_file())

if __name__ == '__main__':
    unittest.main()
