import json
from pathlib import Path
import tempfile
import unittest

from comic_archive.adapters.local import LocalAdapter
from comic_archive.models import ArchiveError


class LocalAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'download'
        self.root.mkdir()
        (self.root / 'one.png').write_bytes(b'page one')
        self.data = {'series': {'id': '1', 'title': 'S'}, 'episodes': [
            {'id': '2', 'title': 'E', 'order': 1, 'pages': [{'order': 1, 'file': 'one.png'}]}]}
        self.path = self.root / 'manifest.json'

    def write(self, value):
        self.path.write_text(value if isinstance(value, str) else json.dumps(value))
        return self.path

    def test_pages_are_read_back_as_the_user_saved_them(self):
        adapter = LocalAdapter(self.write(self.data))
        page = adapter.manifest().episodes[0].pages[0]
        self.assertEqual(adapter.retrieve(page), b'page one')

    def test_damaged_or_incomplete_manifests_are_reported_as_one_plain_error(self):
        broken = (self.data | {'episodes': 'none'}, {'series': {'id': '1'}, 'episodes': []},
                  {'series': ['1'], 'episodes': []}, {'series': self.data['series']}, [], 'not json {')
        for value in broken:
            with self.subTest(value=str(value)[:40]), self.assertRaisesRegex(ArchiveError, 'Invalid or unreadable local manifest'):
                LocalAdapter(self.write(value))
        with self.assertRaisesRegex(ArchiveError, 'Invalid or unreadable local manifest'):
            LocalAdapter(self.root / 'missing.json')

    def test_pages_outside_the_manifest_folder_or_that_are_not_files_are_refused(self):
        outside = self.root.parent / 'outside.png'
        outside.write_bytes(b'secret')
        (self.root / 'link.png').symlink_to(outside)
        (self.root / 'folder').mkdir()
        for name in ('link.png', '../outside.png', 'folder', 'absent.png'):
            data = json.loads(json.dumps(self.data))
            data['episodes'][0]['pages'][0]['file'] = name
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                LocalAdapter(self.write(data))

    def test_page_that_grew_past_100_mib_or_vanished_after_the_manifest_was_read_is_refused(self):
        adapter = LocalAdapter(self.write(self.data))
        page = adapter.manifest().episodes[0].pages[0]
        with (self.root / 'one.png').open('r+b') as stream:
            stream.truncate(100 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ArchiveError, '100 MiB'):
            adapter.retrieve(page)
        (self.root / 'one.png').unlink()
        with self.assertRaisesRegex(ArchiveError, 'Cannot read'):
            adapter.retrieve(page)


if __name__ == '__main__':
    unittest.main()
