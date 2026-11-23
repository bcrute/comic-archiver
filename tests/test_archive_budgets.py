"""Small portable archives for checking expanded-byte limits."""
from base64 import b64decode
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import uuid
import zipfile

from comic_archive import archive
from scripts import offline_restore


PAGE = b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
MEMBER = {'file': '0001.png', 'size': len(PAGE), 'sha256': hashlib.sha256(PAGE).hexdigest()}
METADATA = {'format': 'cbz', 'page_count': 1, 'content_hashes': [MEMBER]}


def write_comic(output):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as packed:
        packed.writestr(MEMBER['file'], PAGE)


class ArchiveBudgetTests(unittest.TestCase):
    def test_windows_restore_holds_parents_and_reads_the_checked_native_handle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve()
            series = source / 'series'
            series.mkdir()
            sidecar = series / 'episode.json'
            sidecar.write_bytes(b'{}')
            names = {offline_restore._windows_name(path): path for path in (*sidecar.parents, sidecar)}
            handles = {}
            opened = []
            transferred = []
            reparse = set()

            def create(name, access, share, security, disposition, flags, template):
                path = names[name]
                if path.parent != path:
                    self.assertIn(path.parent, handles.values())
                self.assertEqual((share, disposition, flags), (1, 3, 0x02200000))
                self.assertEqual(access, 0x80 if path.is_dir() else 0x80000000)
                opened.append(path)
                handles[len(opened)] = path
                return len(opened)

            def information(handle, view):
                path = handles[handle]
                info = ctypes.cast(view, ctypes.POINTER(offline_restore._WindowsFileInformation)).contents
                info.attributes = stat.FILE_ATTRIBUTE_DIRECTORY if path.is_dir() else 0
                if path in reparse:
                    info.attributes |= stat.FILE_ATTRIBUTE_REPARSE_POINT
                return 1

            def descriptor(handle, flags):
                path = handles.pop(handle)
                self.assertIn(path.parent, handles.values())
                transferred.append(path)
                return os.open(path, flags)

            api = {'open': create, 'info': information, 'type': lambda _: 1,
                   'close': handles.pop, 'descriptor': descriptor}
            with patch.object(offline_restore, 'WINDOWS', True), patch.object(offline_restore, '_windows_file_api', return_value=api):
                with offline_restore.open_source(source, sidecar) as (stream, size):
                    self.assertEqual(size, 2)
                    self.assertIn(series, handles.values())
                    self.assertEqual(stream.read(), b'{}')
                self.assertEqual(transferred, [sidecar])
                self.assertEqual(handles, {})
                opened.clear()
                reparse.add(series)
                with self.assertRaisesRegex(ValueError, 'Refusing a link'):
                    offline_restore.read_source(source, sidecar, 8)
                self.assertNotIn(sidecar, opened)
                self.assertEqual(handles, {})

    def test_cloud_only_restore_requires_receipt_for_the_current_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            library = base / 'source'
            series = library / 'series'
            series.mkdir(parents=True)
            library_id, series_id, episode_id, target_id = [str(uuid.uuid4()) for _ in range(4)]
            series_bytes = json.dumps({'episode_count': 1, 'export_id': series_id}).encode()
            (series / 'series.json').write_bytes(series_bytes)
            raw = json.dumps(METADATA | {'schema_version': 2, 'export_id': episode_id, 'content_type': 'comic'}).encode()
            (series / 'episode.json').write_bytes(raw)
            cloud = library / '.archive-state' / 'cloud'
            outbox = cloud / 'outbox' / 'b2'
            outbox.mkdir(parents=True)
            (cloud / 'targets.json').write_text(json.dumps({'schema_version': 1, 'library_id': library_id}))
            checksums = ['1' * 64, hashlib.sha256(raw).hexdigest(), hashlib.sha256(series_bytes).hexdigest()]
            archive_path = 'series/episode.cbz'
            record = {
                'state': 'verified', 'eviction_requested': True, 'library_id': library_id,
                'series_id': series_id, 'episode_id': episode_id, 'target_id': target_id,
                'verified_at': '2026-01-01T00:00:00+00:00', 'archive': archive_path, 'remote_root': 'Comic-Archive',
                'version': hashlib.sha256(''.join(checksums[:2]).encode()).hexdigest(),
                'files': [{'name': name, 'path': path, 'sha256': checksum} for name, path, checksum in zip(
                    ('episode.cbz', 'episode.json', 'series.json'),
                    (archive_path, 'series/episode.json', 'series/series.json'), checksums, strict=True)],
            }
            receipt = outbox / (episode_id + '.json')
            receipt.write_text(json.dumps(record))
            restored = base / 'complete'
            self.assertEqual(offline_restore.restore(library, restored)['offloaded_episode_count'], 1)
            self.assertEqual((restored / 'series' / 'episode.json').read_bytes(), raw)
            mismatches = {'library_id': str(uuid.uuid4()), 'episode_id': str(uuid.uuid4()), 'series_id': str(uuid.uuid4()),
                          'target_id': '', 'version': '0' * 64, 'verified_at': '2026-01-01', 'files': record['files'][:2]}
            for field, replacement in mismatches.items():
                with self.subTest(field=field):
                    receipt.write_text(json.dumps(record | {field: replacement}))
                    with self.assertRaisesRegex(ValueError, 'episode count'):
                        offline_restore.restore(library, base / 'incomplete')
                    self.assertFalse((base / 'incomplete').exists())

    def test_standalone_and_app_agree_on_archive_budget_boundaries(self):
        for name in ('MAX_ARCHIVE_MEMBERS', 'MAX_EXPANDED_ARCHIVE_BYTES', 'MAX_MEMBER_BYTES', 'MAX_PACKED_ARCHIVE_BYTES'):
            self.assertEqual(getattr(archive, name), getattr(offline_restore, name))
        for implementation in (archive, offline_restore):
            with self.subTest(implementation=implementation.__name__), \
                    patch.object(implementation, 'MAX_EXPANDED_ARCHIVE_BYTES', 4), \
                    patch.object(implementation, 'MAX_ARCHIVE_MEMBERS', 2):
                self.assertEqual(implementation.expanded_archive_bytes([{'size': 1}, {'size': 2}]), 3)
                self.assertEqual(implementation.expanded_archive_bytes([{'size': 2}, {'size': 2}]), 4)
                with self.assertRaisesRegex(ValueError, 'Expanded archive'):
                    implementation.expanded_archive_bytes([{'size': 2}, {'size': 3}])
                with self.assertRaisesRegex(ValueError, 'member count'):
                    implementation.expanded_archive_bytes([{'size': 1}] * 3)

    def test_cbz_refuses_the_total_before_opening_and_accepts_the_exact_limit(self):
        comics = io.BytesIO()
        write_comic(comics)
        with patch.object(archive, 'MAX_EXPANDED_ARCHIVE_BYTES', len(PAGE) - 1), \
                patch.object(archive.zipfile, 'ZipFile') as opening:
            self.assertTrue(archive.verify_episode_contents(comics, METADATA))
            opening.assert_not_called()
        with patch.object(archive, 'MAX_EXPANDED_ARCHIVE_BYTES', len(PAGE)):
            self.assertEqual(archive.verify_episode_contents(comics, METADATA), [])

    def test_zstd_readers_keep_small_reads_within_one_block_of_output(self):
        from comic_archive.publication import StrictZstdReader
        import zstandard

        data = b'Archive verification fixture\n' * 12000
        packed = zstandard.ZstdCompressor(write_checksum=True).compress(data)
        for reader_class in (StrictZstdReader, offline_restore.FrameReader):
            for budget in (len(data) - 1, len(data), len(data) + 1):
                with self.subTest(reader=reader_class.__name__, budget=budget):
                    reader = reader_class(io.BytesIO(packed), budget)
                    observed = bytearray()

                    def consume():
                        while chunk := reader.read(512):
                            self.assertLessEqual(len(reader.buffer), 128 * 1024)
                            observed.extend(chunk)

                    if budget < len(data):
                        with self.assertRaisesRegex(ValueError, 'exceeds its manifest'):
                            consume()
                    else:
                        consume()
                        self.assertEqual(observed, data)

    def test_real_tar_expansion_is_checked_by_the_app_and_standalone_restore(self):
        from comic_archive.publication import write_compressed_comic

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            library = base / 'source'
            series = library / 'series'
            series.mkdir(parents=True)
            (series / 'series.json').write_text(json.dumps({'episode_count': 1}))
            comic = series / 'episode.tar.zst'
            metadata = METADATA | {'format': 'tar.zst'}
            write_compressed_comic(comic, metadata, lambda _: io.BytesIO(PAGE))
            offline_restore.sidecar(comic).write_text(json.dumps(metadata))
            for limit in (len(PAGE) - 1, len(PAGE), len(PAGE) + 1):
                with self.subTest(limit=limit), patch.object(archive, 'MAX_EXPANDED_ARCHIVE_BYTES', limit), \
                        patch.object(offline_restore, 'MAX_EXPANDED_ARCHIVE_BYTES', limit):
                    output = base / str(limit)
                    with comic.open('rb') as stream:
                        problems = archive.verify_episode_contents(stream, metadata)
                    if limit < len(PAGE):
                        self.assertTrue(problems)
                        with self.assertRaisesRegex(ValueError, 'Expanded archive'):
                            offline_restore.restore(library, output)
                        self.assertFalse(output.exists())
                        self.assertEqual(list(base.glob('.comic-restore-*')), [])
                    else:
                        self.assertEqual(problems, [])
                        self.assertEqual(offline_restore.restore(library, output)['original_image_bytes'], len(PAGE))
                        self.assertEqual((output / 'series' / comic.name).read_bytes(), comic.read_bytes())

    def test_offline_restore_counts_expansion_across_episodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            library = base / 'source'
            series = library / 'series'
            series.mkdir(parents=True)
            (series / 'series.json').write_text(json.dumps({'episode_count': 2}))
            for number in (1, 2):
                write_comic(series / f'{number}.cbz')
                (series / f'{number}.json').write_text(json.dumps(METADATA))
            with patch.object(offline_restore, 'MAX_RESTORE_EXPANDED_BYTES', 2 * len(PAGE)):
                result = offline_restore.restore(library, base / 'exact')
                self.assertEqual(result['archive_count'], 2)
            with patch.object(offline_restore, 'MAX_RESTORE_EXPANDED_BYTES', 2 * len(PAGE) - 1):
                with self.assertRaisesRegex(ValueError, 'budget for one restore'):
                    offline_restore.restore(library, base / 'limited')
                self.assertFalse((base / 'limited').exists())
                self.assertEqual(list(base.glob('.comic-restore-*')), [])

            with patch.object(offline_restore, 'MAX_PACKED_ARCHIVE_BYTES', 1):
                with self.assertRaisesRegex(ValueError, 'disk budget'):
                    offline_restore.restore(library, base / 'packed-limit')
                self.assertFalse((base / 'packed-limit').exists())
                self.assertEqual(list(base.glob('.comic-restore-*')), [])

            with self.assertRaises(FileExistsError):
                offline_restore.restore(library, base / 'exact')
            self.assertTrue((base / 'exact' / 'restore-report.json').is_file())

    def test_reparse_directory_is_refused_before_inspecting_children(self):
        root = Path('source')
        child = root / 'series'
        stats = {
            root: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0),
            child: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT),
        }
        with patch.object(Path, 'lstat', autospec=True, side_effect=stats.__getitem__) as inspect:
            with self.assertRaisesRegex(ValueError, 'reparse point'):
                offline_restore.source_entry(root, child / 'series.json')
            self.assertEqual([call.args[0] for call in inspect.call_args_list], [root, child])
