from contextlib import closing
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import zipfile

from comic_archive.archive import digest, episode_archives, sidecar_path
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata, write_compressed_comic
from comic_archive.purge import archive_page, evict_episode
from test_archive import MemoryAdapter, png
from test_storage import set_policy


class PurgeTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.work = Path(work.name); self.root = self.work/'Library'
        self.series = Series('42', 'Synthetic', source='local')
        self.episode = Episode('81', '42', 'Chapter', 1, (Page(1, 'https://cdn.test/a', False),), 'local_owned')
        set_policy(self.root, purge_completed_cache=False)

    def publish(self, episode=None):
        store = PublicationStore(self.root)
        try:
            folder = store.archive(Manifest(self.series, (episode or self.episode,)), MemoryAdapter(), progress=lambda _: None)
            return episode_archives(folder)[0]
        finally:
            store.close()

    @property
    def blob(self):
        return self.root/'.archive-cache'/digest(png(1))

    def test_every_page_of_a_compressed_archive_can_be_recovered_by_its_own_name_and_hash(self):
        two_pages = replace(self.episode, pages=(Page(1, 'https://cdn.test/a', False), Page(2, 'https://cdn.test/b', False)))
        cbz = self.publish(episode=two_pages); metadata = import_metadata(sidecar_path(cbz)) | {'format': 'tar.zst'}
        compressed = cbz.with_suffix('.tar.zst')
        with zipfile.ZipFile(cbz) as comic: write_compressed_comic(compressed, metadata, lambda page: comic.open(page['file']))
        cbz.unlink()
        with closing(PublicationStore(self.root)) as store: store.publish_metadata(sidecar_path(compressed), metadata)
        relative = compressed.relative_to(self.root).as_posix()
        for item, expected in zip(metadata['content_hashes'], (png(1), png(2)), strict=True):
            with self.subTest(page=item['file']):
                self.assertEqual(archive_page(self.root, (relative, item['file'], item['size']), item['sha256']), expected)
        first, second = metadata['content_hashes']
        self.assertIsNone(archive_page(self.root, (relative, second['file'], second['size']), first['sha256']))

    def test_recovery_of_an_original_page_requires_the_exact_recorded_bytes(self):
        archive = self.publish(); meta = import_metadata(sidecar_path(archive))
        item = meta['content_hashes'][0]; relative = archive.relative_to(self.root).as_posix()
        record = (relative, item['file'], item['size'])
        self.assertEqual(digest(archive_page(self.root, record, item['sha256'])), item['sha256'])
        self.assertIsNone(archive_page(self.root, record, '0'*64))
        self.assertIsNone(archive_page(self.root, (relative, item['file'], item['size']+1), item['sha256']))
        for unsafe in ('../x.cbz', f'.hidden/{archive.name}', archive.name):
            self.assertIsNone(archive_page(self.root, (unsafe, item['file'], item['size']), item['sha256']))
        link = archive.with_name('link.cbz'); link.symlink_to(archive)
        for missing in (archive.with_name('absent.cbz').relative_to(self.root).as_posix(), link.relative_to(self.root).as_posix()):
            self.assertIsNone(archive_page(self.root, (missing, item['file'], item['size']), item['sha256']))
        sidecar = sidecar_path(archive); moved = self.work/'sidecar.json'; sidecar.rename(moved)
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        moved.rename(sidecar)
        good = archive.read_bytes()
        def rewrite(data):
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(self.work/'rewritten.cbz', 'w') as target:
                for member in source.infolist():
                    target.writestr(member.filename, data if member.filename == item['file'] else source.read(member))
            archive.write_bytes((self.work/'rewritten.cbz').read_bytes())
        rewrite(png(1)+b'extra')
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        rewrite(b'\0'*item['size'])
        self.assertIsNone(archive_page(self.root, record, item['sha256']))
        archive.write_bytes(good)
        self.assertIsNotNone(archive_page(self.root, record, item['sha256']))

    def test_eviction_refuses_an_archive_that_no_longer_verifies_and_keeps_the_cached_original(self):
        archive = self.publish(); meta = import_metadata(sidecar_path(archive))
        archive.write_bytes(b'corrupt')
        store = PublicationStore(self.root)
        try:
            with self.assertRaisesRegex(ArchiveError, 'verification failed'): evict_episode(store, archive, meta)
        finally:
            store.close()
        self.assertTrue(self.blob.is_file())


if __name__ == '__main__':
    unittest.main()
