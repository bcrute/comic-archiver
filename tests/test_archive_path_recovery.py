from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from comic_archive.archive import Store, verify
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from test_archive import MemoryAdapter


class ArchivePathRecoveryTests(unittest.TestCase):
    def test_restored_series_recovers_comics_and_novels_after_alias_and_title_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'; store = Store(root, min_free_bytes=0)
            series = Series('42', 'Original series', platform_id='42')
            comic = Episode('51', '42', 'Original comic', 1,
                            (Page(1, 'https://us-a.tapas.io/pc/a', False),), 'reader_confirmed')
            novel = Episode('52', '42', 'Original novel', 2, (), 'reader_confirmed',
                            content_type='novel', text_html='<p>A complete fixture chapter.</p>')
            try:
                folder = store.archive(Manifest(series, (comic, novel)), MemoryAdapter(), progress=lambda _: None)
                comic_path, novel_path = store.episode_path(series, comic), store.episode_path(series, novel)
                before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
            finally:
                store.close()
            (root / '.archive-cache/index.sqlite3').unlink()
            renamed = replace(series, id='new-alias', title='Renamed series')
            comic = replace(comic, series_id='new-alias', title='Renamed comic')
            novel = replace(novel, series_id='new-alias', title='Renamed novel')
            store = Store(root, min_free_bytes=0)
            try:
                self.assertEqual(store.episode_path(renamed, comic), comic_path)
                self.assertEqual(store.episode_path(renamed, novel), novel_path)
                next_comic = replace(comic, id='53', order=3, title='New episode')
                store.archive(Manifest(renamed, (next_comic,)), MemoryAdapter(), progress=lambda _: None)
                for name, data in before.items():
                    if name != 'series.json': self.assertEqual((folder / name).read_bytes(), data)
                self.assertEqual([p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.')], [folder])
                self.assertEqual(len(list(folder.glob('*.cbz'))), 2)
                self.assertEqual(len(list(folder.glob('*.epub'))), 1)
                self.assertEqual(verify(root), [])
                with self.assertRaises(ArchiveError):
                    store.episode_path(renamed, replace(novel, content_type='comic', text_html=None,
                                                     pages=(Page(1, 'https://us-a.tapas.io/pc/a', False),)))
            finally:
                store.close()

    def test_recovery_skips_invalid_private_ids_foreign_series_and_malformed_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Library'; store = Store(root, min_free_bytes=0)
            series = Series('42', 'Series', platform_id='42')
            episode = Episode('51', '42', 'Episode', 1,
                              (Page(1, 'https://us-a.tapas.io/pc/a', False),), 'reader_confirmed')
            try:
                folder = store.series_folder(series)
                (folder / 'invalid.json').write_text(json.dumps({'series_id': '42',
                    'source_episode_id': 'https://us-a.tapas.io/pc/a?secret=PRIVATE'}))
                (folder / 'foreign.json').write_text(json.dumps({'series_id': '99', 'source_episode_id': '52'}))
                (folder / 'malformed.json').write_text('[]')
                store.episode_path(series, episode)
                ids = [row[0] for row in store.db.execute('SELECT episode_id FROM episode_paths')]
                self.assertEqual(ids, ['51'])
                self.assertNotIn(b'PRIVATE', (store.cache / 'index.sqlite3').read_bytes())
            finally:
                store.close()
