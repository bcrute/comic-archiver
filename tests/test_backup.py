import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive.archive import Store, digest, sidecar_path, verify
from comic_archive.backup import backup_library
from comic_archive.cli import parser
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from tests.test_archive import MemoryAdapter, png


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.source, self.destination = self.root/'Library', self.root/'backup'
        page = self.root/'owned.png'
        page.write_bytes(png(1))
        store = Store(self.source)
        try:
            self.folder = store.archive(Manifest(Series('42', 'Series'), (
                Episode('81', '42', 'Episode', 1, (Page(1, str(page)),), 'local_owned'),)),
                MemoryAdapter(), progress=lambda value: None)
        finally:
            store.close()

    def test_independent_portable_copy_and_idempotent_rerun(self):
        (self.source/'.comic-session').mkdir()
        (self.source/'.comic-session/state.json').write_text('PRIVATE_COOKIE')
        (self.folder/'unexpected-session.json').write_text('PRIVATE_COOKIE')
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertTrue(result['complete'])
        self.assertEqual(result['archive_count'], 1)
        self.assertEqual(result['copied_files'], 3)
        self.assertFalse(result['cache_or_session_copied'])
        self.assertFalse((self.destination/'.archive-cache').exists())
        self.assertFalse((self.destination/'.comic-session').exists())
        self.assertFalse((self.destination/self.folder.name/'unexpected-session.json').exists())
        self.assertEqual(verify(self.destination), [])
        repeat = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(repeat['copied_files'], 0)
        self.assertEqual(repeat['copied_bytes'], 0)
        self.assertEqual(repeat['reused_files'], 3)
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        for path in self.destination.rglob('*'):
            if path.is_file(): self.assertEqual(path.stat().st_mode & 0o777, PRIVATE_FILE)

    def test_interrupted_copy_retains_exact_bytes_and_resumes(self):
        import shutil
        original = shutil.copyfileobj
        calls = 0
        def interrupted(incoming, output, **options):
            nonlocal calls
            calls += 1
            if calls == 2:
                output.write(b'partial')
                raise OSError('PRIVATE_RAW_PATH_OR_CONTENT')
            return original(incoming, output, **options)
        with patch('comic_archive.backup.shutil.copyfileobj', side_effect=interrupted):
            with self.assertRaisesRegex(ArchiveError, 'interrupted') as error:
                backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertNotIn('PRIVATE', str(error.exception))
        state = json.loads((self.destination/'.archive-state/backup.json').read_text())
        self.assertFalse(state['complete'])
        self.assertEqual(state['copied_files'], 1)
        self.assertEqual(list((self.destination/'.archive-state/backup-staging').iterdir()), [])
        result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertTrue(result['complete'])
        self.assertEqual(result['reused_files'], 1)
        self.assertEqual(verify(self.destination), [])

    def test_corrupt_source_does_not_replace_verified_backup(self):
        backup_library(self.source, self.destination, reserve_bytes=0)
        copied = next(self.destination.rglob('*.cbz'))
        before = copied.read_bytes()
        next(self.folder.glob('*.cbz')).write_bytes(b'broken')
        with self.assertRaisesRegex(ArchiveError, 'verification'):
            backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(copied.read_bytes(), before)
        self.assertEqual(verify(self.destination), [])

    def test_corrupt_destination_is_repaired_without_retrieval(self):
        backup_library(self.source, self.destination, reserve_bytes=0)
        next(self.destination.rglob('*.cbz')).write_bytes(b'broken')
        result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(result['copied_files'], 1)
        self.assertEqual(verify(self.destination), [])

    def test_symlinks_unrecognized_directory_and_nested_destinations_fail(self):
        for destination in (self.source, self.source/'nested', self.root,
                            self.source/'..'/'Library'/'nested'):
            with self.assertRaises(ArchiveError): backup_library(self.source, destination, reserve_bytes=0)
        self.destination.mkdir()
        sentinel = self.destination/'unrelated'; sentinel.write_text('keep')
        with self.assertRaises(ArchiveError): backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(sentinel.read_text(), 'keep')
        sentinel.unlink()
        backup_library(self.source, self.destination, reserve_bytes=0)
        copied = next(self.destination.rglob('*.cbz'))
        copied.unlink(); copied.symlink_to(next(self.folder.glob('*.cbz')))
        with self.assertRaises(ArchiveError): backup_library(self.source, self.destination, reserve_bytes=0)

    def test_storage_floor_and_stale_files_never_delete_existing_bytes(self):
        backup_library(self.source, self.destination, reserve_bytes=0)
        before = next(self.destination.rglob('*.cbz')).read_bytes()
        with patch('comic_archive.backup.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(ArchiveError, 'free space'):
                backup_library(self.source, self.destination, reserve_bytes=1)
        extra = self.destination/self.folder.name/'old.cbz'; extra.write_bytes(before)
        with self.assertRaisesRegex(ArchiveError, 'nothing was deleted'):
            backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(extra.read_bytes(), before)

    def test_failed_first_validation_can_retry_and_other_source_cannot_adopt(self):
        metadata = self.folder/'series.json'
        original = metadata.read_bytes()
        metadata.write_bytes(b'{}')
        with self.assertRaises(ArchiveError): backup_library(self.source, self.destination, reserve_bytes=0)
        metadata.write_bytes(original)
        self.assertTrue(backup_library(self.source, self.destination, reserve_bytes=0)['complete'])
        other = self.root/'other'; other.mkdir()
        with self.assertRaisesRegex(ArchiveError, 'another library'):
            backup_library(other, self.destination, reserve_bytes=0)

    def change_series(self, **changes):
        path = self.folder/'series.json'
        path.write_text(json.dumps(json.loads(path.read_text()) | changes))

    def test_cover_is_copied_and_series_metadata_is_published_after_everything_it_describes(self):
        cover = png(7); (self.folder/'cover.png').write_bytes(cover)
        self.change_series(cover_file='cover.png', cover_size=len(cover), cover_sha256=digest(cover))
        result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(result['copied_files'], 4)
        self.assertEqual((self.destination/self.folder.name/'cover.png').read_bytes(), cover)
        files = [record['file'] for record in json.loads((self.destination/'.archive-state/backup.json').read_text())['files']]
        self.assertEqual(files[-1], self.folder.name+'/series.json')
        self.assertLess(files.index(self.folder.name+'/cover.png'), len(files)-1)
        self.assertEqual(verify(self.destination), [])

    def test_series_whose_episode_count_or_metadata_disagrees_with_its_files_is_not_backed_up(self):
        self.change_series(episode_count=2)
        with self.assertRaisesRegex(ArchiveError, 'episode count'): backup_library(self.source, self.destination, reserve_bytes=0)
        self.change_series(episode_count=1)
        (self.folder/'series.json').rename(self.folder/'series.bak')
        with self.assertRaisesRegex(ArchiveError, 'missing portable metadata'): backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(list(self.destination.rglob('*.cbz')), [])

    def test_folders_without_exports_are_skipped_and_a_library_without_any_is_refused(self):
        (self.source/'Plain').mkdir()
        hollow = self.source/'Hollow'; hollow.mkdir()
        (hollow/'series.json').write_bytes((self.folder/'series.json').read_bytes())
        (hollow/'series.json').write_text(json.dumps(json.loads((hollow/'series.json').read_text()) | {'episode_count': 0}))
        result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual((result['series_count'], result['archive_count']), (1, 1))
        self.assertEqual(sorted(p.name for p in self.destination.iterdir() if not p.name.startswith('.')), [self.folder.name])
        empty = self.root/'EmptyLibrary'; (empty/'Plain').mkdir(parents=True)
        with self.assertRaisesRegex(ArchiveError, 'No verified'): backup_library(empty, self.root/'empty-backup', reserve_bytes=0)

    def test_an_episode_kept_only_in_the_cloud_is_backed_up_as_its_sidecar(self):
        archive = next(self.folder.glob('*.cbz')); archive.unlink()
        with self.assertRaisesRegex(ArchiveError, 'episode count'): backup_library(self.source, self.destination, reserve_bytes=0)
        with patch('comic_archive.cloud.offloaded', lambda root, path: {'eviction_requested': True} if path == archive else None):
            result = backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual((result['archive_count'], result['offloaded_episode_count'], result['copied_files']), (0, 1, 2))
        copied = self.destination/self.folder.name
        self.assertEqual(sidecar_path(copied/archive.name).read_bytes(), sidecar_path(archive).read_bytes())
        self.assertFalse((copied/archive.name).exists())

    def test_symlinked_endpoints_and_invalid_reserves_are_refused_before_anything_is_written(self):
        link = self.root/'link'; link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ArchiveError): backup_library(link, self.destination, reserve_bytes=0)
        real = self.root/'real-backup'; real.mkdir(); linked = self.root/'linked-backup'; linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ArchiveError): backup_library(self.source, linked, reserve_bytes=0)
        self.assertEqual(list(real.iterdir()), [])
        for reserve in (-1, True, '1'):
            with self.subTest(reserve=reserve), self.assertRaises(ArchiveError):
                backup_library(self.source, self.destination, reserve_bytes=reserve)
        self.assertFalse(self.destination.exists())

    def test_symlinked_source_series_folder_is_refused(self):
        elsewhere = self.root/'Elsewhere'; shutil.copytree(self.folder, elsewhere)
        (self.source/'Linked').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, 'symlinked source series'): backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(list(self.destination.rglob('*.cbz')), [])

    def test_copy_that_does_not_match_the_source_is_never_published_and_a_retry_succeeds(self):
        original = shutil.copyfileobj; calls = []
        def corrupting(incoming, output, **options):
            calls.append(1)
            if len(calls) == 1:
                output.write(b'x'*len(incoming.read())); return None
            return original(incoming, output, **options)
        with patch('comic_archive.backup.shutil.copyfileobj', side_effect=corrupting):
            with self.assertRaisesRegex(ArchiveError, 'interrupted'): backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual([p for p in self.destination.rglob('*') if p.is_file() and p.relative_to(self.destination).parts[0] != '.archive-state'], [])
        self.assertTrue(backup_library(self.source, self.destination, reserve_bytes=0)['complete'])
        self.assertEqual(verify(self.destination), [])

    def test_destination_folder_replaced_by_a_symlink_mid_copy_writes_nothing_through_it(self):
        outside = self.root/'outside'; outside.mkdir(); original = shutil.copyfileobj; calls = []
        def swapping(incoming, output, **options):
            original(incoming, output, **options)
            if not calls:
                calls.append(1); folder = self.destination/self.folder.name
                folder.rmdir(); folder.symlink_to(outside, target_is_directory=True)
        with patch('comic_archive.backup.shutil.copyfileobj', side_effect=swapping):
            with self.assertRaisesRegex(ArchiveError, 'interrupted'): backup_library(self.source, self.destination, reserve_bytes=0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_progress_reports_the_final_totals_once_per_batch(self):
        seen = []
        backup_library(self.source, self.destination, reserve_bytes=0, progress=seen.append)
        size = sum(p.stat().st_size for p in self.folder.iterdir() if p.is_file())
        self.assertEqual(seen, [{'copied_files': 3, 'copied_bytes': size, 'remaining_files': 0}])

    def test_cli_has_explicit_destination_and_bounded_lock_wait(self):
        options = parser().parse_args(['backup', '/backup/path', '--wait-for-lock', '20', '--reserve-bytes', '300'])
        self.assertEqual(options.destination, Path('/backup/path'))
        self.assertEqual(options.wait_for_lock, 20)
        self.assertEqual(options.reserve_bytes, 300)


if __name__ == '__main__':
    unittest.main()
