"""Actual held export bytes and pinned file owners, without sidecar fabrication."""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from comic_archive.android_local_reader import ReaderHost
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.local_reader import ReaderBook, ReadingError
from comic_archive.selected_reader import COPIES, SelectedReadingSession, selected_path

FIXTURE = Path(__file__).parent / 'fixtures/local-reading-v1'


class SelectedReaderTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name) / 'workspace'
        self.library = self.root / 'Library'
        self.library.mkdir(parents=True)
        self.manifest = json.loads((FIXTURE / 'manifest.json').read_text())
        self.records = {item['kind']: item for item in self.manifest['records']}
        self.host = ReaderHost()
        self.addCleanup(self.host.close_all)

    def copied(self, kind):
        source = FIXTURE / 'payload' / self.records[kind]['archive']
        target = self.library / COPIES / str(uuid.uuid4()) / source.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        return target

    def test_retained_bare_exports_render_and_resume_without_sidecars_or_catalog_imports(self):
        registry = LocalWorkspaceRegistry(self.root)
        before = {name: hashlib.sha256((FIXTURE / 'payload' / name).read_bytes()).hexdigest()
                  for name in self.manifest['files']}
        with patch('httpx.Client.request', side_effect=AssertionError('No network')):
            for kind in ('cbz', 'epub'):
                with self.subTest(kind=kind):
                    path = self.copied(kind)
                    self.assertFalse(path.with_suffix('.json').exists())
                    with self.assertRaises(ReadingError):
                        ReaderBook(self.library, path)  # Registered mode stays strict.
                    raw = path.read_bytes()
                    opened = self.host.open_selected(self.root, path.relative_to(self.library).as_posix(), 'a' * 32)
                    self.assertEqual(opened['kind'], kind)
                    self.assertEqual(opened['content_sha256'], hashlib.sha256(raw).hexdigest())
                    rendered = self.host.page(self.root, 'a' * 32, 0)
                    source = FIXTURE / 'payload' / self.records[kind]['archive']
                    with closing(ReaderBook(FIXTURE / 'payload', source)) as held:
                        expected = held.render(0)
                    if kind == 'cbz':
                        self.assertEqual(base64.b64decode(rendered['data']), expected['data'])
                    else:
                        self.assertEqual(rendered['html'], expected['html'])
                        self.assertEqual({name: base64.b64decode(data) for name, data in rendered['images'].items()},
                                         expected['images'])
                    saved = self.host.save(self.root, 'a' * 32, {'unit': 0, 'offset': 2500}, bookmark=True,
                                           preferences={'fit': 'page', 'text_scale': 1.5})
                    stream = self.host._session.book._stream
                    self.host.close(self.root, 'a' * 32)
                    self.assertTrue(stream.closed)
                    shutil.rmtree(path.parent)
                    later = self.copied(kind)
                    reopened = self.host.open_selected(self.root, later.relative_to(self.library).as_posix(), 'b' * 32)
                    self.assertEqual(reopened['record'], saved['record'])
                    self.host.close_all()
                    self.assertFalse(later.with_suffix('.json').exists())
                    self.assertEqual(later.read_bytes(), raw)
                    self.assertEqual(registry._exports(registry.resolve(LOCAL_OWNER, registry.workspace_id)), {})
        self.assertEqual(before, {name: hashlib.sha256((FIXTURE / 'payload' / name).read_bytes()).hexdigest()
                                  for name in self.manifest['files']})

    def test_only_owned_private_copy_layout_and_current_pin_can_read(self):
        path = self.copied('cbz')
        relative = path.relative_to(self.library).as_posix()
        for value in (str(path), '../' + relative, relative.replace('reader-copies', 'provenance'),
                      relative.replace('/', '//', 1), '.archive-state/reader-copies/not-a-copy/book.cbz'):
            with self.subTest(value=value), self.assertRaises(ReadingError):
                selected_path(self.library, value)
        self.host.open_selected(self.root, relative, 'a' * 32)
        other = self.copied('cbz')
        with self.assertRaises(ReadingError):
            self.host.open_selected(self.root, other.relative_to(self.library).as_posix(), 'a' * 32)
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with self.assertRaises(ReadingError):
            self.host.page(self.root, 'a' * 32, 0)

    def test_unsafe_member_and_resource_limits_refuse_before_member_reads(self):
        for name in ('../page.png', '/page.png', 'page\\name.png'):
            path = self.copied('cbz')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(name, b'not a page')
            with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('No member read')) as read:
                with self.assertRaises(ReadingError):
                    SelectedReadingSession(self.library, path)
                read.assert_not_called()
        path = self.copied('cbz')
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('No member read')) as read:
            with self.assertRaises(ReadingError):
                SelectedReadingSession(self.library, path, max_member_bytes=1)
            read.assert_not_called()

    def test_active_root_retirement_and_hardlinked_or_symlinked_copies_never_open(self):
        path = self.copied('cbz')
        with self.assertRaises(ReadingError):
            self.host.open_selected(self.root, path.relative_to(self.library).as_posix(), 'a' * 32, active=lambda: False)
        self.assertIsNone(self.host._session)
        hard = path.with_suffix('.hard')
        os.link(path, hard)
        with self.assertRaises(ReadingError):
            SelectedReadingSession(self.library, path)
        hard.unlink()
        source = path.with_suffix('.original')
        path.rename(source)
        path.symlink_to(source)
        with self.assertRaises(ReadingError):
            SelectedReadingSession(self.library, path)

    def test_inferred_hashes_cannot_make_active_epub_content_passive(self):
        path = self.copied('epub')
        with zipfile.ZipFile(path) as archive:
            entries = [(member.filename, archive.read(member)) for member in archive.infolist()]
        for attack in (b'<script>PRIVATE</script>', b'<img src="https://private.invalid/image" />'):
            with zipfile.ZipFile(path, 'w') as archive:
                for name, data in entries:
                    if name == 'EPUB/chapter.xhtml':
                        data = data.replace(b'</div></body>', attack + b'</div></body>')
                    archive.writestr(name, data)
            with self.assertRaises(ReadingError) as refusal:
                SelectedReadingSession(self.library, path)
            self.assertNotIn('PRIVATE', str(refusal.exception))
            self.assertNotIn('private.invalid', str(refusal.exception))
