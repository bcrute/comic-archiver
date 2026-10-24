"""Consume the first real fleet-produced reading records without regenerating them."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from comic_archive.local_reader import ReadingSession, fresh_record
from comic_archive.working_library import prepare_snapshot

FIXTURE = Path(__file__).parent / 'fixtures/local-reading-v1'


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


class FrozenLocalReadingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FIXTURE / 'payload'
        self.manifest = json.loads((FIXTURE / 'manifest.json').read_text())

    def assert_sessions(self, library):
        expected_positions = {'cbz': {'unit': 1, 'offset': 2500}, 'epub': {'unit': 0, 'offset': 6500}}
        for item in self.manifest['records']:
            with self.subTest(kind=item['kind']):
                saved = json.loads((self.fixture / item['state']).read_text())
                with closing(ReadingSession(library, library / item['archive'])) as reader:
                    self.assertIsNone(reader.warning)
                    self.assertTrue(reader.can_save)
                    self.assertEqual(reader.record, saved)
                    self.assertEqual(reader.book.content, saved['content_sha256'])
                    self.assertEqual(reader.book.kind, item['kind'])
                    self.assertEqual(reader.book.units, saved['units'])
                    self.assertEqual(reader.record['position'], expected_positions[item['kind']])
                    self.assertEqual(reader.record['bookmarks'], [expected_positions[item['kind']]])
                    self.assertEqual(reader.record['preferences'], {'fit': 'page', 'text_scale': 1.25})
                    self.assertEqual(reader.record['revision'], 2 if item['kind'] == 'cbz' else 1)
                    rendered = reader.book.render(reader.record['position']['unit'])
                    if item['kind'] == 'cbz':
                        self.assertEqual(hashlib.sha256(rendered['data']).hexdigest(),
                                         '3522ca3b40e3ec8ec8920b044362e6f39db829b36d3e65340cb48f749f44dd11')
                    else:
                        self.assertIn('Readable', rendered['html'])
                        self.assertIn('punctuation', rendered['html'])
                        self.assertEqual(rendered['images'], {})

    def test_first_producer_bytes_and_every_written_field_are_held(self):
        expected = {name: facts['sha256'] for name, facts in self.manifest['files'].items()}
        self.assertEqual(file_hashes(self.fixture), expected)
        for name, facts in self.manifest['files'].items():
            self.assertEqual((self.fixture / name).stat().st_size, facts['bytes'])
        self.assertEqual({item['kind'] for item in self.manifest['records']}, {'cbz', 'epub'})
        self.assertEqual(self.manifest['unheld'], [])
        fields = self.manifest['held_fields']
        current = fresh_record('a' * 64, 'cbz', 2)
        self.assertEqual(set(current), set(fields['record']))
        self.assertEqual(set(current['position']), set(fields['position']))
        self.assertEqual(set(current['preferences']), set(fields['preferences']))
        for item in self.manifest['records']:
            record = json.loads((self.fixture / item['state']).read_text())
            self.assertEqual(set(record), set(fields['record']))
            self.assertEqual(set(record['position']), set(fields['position']))
            self.assertEqual(set(record['preferences']), set(fields['preferences']))
            self.assertTrue(record['bookmarks'])
            for bookmark in record['bookmarks']:
                self.assertEqual(set(bookmark), set(fields['bookmark']))
            self.assertEqual(Path(item['state']).name, record['content_sha256'] + '.json')

    def test_current_consumer_keeps_all_first_producer_meanings_and_original_bytes(self):
        before = file_hashes(self.fixture)
        self.assert_sessions(self.fixture)
        self.assertEqual(file_hashes(self.fixture), before)

    def test_adoption_keeps_first_progress_bytes_and_resume_after_library_relocation(self):
        before = file_hashes(self.fixture)
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, output = base / 'source', base / 'adopted'
            shutil.copytree(self.fixture, source / 'Library')
            source_before = file_hashes(source)
            prepare_snapshot(source, output)
            for item in self.manifest['records']:
                self.assertEqual((output / 'Library' / item['state']).read_bytes(),
                                 (self.fixture / item['state']).read_bytes())
                self.assertEqual((output / 'Library' / item['archive']).read_bytes(),
                                 (self.fixture / item['archive']).read_bytes())
            moved = base / 'renamed'
            output.rename(moved)
            self.assert_sessions(moved / 'Library')
            self.assertEqual(file_hashes(source), source_before)
        self.assertEqual(file_hashes(self.fixture), before)
