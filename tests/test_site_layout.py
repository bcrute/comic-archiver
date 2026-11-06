"""Provider layout migrations rename directories and preserve admission and stored state."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive import app_runtime, site_layout, sites
from comic_archive.errors import LibraryBusy, LibraryMoved
from comic_archive.locking import LibraryLock


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        # A real legacy directory structure, with opaque bytes standing for the index and sign-in.
        for name, filename in (('Library/.archive-state', 'inventory.sqlite3'),
                               ('.comic-session', 'state.json'), ('.comic-web', 'secret.key')):
            path = self.root / name; path.mkdir(parents=True)
            (path / filename).write_bytes(b'preserved')
        (self.root / 'Library/episode.cbz').write_bytes(b'archive')

    def test_each_site_uses_a_sibling_folder_and_legacy_files_are_renamed_in_place(self):
        identities = {name: (self.root / name).stat().st_ino for name in site_layout.ENTRIES}
        shared = self.root / '.comic-shared'; shared.mkdir(); (shared / 'kept').write_bytes(b'shared')
        other = self.root / 'Webtoon/Library'; other.mkdir(parents=True); (other / 'kept.cbz').write_bytes(b'other')
        site_layout.prepare(self.root)
        for name, inode in identities.items():
            self.assertFalse((self.root / name).exists())
            self.assertEqual((self.root / 'Tapas' / name).stat().st_ino, inode)
        self.assertEqual((self.root / 'Tapas/Library/episode.cbz').read_bytes(), b'archive')
        self.assertEqual((self.root / 'Tapas/.comic-session/state.json').read_bytes(), b'preserved')
        self.assertEqual((shared / 'kept').read_bytes(), b'shared')
        self.assertEqual((other / 'kept.cbz').read_bytes(), b'other')
        self.assertTrue(site_layout.known(self.root))
        self.assertFalse((shared / site_layout.PENDING).exists())
        for site in sites.SITES:
            self.assertEqual(sites.folder(self.root, site), self.root / site.name)
        with patch('comic_archive.site_layout.os.rename') as rename:
            site_layout.prepare(self.root)
        rename.assert_not_called()
        for old in (self.root / 'Library', self.root / '.comic-session/worker/.runtime'):
            with self.assertRaises(LibraryMoved), LibraryLock(old):
                pass
        from comic_archive.inventory import Inventory
        from comic_archive.local_library import LocalWorkspaceRegistry
        for open_old in (lambda: Inventory(self.root / 'Library'), lambda: LocalWorkspaceRegistry(self.root)):
            with self.assertRaises(LibraryMoved):
                open_old()
        self.assertFalse((self.root / 'Library').exists())
        self.assertFalse((self.root / '.comic-web').exists())
        for chosen in (self.root, self.root / 'Tapas', self.root / 'Tapas/Library', other, other.parent):
            self.assertEqual(app_runtime.library_root(chosen), self.root)

    def test_an_empty_normalized_root_is_still_the_selected_library(self):
        root = self.root / 'empty'; root.mkdir()
        site_layout.prepare(root)
        self.assertTrue(app_runtime.is_library(root))
        self.assertEqual(app_runtime.library_root(root), root)
        self.assertFalse((root / 'Library').exists())
        self.assertFalse((root / 'Tapas').exists())

    def test_an_active_worker_prevents_any_move(self):
        with patch('comic_archive.background.status', return_value={'running':True}), self.assertRaises(LibraryBusy):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
        self.assertFalse((self.root / 'Tapas').exists())

    def test_a_busy_writer_prevents_any_move(self):
        held, release = threading.Event(), threading.Event()
        def writer():
            with LibraryLock(self.root / 'Library'):
                held.set(); release.wait(5)
        thread = threading.Thread(target=writer); thread.start()
        try:
            self.assertTrue(held.wait(5))
            with self.assertRaises(LibraryBusy):
                site_layout.prepare(self.root)
            self.assertFalse((self.root / 'Tapas').exists())
        finally:
            release.set(); thread.join(5)

    def test_interrupted_move_resumes_and_blocks_new_writers_and_workers(self):
        rename = os.rename
        def interrupted(source, destination):
            if Path(source).name == '.comic-session':
                raise OSError('interrupted')
            rename(source, destination)
        with patch('comic_archive.site_layout.os.rename', side_effect=interrupted), self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Tapas/Library/episode.cbz').is_file())
        self.assertTrue((self.root / '.comic-session/state.json').is_file())
        for root in (self.root / 'Library', self.root / 'Tapas/Library',
                     self.root / '.comic-session/worker/.runtime', self.root / 'Tapas/.comic-session/worker/.runtime'):
            with self.assertRaises(LibraryBusy), LibraryLock(root):
                pass
        site_layout.prepare(self.root)
        self.assertTrue(site_layout.known(self.root))
        self.assertTrue((self.root / 'Tapas/.comic-session/state.json').is_file())
        self.assertFalse((self.root / '.comic-session').exists())
        with LibraryLock(self.root / 'Tapas/Library'):
            pass

    def test_destination_conflicts_and_cross_filesystem_moves_never_copy_or_overwrite(self):
        target = self.root / 'Tapas'; target.mkdir(); kept = target / 'keep'; kept.write_bytes(b'keep')
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertEqual(kept.read_bytes(), b'keep'); kept.unlink()
        with patch('comic_archive.site_layout.os.rename', side_effect=OSError(errno.EXDEV, 'different filesystem')):
            with self.assertRaises(site_layout.LayoutError):
                site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
        self.assertFalse((target / 'Library').exists())

    def test_malformed_journals_and_symlinked_sources_are_refused(self):
        shared = self.root / '.comic-shared'; shared.mkdir()
        journal = shared / site_layout.PENDING; journal.write_text(json.dumps({'schema_version':1,'entries':{}}))
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        journal.unlink()
        path = self.root / '.comic-web'; path.rename(self.root / 'outside'); path.symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
