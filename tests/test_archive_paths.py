from pathlib import Path
import tempfile
import unittest

from comic_archive import archive_paths
from comic_archive.archive_paths import directory
from comic_archive.errors import LibraryMoved
from comic_archive.locking import LibraryLock
from comic_archive.session import store_session


class KeptLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.library = self.work / 'Comic Archiver'
        (self.library / 'Library').mkdir(parents=True)

    def test_a_library_folder_renamed_while_open_is_never_made_again(self):
        with archive_paths.kept(self.library):
            self.assertEqual(directory(self.library / 'Library/Series'), self.library / 'Library/Series')
            self.library.rename(self.work / 'Renamed')
            for path in (self.library / 'Library/Other', self.library / 'Library', self.library):
                with self.subTest(path=path), self.assertRaises(LibraryMoved):
                    directory(path)
            # Nor through the lock every writer takes, which makes its folder.
            with self.assertRaises(LibraryMoved):
                with LibraryLock(self.library / 'Library'):
                    pass
            # Nor by a sign-in saved meanwhile.
            with self.assertRaises(LibraryMoved):
                store_session({'cookies': [], 'origins': []}, self.library / '.comic-session')
            self.assertFalse(self.library.exists())
            self.assertTrue((self.work / 'Renamed/Library/Series').is_dir())
            # Folders outside it are made as before.
            self.assertTrue(directory(self.work / 'Elsewhere/Folder').is_dir())
        # Once closed it is an ordinary folder again, made when asked for.
        self.assertTrue(directory(self.library / 'Library').is_dir())

    def test_it_stays_kept_until_everyone_who_kept_it_has_let_go(self):
        first = archive_paths.keep(self.library)
        second = archive_paths.keep(self.library)
        self.library.rename(self.work / 'Renamed')
        archive_paths.release(first)
        with self.assertRaises(LibraryMoved):
            directory(self.library / 'Library')
        archive_paths.release(second)
        self.assertTrue(directory(self.library / 'Library').is_dir())


if __name__ == '__main__':
    unittest.main()
