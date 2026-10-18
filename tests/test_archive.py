import json
import subprocess
import sys
import shutil
import struct
import tempfile
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
import zlib
import zipfile
from pathlib import Path
from comic_archive.archive import (Store, atomic_json, digest, safe_name, sidecar_path, verify, verify_episode,
                                   verify_series, verify_series_contents, image_extension)
from comic_archive.errors import RunLimit
from comic_archive.adapters.local import LocalAdapter
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series, canonical_url
from comic_archive.transport import ExpiredAsset


def png(color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0]))) + chunk(b"IEND", b""))


class MemoryAdapter:
    def __init__(self):
        self.calls = 0
        self.fail_on = None

    def retrieve(self, page):
        self.calls += 1
        if self.calls == self.fail_on:
            raise ArchiveError("Test interruption")
        return png(page.order)


class ArchiveTests(unittest.TestCase):
    def test_optional_decoding_checks_saved_pages_without_retrieval_or_rewrite(self):
        folder = self.archive()
        next(folder.glob('*.cbz'))
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        calls = self.adapter.calls
        decoder = Mock()
        with patch('comic_archive.image_validation.decoded_image_checker', return_value=decoder):
            self.assertEqual(verify(folder, decode_images=True), [])
        self.assertEqual([call.args[0] for call in decoder.call_args_list], [png(1), png(2)])
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}, before)
        decoder.reset_mock()
        decoder.side_effect = ArchiveError('PRIVATE_IMAGE_BYTES')
        with patch('comic_archive.image_validation.decoded_image_checker', return_value=decoder):
            errors = verify(folder, decode_images=True)
        self.assertEqual(len(errors), 1)
        self.assertIn('Page 1: image pixels could not be decoded', errors[0])
        self.assertNotIn('PRIVATE', str(errors))

    def test_optional_decoding_checks_cover_pixels_after_hash_validation(self):
        from comic_archive.archive import digest, verify_series_contents
        cover = png(0)
        metadata = {'source': 'tapas', 'source_series_id': '42', 'title': 'Series',
                    'cover_file': 'cover.png', 'cover_size': len(cover), 'cover_sha256': digest(cover)}
        decoder = Mock(side_effect=ArchiveError('PRIVATE_IMAGE_BYTES'))
        self.assertEqual(verify_series_contents(metadata, cover), [])
        self.assertEqual(verify_series_contents(metadata, cover, image_decoder=decoder),
                         ['Cover image pixels could not be decoded'])
        decoder.assert_called_once_with(cover)
        decoder.reset_mock()
        metadata['cover_sha256'] = 'wrong'
        self.assertEqual(verify_series_contents(metadata, cover, image_decoder=decoder),
                         ['Cover size or hash does not match metadata'])
        decoder.assert_not_called()

    def test_standalone_restore_excludes_private_folders_even_with_archive_shaped_files(self):
        folder=self.archive()
        private=self.store.root/'.archive-state';private.mkdir(exist_ok=True)
        for path in folder.iterdir():
            if path.is_file():shutil.copy2(path,private/path.name)
        metadata=private/'series.json';value=json.loads(metadata.read_text());value['cookie']='PRIVATE'
        metadata.write_text(json.dumps(value))
        destination=self.root/'independent'
        script=Path(__file__).resolve().parents[1]/'scripts/offline_restore.py'
        result=subprocess.run([sys.executable,str(script),str(self.store.root),'--destination',str(destination)],
                              capture_output=True,text=True,check=True)
        report=json.loads(result.stdout)
        self.assertEqual(report['archive_count'],1)
        self.assertFalse((destination/'.archive-state').exists())
        self.assertFalse(report['application_imported']);self.assertFalse(report['cache_or_session_copied'])
        self.assertNotIn('PRIVATE',''.join(p.read_text() for p in destination.rglob('*.json')))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.series = Series("42", "../../CON: title")
        self.episode = Episode("81", "42", "../First", 1,
                               (Page(2, "https://cdn.tapas.io/b", False), Page(1, "https://cdn.tapas.io/a", False)),
                               "reader_confirmed", "https://tapas.io/episode/81")
        self.manifest = Manifest(self.series, (self.episode,))
        self.adapter = MemoryAdapter()
        self.store = Store(self.root / "Library")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def archive(self, refresh=False):
        return self.store.archive(self.manifest, self.adapter, refresh, progress=lambda _: None)

    def test_portable_order_original_bytes_deterministic_zip(self):
        folder = self.archive()
        cbz = next(folder.glob("*.cbz"))
        with zipfile.ZipFile(cbz) as z:
            self.assertEqual(z.namelist(), ["0001.png", "0002.png"])
            self.assertEqual(z.read("0001.png"), png(1))
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
        old = cbz.read_bytes()
        sidecar = cbz.with_suffix(".json").read_bytes()
        self.archive()
        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(cbz.read_bytes(), old)
        self.assertEqual(cbz.with_suffix(".json").read_bytes(), sidecar)
        self.assertEqual(verify(self.store.root), [])
        self.archive(refresh=True)
        self.assertEqual(cbz.read_bytes(), old)

    @unittest.skipIf(os.name == 'nt', "The first library layout puts the site's own long identifiers in its names; "
                                      'Windows paths have no room for a hundred characters of them twice.')
    def test_long_unicode_titles_and_identifiers_publish_with_original_metadata(self):
        title='😀'*80
        series=Series('s'*100,title)
        episode=Episode('e'*100,series.id,title,999999999,
                        (Page(1,'https://cdn.tapas.io/a',False),),'reader_confirmed')
        folder=self.store.archive(Manifest(series,(episode,)),self.adapter,progress=lambda _:None)
        archive=next(folder.glob('*.cbz'))
        self.assertLessEqual(len(folder.name.encode('utf-8')),255)
        self.assertLessEqual(len((archive.name+'.tmp').encode('utf-8')),255)
        self.assertEqual(json.loads((folder/'series.json').read_text())['title'],title)
        self.assertEqual(json.loads(archive.with_suffix('.json').read_text())['title'],title)
        self.assertEqual(verify(folder),[])

    def test_source_titles_are_bounded_so_exports_stay_verifiable(self):
        # A hostile source could otherwise publish a title longer than the archives' text caps.
        with self.assertRaises(ArchiveError):
            Series('42','x'*501)
        with self.assertRaises(ArchiveError):
            Episode('81','42','x'*501,1,(Page(1,'https://cdn.tapas.io/a',False),),'reader_confirmed')
        Series('42','x'*500)
        Episode('81','42','x'*500,1,(Page(1,'https://cdn.tapas.io/a',False),),'reader_confirmed')

    def test_normalized_source_links_and_storage_are_platform_independent(self):
        series = Series('42', 'Other platform', source='other_platform',
                        source_url='https://example.org/comic/42?session=PRIVATE')
        owned = self.root / 'owned.png'
        owned.write_bytes(png(1))
        episode = Episode('81','42','E',1,(Page(1,str(owned)),),'local_owned',
                          'https://example.org/comic/42/81?token=PRIVATE')
        folder=self.store.archive(Manifest(series,(episode,)),self.adapter,progress=lambda _:None)
        metadata=json.loads((folder/'series.json').read_text())
        self.assertEqual(metadata['source'],'other_platform')
        self.assertEqual(metadata['source_url'],'https://example.org/comic/42')
        self.assertNotIn('PRIVATE',json.dumps(metadata))
        self.assertEqual(verify(folder),[])
        for url in ('http://example.org/a','https://user:PRIVATE@example.org/a','https://example.org:invalid/a'):
            with self.assertRaises(ArchiveError):
                canonical_url(url)

    def test_page_resume_and_no_partial_archive(self):
        self.adapter.fail_on = 2
        with self.assertRaises(ArchiveError):
            self.archive()
        self.assertEqual(list(self.store.root.rglob("*.cbz")), [])
        self.adapter.fail_on = None
        self.archive()
        self.assertEqual(self.adapter.calls, 3)

    def test_discovery_report_is_not_an_orphan_but_episode_metadata_still_is(self):
        folder = self.archive()
        report = self.store.root / "custom-report.json"
        report.write_text(json.dumps({"document_type": "comic_access_discovery", "schema_version": 1,
                                      "access_snapshot_only": True}))
        self.assertEqual(verify(self.store.root), [])
        cbz = next(folder.glob("*.cbz"))
        cbz.unlink()
        # Keep another archive so verification also reaches orphan detection.
        other = Episode("82", "42", "Other", 2, self.episode.pages, "reader_confirmed")
        self.store.archive(Manifest(self.series, (other,)), self.adapter, progress=lambda _: None)
        self.assertTrue(any("missing CBZ" in error for error in verify(self.store.root)))

    def test_corrupt_cache_retrieved_and_corrupt_archive_detected(self):
        folder = self.archive()
        blob = next(p for p in self.store.cache.iterdir() if len(p.name) == 64)
        blob.write_bytes(b"broken")
        self.archive()
        self.assertEqual(self.adapter.calls, 3)
        cbz = next(folder.glob("*.cbz"))
        cbz.write_bytes(b"bad")
        self.assertTrue(verify(self.store.root))
        self.archive()
        self.assertEqual(verify(self.store.root), [])

    def test_signed_url_refresh_reuses_completed_pages(self):
        adapter = self.adapter
        retrieve = adapter.retrieve
        count = [0]
        def get(page):
            if page.order == 2 and count[0] == 0:
                count[0] += 1
                raise ExpiredAsset("expired")
            return retrieve(page)
        adapter.retrieve = get
        adapter.refresh = lambda e: Episode(e.id, e.series_id, e.title, e.order,
            tuple(Page(p.order, p.location + "?secret=renewed", False) for p in e.pages), e.access, e.source_url)
        folder = self.archive()
        self.assertEqual(adapter.calls, 2)
        for p in folder.glob("*.json"):
            self.assertNotIn("secret", p.read_text())
        self.assertNotIn(b"secret", (self.store.cache / "index.sqlite3").read_bytes())

    def test_refreshed_urls_used_for_remaining_pages(self):
        episode = Episode("81", "42", "E", 1,
            tuple(Page(i, f"https://cdn.tapas.io/{i}?old=1", False) for i in range(1, 4)),
            "reader_confirmed")
        self.manifest = Manifest(self.series, (episode,))
        seen = []
        retrieve = self.adapter.retrieve
        def get(page):
            seen.append(page.location)
            if page.order == 2 and "old" in page.location:
                raise ExpiredAsset("expired")
            if page.order == 3:
                self.assertIn("new", page.location)
            return retrieve(page)
        self.adapter.retrieve = get
        self.adapter.refresh = lambda e: Episode(e.id, e.series_id, e.title, e.order,
            tuple(Page(p.order, p.location.replace("old", "new"), False) for p in e.pages), e.access)
        self.archive()
        self.assertEqual(len(seen), 4)

    def test_reject_invalid_access_order_and_ids(self):
        for access in ["locked", "unknown", "purchased"]:
            with self.assertRaises(ArchiveError):
                Episode("81", "42", "E", 1, (Page(1, "a"),), access)
        for pages in [(), (Page(2, "a"),), (Page(1, "a"), Page(1, "b"))]:
            with self.assertRaises(ArchiveError):
                Episode("81", "42", "E", 1, pages, "local_owned")
        with self.assertRaises(ArchiveError):
            Series("../../bad", "S")
        with self.assertRaises(ArchiveError):
            Manifest(self.series, (self.episode, self.episode))

    def test_local_manifest_opaque_names_and_traversal(self):
        (self.root / "opaque").write_bytes(png(3))
        data = {"series": {"id": "1", "title": "S"}, "episodes": [
            {"id": "2", "title": "E", "order": 1, "pages": [{"order": 1, "file": "opaque"}]}]}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(data))
        adapter = LocalAdapter(path)
        folder = self.store.archive(adapter.manifest(), adapter, progress=lambda _: None)
        self.assertEqual(verify(folder), [])
        data["episodes"][0]["pages"][0]["file"] = "../outside"
        path.write_text(json.dumps(data))
        with self.assertRaises(ArchiveError):
            LocalAdapter(path)

    def test_filenames_and_nonimages(self):
        self.assertNotIn("/", safe_name("../x/../y"))
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name("..."), "Untitled")
        self.assertEqual(image_extension(b"\xff\xd8\xffdata\xff\xd9"), ".jpg")
        for data in [b"<html>login</html>", b"encrypted", b"\xff\xd8\xffincomplete"]:
            with self.assertRaises(ArchiveError):
                image_extension(data)

    def test_the_librarys_private_cache_folder_is_hidden_from_the_file_manager(self):
        with patch('comic_archive.archive.hide') as hide:
            store = Store(self.root / "Other")
            store.close()
        hide.assert_called_once_with((self.root / "Other").absolute() / ".archive-cache")

    def test_output_symlink_rejected(self):
        folder = self.store.root / "escape"
        folder.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            Store(folder)

    def test_known_metadata_survives_import_without_those_fields(self):
        folder = self.archive()
        sidecar = next(p for p in folder.glob("*.json") if p.name != "series.json")
        episode = json.loads(sidecar.read_text())
        episode["published_at"] = "2023-07-19"
        sidecar.write_text(json.dumps(episode))
        series_path = folder / "series.json"
        series = json.loads(series_path.read_text())
        series.update(creators=["Known creator"], source_url="https://tapas.io/series/42/info")
        series_path.write_text(json.dumps(series))
        self.archive()
        self.assertEqual(json.loads(sidecar.read_text())["published_at"], "2023-07-19")
        self.assertEqual(json.loads(series_path.read_text())["creators"], ["Known creator"])
        self.assertEqual(self.adapter.calls, 2)

    def test_gif_and_webp_pages_are_recognized_only_when_complete(self):
        def riff(body):
            return b'RIFF' + (len(body) + 4).to_bytes(4, 'little') + b'WEBP' + body
        self.assertEqual(image_extension(b'GIF89a' + b'\0' * 10 + b';'), '.gif')
        self.assertEqual(image_extension(b'GIF87a' + b'\0' * 10 + b';'), '.gif')
        self.assertEqual(image_extension(riff(b'VP8 ' + b'\0' * 20)), '.webp')
        for data in (b'GIF89a' + b'\0' * 10, riff(b'VP8 ' + b'\0' * 20)[:-1],
                     riff(b'VP8 ' + b'\0' * 20) + b'\0', b'RIFF\0\0\0\0WAVE'):
            with self.assertRaises(ArchiveError):
                image_extension(data)

    def test_symlinks_in_archive_output_are_refused_without_touching_their_targets(self):
        folder = self.archive()
        cbz = next(folder.glob('*.cbz'))
        sidecar = cbz.with_suffix('.json')
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'keep')
        link = self.root / 'link.json'
        link.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            atomic_json(link, {'a': 1})
        link.unlink()
        (self.root / 'plain.json.tmp').symlink_to(outside)
        with self.assertRaises(ArchiveError):
            atomic_json(self.root / 'plain.json', {'a': 1})
        # A symlinked temporary file must stop the CBZ before anything is written through it.
        original = cbz.read_bytes()
        cbz.unlink()
        temporary = cbz.with_name(cbz.name + '.tmp')
        temporary.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.archive()
        self.assertFalse(cbz.exists())
        temporary.unlink()
        cbz.write_bytes(original)
        # A symlinked sidecar must not be followed when an existing episode is revisited.
        moved = self.root / 'sidecar-copy.json'
        sidecar.replace(moved)
        sidecar.symlink_to(moved)
        before = moved.read_bytes()
        with self.assertRaises(ArchiveError):
            self.archive()
        self.assertEqual(moved.read_bytes(), before)
        self.assertEqual(cbz.read_bytes(), original)
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_stored_paths_must_stay_inside_the_library_without_symlinks(self):
        self.assertEqual(self.store.stored_path('Series/one.cbz'), self.store.root / 'Series/one.cbz')
        for bad in ('/etc/passwd', '../escape.cbz', 'Series/../../escape.cbz'):
            with self.assertRaises(ArchiveError):
                self.store.stored_path(bad)
        (self.store.root / 'linked').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            self.store.stored_path('linked/one.cbz')

    def test_cache_refuses_symlinked_blobs_and_low_disk_without_writing(self):
        self.store.min_free_bytes = 0
        data = png(5)
        blob = self.store.cache / digest(data)
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'keep')
        blob.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.cache_image(data)
        blob.unlink()
        temporary = self.store.cache / (digest(data) + '.tmp')
        temporary.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.cache_image(data)
        temporary.unlink()
        self.assertEqual(outside.read_bytes(), b'keep')
        with patch('comic_archive.archive.shutil.disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(RunLimit):
                self.store.cache_image(data)
        self.assertFalse(blob.exists())
        self.assertEqual(self.store.cache_image(data), digest(data))
        self.assertEqual(blob.read_bytes(), data)

    def test_series_folder_scan_skips_unreadable_and_foreign_folders_and_adopts_a_matching_one(self):
        root = self.store.root
        (root / 'Broken [1]').mkdir()
        (root / 'Broken [1]/series.json').write_text('{nope')
        (root / 'Other [7]').mkdir()
        (root / 'Other [7]/series.json').write_text(json.dumps({'source': 'tapas', 'source_series_id': '7', 'title': 'Other'}))
        created = self.store.series_folder(self.series)
        self.assertEqual(created, root / (safe_name(self.series.title) + ' [42]'))
        renamed = Series('43', 'New title')
        (root / 'Old title [43]').mkdir()
        (root / 'Old title [43]/series.json').write_text(
            json.dumps({'source': 'tapas', 'source_series_id': '43', 'title': 'Old title'}))
        self.assertEqual(self.store.series_folder(renamed), root / 'Old title [43]')
        self.assertEqual(self.store.series_folder(renamed), root / 'Old title [43]')

    def test_missing_local_source_page_stops_before_any_archive_is_written(self):
        episode = Episode('81', '42', 'E', 1, (Page(1, str(self.root / 'absent.png')),), 'local_owned')
        with self.assertRaisesRegex(ArchiveError, 'missing'):
            self.store.archive(Manifest(self.series, (episode,)), self.adapter, progress=lambda _: None)
        self.assertEqual(self.adapter.calls, 0)
        self.assertEqual(list(self.store.root.rglob('*.cbz')), [])

    def test_series_cover_is_cached_rechecked_and_limited_in_size(self):
        series = Series('42', 'Covered', cover_url='https://cdn.tapas.io/cover.png')
        cover = png(9)
        adapter = Mock()
        adapter.retrieve_cover.return_value = cover
        self.assertEqual(self.store.cover(series, adapter), cover)
        self.assertEqual(self.store.cover(series, adapter), cover)
        self.assertEqual(adapter.retrieve_cover.call_count, 1)
        blob = self.store.cache / digest(cover)
        blob.write_bytes(b'damaged')
        self.assertEqual(self.store.cover(series, adapter), cover)
        self.assertEqual(adapter.retrieve_cover.call_count, 2)
        self.assertEqual(blob.read_bytes(), cover)
        blob.unlink()
        self.assertEqual(self.store.cover(series, adapter), cover)
        self.assertEqual(adapter.retrieve_cover.call_count, 3)
        self.assertEqual(self.store.cover(replace(series, cover_url=None), adapter), None)
        for value in (None, 'text', b'\xff\xd8\xff' + b'0' * (10 * 1024 * 1024) + b'\xff\xd9'):
            adapter.retrieve_cover.return_value = value
            with self.assertRaisesRegex(ArchiveError, '10 MiB'):
                self.store.cover(replace(series, cover_url='https://cdn.tapas.io/other.png'), adapter)

    def test_series_metadata_ignores_unreadable_or_foreign_previous_files_and_counts_readable_episodes(self):
        folder = self.archive()
        (folder / 'garbage.json').write_text('{nope')
        path = folder / 'series.json'
        known = replace(self.series, creators=('Known creator',))
        self.store.write_series_metadata(known)
        self.assertEqual(json.loads(path.read_text())['creators'], ['Known creator'])
        self.assertEqual(json.loads(path.read_text())['episode_count'], 1)
        path.write_text('{broken')
        self.store.write_series_metadata(self.series)
        rewritten = json.loads(path.read_text())
        self.assertEqual(rewritten['creators'], [])
        self.assertEqual(rewritten['source_series_id'], '42')
        path.write_text(json.dumps({'source': 'other_platform', 'source_series_id': '42', 'title': 'Foreign',
                                    'creators': ['Foreign creator']}))
        self.store.write_series_metadata(self.series)
        self.assertEqual(json.loads(path.read_text())['creators'], [])
        self.assertEqual(verify_series(path), [])

    def test_cover_output_refuses_symlinks_and_low_disk_and_then_saves_a_verified_cover(self):
        folder = self.archive()
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'keep')
        path = folder / 'series.json'
        before = path.read_bytes()
        (folder / 'cover.png').symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.write_series_metadata(self.series, cover=png(0))
        (folder / 'cover.png').unlink()
        (folder / 'cover.png.tmp').symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.write_series_metadata(self.series, cover=png(0))
        (folder / 'cover.png.tmp').unlink()
        with patch('comic_archive.archive.shutil.disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(RunLimit):
                self.store.write_series_metadata(self.series, cover=png(0))
        self.assertFalse((folder / 'cover.png').exists())
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertEqual(path.read_bytes(), before)
        self.store.min_free_bytes = 0
        self.store.write_series_metadata(self.series, cover=png(0))
        self.assertEqual((folder / 'cover.png').read_bytes(), png(0))
        self.assertEqual(json.loads(path.read_text())['cover_file'], 'cover.png')
        self.assertEqual(verify(self.store.root), [])

    def test_reader_authorization_is_refreshed_once_per_episode(self):
        retrieve = self.adapter.retrieve
        def get(page):
            if page.order == 2 or 'renewed' not in page.location:
                raise ExpiredAsset('expired')
            return retrieve(page)
        self.adapter.retrieve = get
        self.adapter.refresh = lambda e: Episode(e.id, e.series_id, e.title, e.order,
            tuple(Page(p.order, p.location + '?renewed', False) for p in e.pages), e.access, e.source_url)
        with self.assertRaisesRegex(ArchiveError, 'Refreshed reader assets still failed'):
            self.archive()
        self.assertEqual(list(self.store.root.rglob('*.cbz')), [])

    def test_low_disk_before_publishing_keeps_pages_cached_and_publishes_nothing(self):
        self.store.min_free_bytes = 0
        with patch('comic_archive.archive.shutil.disk_usage', return_value=Mock(free=1000)):
            with self.assertRaises(RunLimit):
                self.archive()
        folder = self.store.series_folder(self.series)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(self.adapter.calls, 2)
        self.archive()
        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(verify(self.store.root), [])

    def novel(self):
        return Episode('90', '42', 'Text', 4, (), 'reader_confirmed', content_type='novel', text_html='<p>Preserved.</p>')

    def test_novel_export_refuses_symlinks_low_disk_and_a_failed_self_check_without_partial_files(self):
        from comic_archive.novels import epub_entries
        self.store.min_free_bytes = 0
        novel = self.novel()
        manifest = Manifest(self.series, (novel,))
        epub = self.store.episode_path(self.series, novel)
        sidecar = sidecar_path(epub)
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'keep')
        sidecar.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.archive(manifest, self.adapter, progress=lambda _: None)
        sidecar.unlink()
        temporary = epub.with_name(epub.name + '.tmp')
        temporary.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.store.archive(manifest, self.adapter, progress=lambda _: None)
        temporary.unlink()
        with patch('comic_archive.archive.shutil.disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(RunLimit):
                self.store.archive(manifest, self.adapter, progress=lambda _: None)
        entries = epub_entries(self.series, novel, '2024-01-01T00:00:00Z')
        entries['EPUB/chapter.xhtml'] = b'<html'
        with patch.object(self.store, 'novel_entries', return_value=entries):
            with self.assertRaisesRegex(ArchiveError, 'failed verification'):
                self.store.archive(manifest, self.adapter, progress=lambda _: None)
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertEqual(sorted(p.name for p in epub.parent.iterdir()), [])
        # A damaged sidecar is rebuilt instead of trusted.
        self.store.archive(manifest, self.adapter, progress=lambda _: None)
        sidecar.write_text('{broken')
        self.store.archive(manifest, self.adapter, progress=lambda _: None)
        self.assertEqual(json.loads(sidecar.read_text())['title'], 'Text')
        self.assertEqual(verify(self.store.root), [])

    def test_tampered_epub_is_reported_by_membership_manifest_and_content(self):
        novel = self.novel()
        folder = self.store.archive(Manifest(self.series, (novel,)), self.adapter, progress=lambda _: None)
        epub = next(folder.glob('*.epub'))
        sidecar = sidecar_path(epub)
        self.assertEqual(verify_episode(epub), [])
        good_epub, good_sidecar = epub.read_bytes(), sidecar.read_text()
        with zipfile.ZipFile(epub) as z:
            members = [(i.filename, z.read(i)) for i in z.infolist()]
        def write(changed):
            with zipfile.ZipFile(epub, 'w', zipfile.ZIP_STORED) as z:
                for name, data in changed:
                    z.writestr(name, data)
        metadata = json.loads(good_sidecar)
        metadata['content_hashes'].reverse()
        sidecar.write_text(json.dumps(metadata))
        self.assertEqual(verify_episode(epub), ['Invalid EPUB file manifest'])
        sidecar.write_text(good_sidecar)
        write(members + [('EPUB/extra.txt', b'x')])
        self.assertEqual(verify_episode(epub), ['EPUB membership differs from metadata'])
        write([(n, b'<html/>' if n == 'EPUB/chapter.xhtml' else d) for n, d in members])
        self.assertEqual(verify_episode(epub), ['EPUB content size or hash mismatch'])
        epub.write_bytes(good_epub)
        self.assertEqual(verify_episode(epub), [])

    def test_series_metadata_and_cover_damage_is_reported(self):
        folder = self.archive()
        self.store.write_series_metadata(self.series, cover=png(0))
        path, cover = folder / 'series.json', folder / 'cover.png'
        good = path.read_text()
        metadata = json.loads(good)
        self.assertEqual(verify_series(path), [])
        def damaged(text):
            path.write_text(text)
            try:
                return verify_series(path)
            finally:
                path.write_text(good)
        self.assertEqual(damaged(json.dumps(metadata | {'title': ' '})), ['Invalid series identity or title'])
        self.assertEqual(damaged(json.dumps(metadata | {'cover_file': 'photo.png'})), ['Invalid cover filename'])
        self.assertEqual(damaged('{broken'), ['Invalid or unreadable series metadata/cover'])
        saved = cover.read_bytes()
        cover.unlink()
        self.assertEqual(verify_series(path), ['Cover is missing, a symlink or oversized'])
        self.assertTrue(any('Cover is missing' in error for error in verify(self.store.root)))
        stray = self.root / 'cover-copy.png'
        stray.write_bytes(saved)
        cover.symlink_to(stray)
        self.assertEqual(verify_series(path), ['Cover is missing, a symlink or oversized'])
        cover.unlink()
        cover.write_bytes(saved)
        self.assertEqual(verify_series(path), [])
        moved = self.root / 'series-copy.json'
        path.replace(moved)
        path.symlink_to(moved)
        self.assertEqual(verify_series(path), ['Series metadata is a symlink'])
        direct = {'source': 'tapas', 'source_series_id': '42', 'title': 'S', 'cover_file': 'photo.png'}
        self.assertEqual(verify_series_contents(direct, saved), ['Invalid cover filename'])
        direct['cover_file'] = 'cover.png'
        self.assertEqual(verify_series_contents(direct, None), ['Cover is missing, a symlink or oversized'])
        self.assertEqual(verify_series_contents(direct, b'0' * (10 * 1024 * 1024 + 1)),
                         ['Cover is missing, a symlink or oversized'])

    def test_damaged_comic_archives_and_stray_metadata_are_reported(self):
        folder = self.archive()
        cbz = next(folder.glob('*.cbz'))
        sidecar = cbz.with_suffix('.json')
        good = json.loads(sidecar.read_text())
        def expect(message, change):
            meta = json.loads(json.dumps(good))
            change(meta)
            sidecar.write_text(json.dumps(meta))
            self.assertEqual(verify_episode(cbz), [message])
        def duplicate(meta):
            meta['content_hashes'][1]['file'] = meta['content_hashes'][0]['file']
        def miscounted(meta):
            meta['page_count'] = 5
        def renamed(meta):
            meta['content_hashes'][0]['file'] = '0009.png'
        def resized(meta):
            meta['content_hashes'][0]['size'] += 1
        expect('Invalid page count or duplicate pages', duplicate)
        expect('Invalid page count or duplicate pages', miscounted)
        expect('Archive page order or membership differs from metadata', renamed)
        expect('Page size mismatch', resized)
        sidecar.write_text(json.dumps(good))
        self.assertEqual(verify_episode(cbz), [])
        epub_named = cbz.with_suffix('.epub')
        epub_named.write_bytes(cbz.read_bytes())
        self.assertEqual(verify_episode(epub_named), ['Archive extension differs from metadata format'])
        epub_named.unlink()
        moved = self.root / 'sidecar-copy.json'
        sidecar.replace(moved)
        sidecar.symlink_to(moved)
        self.assertEqual(verify_episode(cbz), ['Archive or sidecar is a symlink'])
        sidecar.unlink()
        moved.replace(sidecar)
        stray = folder / 'notes.json'
        for text in ('{nope', '[1]'):
            stray.write_text(text)
            self.assertEqual(verify(self.store.root), ['notes.json: missing CBZ or EPUB'])
        report = self.root / 'report.json'
        report.write_text(json.dumps({'document_type': 'comic_access_discovery', 'schema_version': 1,
                                      'access_snapshot_only': True}))
        stray.unlink()
        stray.symlink_to(report)
        self.assertEqual(verify(self.store.root), ['notes.json: missing CBZ or EPUB'])
        stray.unlink()
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertEqual(verify(empty), ['No portable episode archives found'])


if __name__ == "__main__":
    unittest.main()
