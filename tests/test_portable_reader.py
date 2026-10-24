"""Actual first95 exports selected outside their library; no fabricated sidecars."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from comic_archive import app_actions, sites
from comic_archive.connected.files import Index, SharedRoot
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.local_reader import ReaderBook, ReadingError, ReadingSession, ReadingStore
from comic_archive.portable_reader import PortableReadingSession

FIXTURE = Path(__file__).parent / 'fixtures/local-reading-v1'


class PortableReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.library = self.base / 'workspace/Library'
        self.library.parent.mkdir()
        self.external = self.base / 'Read only exports'
        self.external.mkdir()
        self.addCleanup(self.external.chmod, 0o755)
        self.manifest = json.loads((FIXTURE / 'manifest.json').read_text())
        self.records = {item['kind']: item for item in self.manifest['records']}
        self.registry = LocalWorkspaceRegistry(self.library.parent)
        self.actions = app_actions.Actions(lambda: SimpleNamespace(registry=self.registry, site=sites.DEFAULT))

    def copied(self, kind):
        original = FIXTURE / 'payload' / self.records[kind]['archive']
        path = self.external / original.name
        shutil.copyfile(original, path)
        return path

    def test_held_bare_exports_render_old_records_and_resume_after_a_rename_without_import(self):
        held = {name: hashlib.sha256((FIXTURE / 'payload' / name).read_bytes()).hexdigest()
                for name in self.manifest['files']}
        with (patch('httpx.Client.request', side_effect=AssertionError('No network')),
              patch('comic_archive.local_library.submit_inspection', side_effect=AssertionError('No job'))):
            for kind in ('cbz', 'epub'):
                with self.subTest(kind=kind):
                    item = self.records[kind]
                    original = FIXTURE / 'payload' / item['archive']
                    path = self.copied(kind)
                    path.chmod(0o444)
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    prior = FIXTURE / 'payload' / item['state']
                    progress = self.library / item['state']
                    progress.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(prior, progress)
                    expected = json.loads(prior.read_text())
                    with closing(ReaderBook(FIXTURE / 'payload', original)) as registered:
                        expected_render = registered.render(expected['position']['unit'])
                    self.external.chmod(0o555)
                    with closing(self.actions.read_selected_file(str(path))) as session:
                        self.assertEqual(session.record, expected)
                        self.assertEqual(session.book.content, digest)
                        self.assertEqual(session.book.render(expected['position']['unit']), expected_render)
                        self.assertEqual(session.store.library, self.library)
                        position = {'unit': 0, 'offset': 3200}
                        saved, warning = session.save(position, bookmark=True,
                            preferences={'fit': 'width', 'text_scale': 1.5})
                        self.assertIsNone(warning)
                        stream = session.book._stream
                    self.assertTrue(stream.closed)
                    self.external.chmod(0o755)
                    moved = path.with_name('Renamed export.' + kind)
                    path.rename(moved)
                    with closing(self.actions.read_selected_file(str(moved))) as session:
                        self.assertEqual(session.record, saved)
                    self.assertEqual(hashlib.sha256(moved.read_bytes()).hexdigest(), digest)
                    self.assertFalse(moved.with_suffix('.json').exists())
                    self.assertEqual(list(self.external.iterdir()), [moved])
                    moved.unlink()
                    self.assertEqual(self.registry._exports(
                        self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id)), {})
        self.assertEqual(held, {name: hashlib.sha256((FIXTURE / 'payload' / name).read_bytes()).hexdigest()
                               for name in self.manifest['files']})

    def test_selected_reading_keeps_registered_completion_and_connected_share_authority_strict(self):
        shutil.copytree(FIXTURE / 'payload', self.library, dirs_exist_ok=True)
        path = self.copied('cbz')
        orphan = self.library / 'orphan.cbz'
        shutil.copyfile(path, orphan)
        with self.assertRaises(ReadingError):
            ReadingSession(self.library, orphan)
        with closing(SharedRoot(self.library)) as root, closing(Index(self.base / 'index.sqlite', root)) as index:
            book = index.register_completed(self.records['cbz']['archive'], 'Reader fixture')
            snapshot = index.snapshot()
            with closing(self.actions.read_selected_file(str(path))) as session:
                session.book.render(0)
                _, warning = session.save({'unit': 0, 'offset': 4100}, bookmark=True)
                self.assertIsNone(warning)
            self.assertEqual(index.snapshot(), snapshot)
            self.assertEqual(index.register_completed(self.records['cbz']['archive'], 'Reader fixture'), book)
            self.assertFalse(orphan.with_suffix('.json').exists())
            self.assertFalse(path.with_suffix('.json').exists())

    def test_replaced_selected_original_refuses_further_render_and_preserves_saved_progress(self):
        path = self.copied('cbz')
        with closing(self.actions.read_selected_file(str(path))) as session:
            session.save({'unit': 0, 'offset': 100})
            state = session.store.folder / (session.book.content + '.json')
            before = state.read_bytes()
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            with self.assertRaises(ReadingError):
                session.book.render(0)
            with self.assertRaises(ReadingError):
                session.book.check()
            self.assertEqual(state.read_bytes(), before)
        self.assertTrue(session.book._stream.closed)

    def test_bad_paths_symlinks_and_unsupported_members_refuse_without_source_path_disclosure(self):
        path = self.copied('cbz')
        link = self.external / 'link.cbz'
        link.symlink_to(path)
        for chosen in ('relative.cbz', str(link), str(path.parent), str(path.with_suffix('.txt'))):
            with self.subTest(chosen=chosen), self.assertRaises(app_actions.Refused) as refused:
                self.actions.read_selected_file(chosen)
            self.assertNotIn(str(self.base), str(refused.exception))
        bad = self.external / 'unsafe.cbz'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('../PRIVATE.png', b'not an image')
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('No decompression')) as read:
            with self.assertRaises(ReadingError):
                PortableReadingSession(self.library, bad)
            read.assert_not_called()
            with self.assertRaises(ReadingError):
                PortableReadingSession(self.library, path, max_member_bytes=1)
            read.assert_not_called()

    def test_active_epub_cannot_become_trusted_through_inferred_member_hashes(self):
        path = self.copied('epub')
        with zipfile.ZipFile(path) as archive:
            entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
        for attack in (b'<script>PRIVATE</script>', b'<img src="https://private.invalid/image" />',
                       b'<!DOCTYPE div [<!ENTITY x "PRIVATE">]>'):
            with self.subTest(attack=attack):
                with zipfile.ZipFile(path, 'w') as archive:
                    for name, data in entries:
                        if name == 'EPUB/chapter.xhtml':
                            data = data.replace(b'</div></body>', attack + b'</div></body>')
                        archive.writestr(name, data)
                with self.assertRaises(app_actions.Refused) as refused:
                    self.actions.read_selected_file(str(path))
                self.assertNotIn('PRIVATE', str(refused.exception))
                self.assertNotIn('private.invalid', str(refused.exception))
        self.assertEqual(list(ReadingStore(self.library).folder.glob('*.json')), [])
