"""Windows publication requires a stable chain of ordinary directories."""
import ctypes
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from comic_archive import osdep, windows_privacy
from comic_archive.archive import Store
from comic_archive.models import ArchiveError


class ArchiveDirectoryCustodyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name).resolve()
        self.parent = self.base / 'library' / 'series'
        self.parent.mkdir(parents=True)
        self.archive = self.parent / 'comic.cbz'
        self.cache = self.base / 'cache'
        self.cache.mkdir()
        page = b'tiny synthetic page'
        self.digest = hashlib.sha256(page).hexdigest()
        (self.cache / self.digest).write_bytes(page)
        self.store = SimpleNamespace(cache=self.cache)
        self.folders = list(reversed(self.parent.parents)) + [self.parent]

    def windows_api(self, *, reparse=None):
        held = {}
        all_paths = {str(folder): number for number, folder in enumerate(self.folders, 1)}

        def open_directory(path, access, sharing, security, disposition, flags, template):
            self.assertEqual((access, sharing, disposition, flags), (0x81, 1, 3, 0x02200000))
            handle = all_paths[path]
            held[handle] = path
            return handle

        def information(handle, output):
            info = ctypes.cast(output, ctypes.POINTER(windows_privacy.FileInformation)).contents
            info.attributes = 0x410 if held[handle] == str(reparse) else 0x10
            return 1

        api = {'open': Mock(side_effect=open_directory), 'info': Mock(side_effect=information),
               'close': Mock(side_effect=held.pop)}
        return api, held

    def test_handles_cover_the_archive_write_through_final_rename(self):
        api, held = self.windows_api()
        replace = osdep.replace_file

        def finishing(source, target, *, pinned):
            self.assertTrue(pinned)
            self.assertEqual(set(held.values()), {str(folder) for folder in self.folders})
            replace(source, target)

        with patch.object(osdep, 'WINDOWS', True), patch.object(osdep, 'extended_path', side_effect=str), \
                patch.object(windows_privacy, '_api', return_value=api), \
                patch('comic_archive.archive_paths.linked', return_value=False), \
                patch('comic_archive.archive.replace_file', side_effect=finishing):
            Store.publish_comic(self.store, self.archive, [('0001.png', self.digest, 19)], {})
        self.assertFalse(held)
        with zipfile.ZipFile(self.archive) as saved:
            self.assertEqual(saved.namelist(), ['0001.png'])
        self.assertEqual([call.args[0] for call in api['close'].call_args_list],
                         list(range(len(self.folders), 0, -1)))

    def test_native_publication_replaces_existing_archive_in_its_original_directory(self):
        self.archive.write_bytes(b'old archive')
        Store.publish_comic(self.store, self.archive, [('0001.png', self.digest, 19)], {})
        with zipfile.ZipFile(self.archive) as saved:
            self.assertEqual(saved.read('0001.png'), b'tiny synthetic page')
        self.assertEqual(set(self.parent.iterdir()), {self.archive})

    def test_reparse_directory_refusal_happens_before_creating_archive_output(self):
        api, held = self.windows_api(reparse=self.parent.parent)
        with patch.object(osdep, 'WINDOWS', True), patch.object(osdep, 'extended_path', side_effect=str), \
                patch.object(windows_privacy, '_api', return_value=api):
            with self.assertRaisesRegex(ArchiveError, 'regular directory'):
                Store.publish_comic(self.store, self.archive, [], {})
        self.assertFalse(held)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.archive.with_suffix('.cbz.tmp').exists())
        self.assertEqual(api['open'].call_args.args[0], str(self.parent.parent))
