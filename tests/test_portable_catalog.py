"""Document facts retain the desktop catalog rules; no Android execution is implied."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from comic_archive import library_model, portable_catalog
from comic_archive.models import ArchiveError
from comic_archive.saved_library import Catalog
from comic_archive.workspace import Workspace


def fact(name, *, metadata=None, size=12, modified=1700000000, salt='fixture'):
    value = {'key': hashlib.sha256((salt + '/' + name).encode()).hexdigest(),
             'name': name, 'bytes': size, 'modified': modified}
    if metadata is not None:
        value['metadata'] = metadata
    return value


def manifest(*files):
    return {'v': 1, 'folders': [{'name': 'Series', 'files': list(files)}]}


class PortableCatalogTests(unittest.TestCase):
    def test_existing_portable_library_matches_filesystem_catalog_without_importing_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace('a' * 32, Path(directory))
            folder = workspace.library / 'Series'; folder.mkdir(parents=True)
            entries = {'series.json': {'title': 'Series', 'cover_file': 'cover.png'},
                       '002 - Two.json': {'format': 'cbz', 'title': 'Two', 'episode_number': 2,
                                        'imported_at': '2025-01-02T08:30:00-06:00'},
                       '010 - Ten.json': {'format': 'epub', 'title': 'Ten', 'episode_number': 10,
                                         'imported_at': '2025-01-03T08:30:00-06:00'}}
            for name in ['002 - Two.cbz', '010 - Ten.epub', 'cover.png', *entries]:
                (folder / name).write_bytes(json.dumps(entries[name]).encode() if name in entries else b'CATALOG_FACT')
                os.utime(folder / name, (1700000000, 1700000000))
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
            facts = [fact(p.name, metadata=entries.get(p.name), size=p.stat().st_size) for p in folder.iterdir()]
            document = manifest(*facts)
            for kind in ['all', 'episodes', 'metadata', 'covers']:
                for order in ['series', 'saved_desc', 'saved_asc']:
                    expected = Catalog(b'fixture-key').page(workspace, False, kind=kind, sort=order)
                    actual = portable_catalog.page(document, kind=kind, sort=order)
                    for value in [expected, actual]:
                        for row in value['files']:
                            row.pop('id')
                    self.assertEqual(actual['files'], expected['files'])
                    self.assertEqual(actual['total'], expected['total'])
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()})
            self.assertEqual(sorted(p.name for p in workspace.root.iterdir()), ['Library'])

    def test_exported_archives_need_no_account_or_sidecars_and_do_not_invent_save_times(self):
        value = manifest(fact('One.cbz'), fact('Two.epub', modified=None, size=None), fact('unfinished.cbz', size=0),
                         fact('partial.cbz.tmp'), fact('state.json', metadata=None))
        page = portable_catalog.page(value, kind='episodes', sort='series')
        self.assertEqual(page['total'], 2)
        self.assertEqual([row['title'] for row in page['files']], ['One', 'Two'])
        self.assertTrue(all(row['date_source'] == 'file_modified' for row in page['files']))
        self.assertIsNone(page['files'][1]['saved_at'])
        self.assertEqual(library_model.rows(page, False)[1]['size'], '—')

    def test_search_kind_order_paging_and_offset_clamping_use_the_shared_rules(self):
        files = []
        for index in range(1, 76):
            name = f'{index:03} - Episode'
            files += [fact(name + '.cbz'), fact(name + '.json', metadata={'title': f'Chapter {index}',
                                                                       'episode_number': index})]
        value = manifest(*files)
        first = portable_catalog.page(value, kind='episodes', sort='series')
        last = portable_catalog.page(value, kind='episodes', sort='series', offset=10000)
        self.assertEqual(first['total'], 75); self.assertEqual(len(first['files']), 50)
        self.assertEqual(last['offset'], 50); self.assertEqual(len(last['files']), 25)
        self.assertEqual(last['files'][-1]['title'], 'Chapter 75')
        self.assertEqual(portable_catalog.page(value, query='chapter 75', kind='metadata')['total'], 1)
        with self.assertRaises(ArchiveError):
            portable_catalog.page(value, location='b2')

    def test_old_compressed_format_is_listed_with_metadata_but_never_offered_to_a_cbz_reader(self):
        value = manifest(fact('Old.tar.zst'), fact('Old.json', metadata={'format': 'tar.zst', 'title': 'Old'}),
                         fact('Unpaired.tar.zst'), fact('Wrong.epub'), fact('Wrong.json', metadata={'format': 'cbz'}))
        page = portable_catalog.page(value, kind='episodes')
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['files'][0]['kind'], 'tar.zst')
        self.assertIsNone(library_model.rows(page, False)[0]['open'])

    def test_private_fields_uris_and_nested_metadata_are_refused_before_projection(self):
        for extra in [{'uri': 'content://private/document'}, {'metadata': {'cookie': 'PRIVATE'}},
                      {'metadata': {'title': {'private': 'nested'}}}]:
            row = fact('One.cbz'); row.update(extra)
            with self.subTest(extra=extra), self.assertRaises(ArchiveError):
                portable_catalog.page(manifest(row))

    def test_duplicate_id_filename_or_folder_cannot_alias_a_reader_action(self):
        first = fact('One.cbz')
        for value in [manifest(first, first), manifest(first, dict(fact('Two.cbz'), key=first['key'])),
                      {'v': 1, 'folders': manifest(first)['folders'] * 2}]:
            with self.assertRaises(ArchiveError):
                portable_catalog.page(value)

    def test_unsafe_names_untyped_sizes_and_unversioned_snapshots_fail_closed(self):
        for name in ['../One.cbz', 'private/One.cbz', '.hidden.cbz', 'One\n.cbz', '/One.cbz']:
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                portable_catalog.page(manifest(fact(name)))
        for key, bad in [('bytes', True), ('bytes', -1), ('modified', 2**63), ('key', 'bad')]:
            row = fact('One.cbz'); row[key] = bad
            with self.subTest(field=key), self.assertRaises(ArchiveError):
                portable_catalog.page(manifest(row))
        for version in [True, 0, '1']:
            with self.assertRaises(ArchiveError):
                portable_catalog.page(dict(manifest(fact('One.cbz')), v=version))

    def test_bounded_catalog_does_not_silently_report_a_truncated_folder_as_complete(self):
        from unittest.mock import patch
        with patch.object(portable_catalog, 'MAX_FILES', 1), self.assertRaises(ArchiveError):
            portable_catalog.page(manifest(fact('One.cbz'), fact('Two.cbz')))
        with patch.object(portable_catalog, 'MAX_FOLDERS', 0), self.assertRaises(ArchiveError):
            portable_catalog.page(manifest(fact('One.cbz')))

    def test_projection_never_mutates_provider_facts_or_leaks_sort_bookkeeping(self):
        value = manifest(fact('One.cbz'), fact('One.json', metadata={'title': 'A title', 'imported_at': 'invalid'}))
        before = copy.deepcopy(value)
        page = portable_catalog.page(value, sort='series')
        self.assertEqual(value, before)
        self.assertTrue(all('order' not in row for row in page['files']))
        self.assertNotIn('content://', json.dumps(page))

    def test_empty_folder_is_truthfully_empty_with_no_reader_actions(self):
        value = portable_catalog.page({'v': 1, 'folders': []})
        self.assertEqual(value['files'], [])
        self.assertEqual(value['episode_count'], 0)
        self.assertEqual(value['file_count'], 0)
