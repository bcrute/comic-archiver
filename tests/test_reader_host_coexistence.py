"""Real local reading and native catalogue custody over the held95 producer files.

An Index is local selected-file custody, not remote account/peer/read authority.
These checks never claim pairing, DTLS, a remote lease or network reading.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from comic_archive.connected.files import Index, SharedRoot, SourceError
from comic_archive.local_reader import ReadingSession


FIXTURE = Path(__file__).parent / 'fixtures/local-reading-v1'


class ReaderHostCoexistenceTests(unittest.TestCase):
    def test_first_reader_records_and_native_book_identity_survive_local_reading(self):
        manifest = json.loads((FIXTURE / 'manifest.json').read_text())
        original = FIXTURE / 'payload'
        held = {name: hashlib.sha256((original / name).read_bytes()).hexdigest()
                for name in manifest['files']}
        self.assertEqual(held, {name: value['sha256'] for name, value in manifest['files'].items()})
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            library = base / 'Library'
            shutil.copytree(original, library)
            with closing(SharedRoot(library)) as root, closing(Index(base / 'index.sqlite', root)) as index:
                comic = next(item for item in manifest['records'] if item['kind'] == 'cbz')
                book = index.register_completed(comic['archive'], 'Reader fixture')
                before = index.snapshot()
                for item in manifest['records']:
                    expected = json.loads((original / item['state']).read_text())
                    with closing(ReadingSession(library, library / item['archive'])) as session:
                        self.assertIsNone(session.warning)
                        self.assertEqual(session.record, expected)
                        self.assertEqual(session.book.kind, item['kind'])
                        rendered = session.book.render(session.record['position']['unit'])
                        self.assertEqual(rendered['kind'], item['kind'])
                        changed, warning = session.save({'unit': 0, 'offset': 3300}, bookmark=True)
                        self.assertIsNone(warning)
                        self.assertEqual(changed['position'], {'unit': 0, 'offset': 3300})
                    with closing(ReadingSession(library, library / item['archive'])) as reopened:
                        self.assertEqual(reopened.record['position'], {'unit': 0, 'offset': 3300})
                    if item['kind'] == 'epub':
                        with self.assertRaises(SourceError) as refused:
                            index.register_completed(item['archive'], 'Reader fixture')
                        self.assertEqual(refused.exception.code, 'unsupported_format')
                self.assertEqual(index.snapshot(), before)
                self.assertEqual(index.register_completed(comic['archive'], 'Reader fixture'), book)
                self.assertEqual(index.snapshot(), before)
                with closing(index.open_original(book['book_id'], book['content_version'])) as source:
                    self.assertEqual(source.sha256(), book['sha256'])
                for relative, facts in manifest['files'].items():
                    if relative.startswith('.archive-state/reading/'):
                        continue
                    self.assertEqual(hashlib.sha256((library / relative).read_bytes()).hexdigest(),
                                     facts['sha256'])
        self.assertEqual({name: hashlib.sha256((original / name).read_bytes()).hexdigest()
                          for name in manifest['files']}, held)
