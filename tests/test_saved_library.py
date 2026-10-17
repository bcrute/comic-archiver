import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from comic_archive.hosted import Workspace
from comic_archive.models import ArchiveError
from comic_archive.saved_library import Catalog, options


class SavedLibraryTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name); (self.root/'Library/series').mkdir(parents=True)
        self.workspace = Workspace('a'*32, self.root)
        self.folder = self.workspace.library/'series'
        self.series = {'schema_version':2, 'export_id':str(uuid.uuid4()), 'title':'Series <img src=x>', 'creators':[]}
        (self.folder/'series.json').write_text(json.dumps(self.series))
        self.catalog = Catalog(b'synthetic-catalog-key')

    def episode(self, number, extension='cbz'):
        archive = self.folder/f'episode-{number:03}.{extension}'
        archive.write_bytes(b'SAVED_ARCHIVE_FIXTURE')
        value = {'schema_version':2, 'export_id':str(uuid.uuid4()), 'title':f'Chapter {number}',
                 'format':extension, 'content_type':'novel' if extension == 'epub' else 'comic',
                 'imported_at':f'2025-01-{number%28+1:02}T08:30:00-06:00'}
        archive.with_suffix('.json').write_text(json.dumps(value))
        return archive, value

    def cloud_copy(self, archive, value, provider='google', *, current=True, ids=None, extra=()):
        state = self.workspace.library/'.archive-state/cloud'; (state/'outbox'/provider).mkdir(parents=True, exist_ok=True)
        library_id, target_id = ids or (str(uuid.uuid4()), str(uuid.uuid4()))
        settings = {'schema_version':1, 'library_id':library_id, 'targets':{
            provider:{'config':os.path.abspath('/PRIVATE_CONFIG'), 'remote':provider, 'enabled':True, 'tested':True,
                      'target_id':target_id if current else str(uuid.uuid4())}}}
        (state/'targets.json').write_text(json.dumps(settings))
        files = [{'name':name, 'path':path.relative_to(self.workspace.library).as_posix(),
                  'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                 for name,path in [('episode.'+value['format'], archive), ('episode.json', archive.with_suffix('.json')),
                                   ('series.json', self.folder/'series.json')]]
        files += [dict(item) for item in extra]
        record = {'state':'verified', 'library_id':library_id, 'target_id':target_id,
                  'archive':files[0]['path'], 'episode_id':value['export_id'], 'series_id':self.series['export_id'],
                  'files':files, 'verified_at':'2026-10-11T12:00:00+00:00', 'eviction_requested':True,
                  'version':hashlib.sha256((files[0]['sha256']+files[1]['sha256']).encode()).hexdigest(),
                  'token':'PRIVATE_TOKEN', 'diagnostic':'PRIVATE_LOG'}
        path = state/'outbox'/provider/(value['export_id']+'.json'); path.write_text(json.dumps(record))
        return path

    def test_entire_catalog_paginates_searches_and_keeps_original_save_time_without_opening_archives(self):
        for n in range(1,76): self.episode(n, 'epub' if n == 75 else 'cbz')
        with patch('zipfile.ZipFile', side_effect=AssertionError('Listing must not inspect comic bytes')):
            page = self.catalog.page(self.workspace, True, limit=50)
        self.assertEqual(page['episode_count'], 75); self.assertEqual(page['file_count'], 151)
        self.assertEqual(len(page['files']), 50); self.assertEqual(page['total'], 151)
        last = self.catalog.page(self.workspace, True, offset=150, limit=50)
        self.assertEqual(len(last['files']), 1)
        found = self.catalog.page(self.workspace, True, query='chapter 75', kind='episodes')
        self.assertEqual(found['total'], 1); self.assertEqual(found['files'][0]['kind'], 'epub')
        self.assertEqual(found['files'][0]['saved_at'], '2025-01-20T14:30:00+00:00')
        self.assertEqual(found['files'][0]['date_source'], 'saved')
        self.assertEqual(found['files'][0]['series'], self.series['title'])
        self.assertNotIn(str(self.root), json.dumps(found))
        earlier = self.catalog.page(self.workspace, True, kind='episodes', sort='saved_asc')
        self.assertEqual([f['saved_at'] for f in earlier['files']], sorted(f['saved_at'] for f in earlier['files']))

    def test_cloud_only_copies_remain_visible_without_provider_calls_or_false_local_download_links(self):
        archive, value = self.episode(1); self.cloud_copy(archive, value); archive.unlink()
        with patch('comic_archive.cloud.Transfer', side_effect=AssertionError('No cloud browsing')):
            page = self.catalog.page(self.workspace, True, location='google')
        self.assertEqual(page['episode_count'], 1); self.assertEqual(page['total'], 3)
        self.assertEqual({r['kind'] for r in page['files']}, {'cbz','episode_metadata','series_metadata'})
        self.assertTrue(all(r['id'] is None and r['saved_at']=='2026-10-11T12:00:00+00:00' for r in page['files']))
        local = self.catalog.page(self.workspace, True, location='device')
        self.assertEqual(local['total'], 2); self.assertTrue(all(r['id'] is None for r in local['files']))
        self.assertEqual(next(g for g in local['locations'] if g['id']=='device')['episodes'], 0)
        self.assertNotIn('PRIVATE', json.dumps(page))

    def test_dropbox_current_and_previous_receipts_are_visible_without_provider_reads(self):
        archive, value = self.episode(1)
        receipt = self.cloud_copy(archive, value, 'dropbox'); archive.unlink()
        with patch('comic_archive.cloud.Transfer', side_effect=AssertionError('No cloud browsing')):
            page = self.catalog.page(self.workspace, True, location='dropbox')
            self.assertEqual(page['total'], 3)
            self.assertEqual(next(g for g in page['locations'] if g['id'] == 'dropbox')['label'], 'Dropbox')
            settings_path = receipt.parent.parent.parent/'targets.json'
            value = json.loads(settings_path.read_text()); value['targets']['dropbox']['target_id'] = str(uuid.uuid4())
            settings_path.write_text(json.dumps(value))
            summary = self.catalog.page(self.workspace, True, summary=True, refresh=True)
            previous = next(g for g in summary['locations'] if g['id'].startswith('dropbox-'))
            self.assertEqual(previous['episodes'], 1); self.assertFalse(previous['connected'])
            self.assertEqual(options('location='+previous['id'])['location'], previous['id'])
        self.assertTrue(all(row['id'] is None for row in page['files']))
        self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_previous_cloud_connection_is_separate_and_unverified_or_changed_receipts_are_not_saved_copies(self):
        archive, value = self.episode(1); receipt = self.cloud_copy(archive, value, current=False)
        summary = self.catalog.page(self.workspace, True, summary=True)
        previous = next(g for g in summary['locations'] if g['id'].startswith('google-'))
        self.assertEqual(previous['episodes'], 1); self.assertFalse(previous['connected'])
        self.assertEqual(next(g for g in summary['locations'] if g['id']=='google')['episodes'], 0)
        record = json.loads(receipt.read_text()); record['state']='pending'; receipt.write_text(json.dumps(record))
        self.assertEqual(self.catalog.page(self.workspace, True, location='google', refresh=True)['total'], 0)
        record['state']='verified'; receipt.write_text(json.dumps(record)); archive.with_suffix('.json').write_text('{}')
        self.assertEqual(sum(g['episodes'] for g in self.catalog.page(self.workspace, True, summary=True, refresh=True)['locations'] if g['id']!='device'), 0)

    def test_private_symlinked_and_unpaired_files_are_excluded_and_missing_date_is_labeled_file_time(self):
        archive, value = self.episode(1); value.pop('imported_at'); archive.with_suffix('.json').write_text(json.dumps(value))
        (self.folder/'unpaired.cbz').write_bytes(b'NO_METADATA')
        (self.folder/'linked.cbz').symlink_to(archive)
        (self.folder/'linked.json').symlink_to(archive.with_suffix('.json'))
        private = self.workspace.library/'.private'; private.mkdir()
        (private/'secret.cbz').write_bytes(b'PRIVATE')
        (self.workspace.library/'other').symlink_to(self.folder)
        os.utime(archive, (1700000000,1700000000))
        page = self.catalog.page(self.workspace, False)
        self.assertEqual(page['total'], 3); self.assertEqual(len(page['locations']), 1)
        row = next(r for r in page['files'] if r['kind']=='cbz')
        self.assertEqual(row['saved_at'], '2023-11-14T22:13:20+00:00'); self.assertEqual(row['date_source'],'file_modified')

    def test_cache_reuses_metadata_and_explicit_refresh_includes_newly_saved_files(self):
        self.episode(1); self.catalog.page(self.workspace, True)
        self.episode(2)
        with patch.object(self.catalog, 'collect', side_effect=AssertionError('Page navigation should reuse metadata')):
            self.assertEqual(self.catalog.page(self.workspace, True, offset=1)['total'],3)
        page = self.catalog.page(self.workspace, True, refresh=True)
        self.assertEqual(page['total'], 5)
        self.assertEqual(self.catalog.page(self.workspace, True, offset=999)['offset'],0)

    def test_query_bounds_prevent_unknown_keys_duplicate_values_and_unbounded_responses(self):
        self.assertEqual(options(''), dict(location='all',query='',kind='all',sort='saved_desc',offset=0,limit=50,summary=False,refresh=False))
        self.assertEqual(options('q=hello+world&limit=200&offset=100&location=device')['query'],'hello world')
        for query in ('owner=PRIVATE','limit=10000','offset=-1','q=a&q=b','location=../../PRIVATE','kind=private',
                      'summary=yes','sort=unknown','q='+'x'*201,'q=%FF','refresh=1&refresh=0'):
            with self.subTest(query=query), self.assertRaises(ArchiveError): options(query)

    def rows(self, **options):
        return self.catalog.page(self.workspace, options.pop('local', False), refresh=True, **options)['files']

    def test_series_title_comes_only_from_a_small_regular_file_inside_the_library(self):
        self.episode(1)
        outside = self.root/'outside.json'; outside.write_text(json.dumps({'title':'LEAKED'}))
        series = self.folder/'series.json'; series.unlink()
        for case in ('symlink', 'oversized', 'folder'):
            if case == 'symlink': series.symlink_to(outside)
            elif case == 'oversized': series.write_text(json.dumps({'title':'LEAKED', 'pad':'x'*(256*1024)}))
            else: series.mkdir()
            with self.subTest(case):
                files = self.rows()
                self.assertEqual({r['series'] for r in files}, {'series'})
                self.assertNotIn('LEAKED', json.dumps(files))
                self.assertEqual({r['kind'] for r in files} - {'series_metadata'}, {'cbz', 'episode_metadata'})
            series.rmdir() if case == 'folder' else series.unlink()

    def test_a_folder_where_a_details_file_should_be_leaves_no_file_handle_open(self):
        # The saved-library view reads these files again on every refresh.
        from comic_archive.saved_library import metadata
        series = self.folder/'series.json'; series.unlink(); series.mkdir()
        real = os.open; opened = []
        def recording(*args, **kwargs):
            opened.append(real(*args, **kwargs)); return opened[-1]
        with patch.object(os, 'open', recording):
            self.assertIsNone(metadata(series, 256*1024))
        for handle in opened:
            with self.assertRaises(OSError): os.fstat(handle)

    def test_series_cover_is_listed_only_under_an_allowed_name_and_folders_without_episodes_are_skipped(self):
        self.episode(1)
        (self.folder/'cover.png').write_bytes(b'COVER')
        (self.folder/'cover.svg').write_bytes(b'NOT A COVER')
        empty = self.workspace.library/'no-episodes'; empty.mkdir()
        (empty/'series.json').write_text(json.dumps({'title':'HIDDEN SERIES', 'cover_file':'cover.png'}))
        (empty/'cover.png').write_bytes(b'COVER')
        for cover_file, listed in (('cover.png', True), ('cover.svg', False), ('../outside.png', False)):
            (self.folder/'series.json').write_text(json.dumps(dict(self.series, cover_file=cover_file)))
            files = self.rows()
            covers = [r for r in files if r['kind'] == 'cover']
            self.assertEqual([(r['filename'], r['size'], r['id'] is not None) for r in covers],
                             [('cover.png', 5, True)] if listed else [], cover_file)
            self.assertNotIn('HIDDEN', json.dumps(files))
            self.assertEqual(len(files), 3 + listed)

    def test_unusable_saved_dates_fall_back_to_file_time_and_are_labelled_as_such(self):
        archive, value = self.episode(1)
        os.utime(archive, (1700000000, 1700000000))
        for saved, expected, source in (('2025-01-05T08:30:00', '2023-11-14T22:13:20+00:00', 'file_modified'),
                ('not a date', '2023-11-14T22:13:20+00:00', 'file_modified'), (12345, '2023-11-14T22:13:20+00:00', 'file_modified'),
                (None, '2023-11-14T22:13:20+00:00', 'file_modified'), ('2025-01-05T08:30:00Z', '2025-01-05T08:30:00+00:00', 'saved')):
            value['imported_at'] = saved; archive.with_suffix('.json').write_text(json.dumps(value))
            row = next(r for r in self.rows(kind='episodes'))
            self.assertEqual((row['saved_at'], row['date_source']), (expected, source), saved)

    def test_unknown_storage_location_is_refused_and_server_libraries_offer_no_cloud_locations(self):
        self.episode(1)
        for local, location in ((True, 'google-0123456789abcdef'), (False, 'google'), (False, 'proton')):
            with self.subTest(local=local, location=location), self.assertRaises(ArchiveError):
                self.catalog.page(self.workspace, local, location=location, refresh=True)
        # Cloud storage that is not set up is not listed.
        self.assertEqual([g['id'] for g in self.catalog.page(self.workspace, True, summary=True, refresh=True)['locations']], ['device'])
        for location in ('b2', 'proton'):
            with self.subTest(location=location), self.assertRaises(ArchiveError):
                self.catalog.page(self.workspace, True, location=location, refresh=True)

    def test_series_sort_groups_by_series_name_then_episode_title_ignoring_case(self):
        self.episode(1)
        other = self.workspace.library/'alpha'; other.mkdir()
        (other/'series.json').write_text(json.dumps({'title':'alpha story'}))
        (other/'e.cbz').write_bytes(b'SAVED_ARCHIVE_FIXTURE')
        (other/'e.json').write_text(json.dumps({'title':'First', 'format':'cbz', 'imported_at':'2020-01-01T00:00:00+00:00'}))
        files = self.rows(kind='episodes', sort='series')
        self.assertEqual([(r['series'], r['title']) for r in files], [('alpha story', 'First'), (self.series['title'], 'Chapter 1')])
        self.assertEqual([r['title'] for r in self.rows(kind='episodes', sort='saved_desc')], ['Chapter 1', 'First'])

    def test_series_sort_lists_episodes_in_their_order_not_by_the_words_of_their_titles(self):
        # Saved as the app names them: the order number, then the title. Episode 10 comes after Episode 2.
        # The saved number counts first; one file here was renamed without it.
        for number, stored in ((10, True), (2, True), (1, True), (3, False)):
            archive = self.folder/((f'{number:03} - ' if number != 10 else '')+f'Episode {number} [{number}].cbz')
            archive.write_bytes(b'SAVED_ARCHIVE_FIXTURE')
            value = {'title':f'Episode {number}', 'format':'cbz', 'imported_at':'2025-01-01T00:00:00+00:00'}
            if stored: value['episode_number'] = number  # Without it, the number the file name starts with.
            archive.with_suffix('.json').write_text(json.dumps(value))
        unnumbered = self.folder/'Bonus.cbz'; unnumbered.write_bytes(b'SAVED_ARCHIVE_FIXTURE')
        unnumbered.with_suffix('.json').write_text(json.dumps({'title':'A bonus', 'format':'cbz', 'episode_number':True}))
        files = self.rows(kind='episodes', sort='series')
        self.assertEqual([r['title'] for r in files], ['Episode 1', 'Episode 2', 'Episode 3', 'Episode 10', 'A bonus'])
        # The series' own files come first, and the order itself is not sent to the page.
        everything = self.rows(sort='series')
        self.assertEqual(everything[0]['kind'], 'series_metadata')
        self.assertEqual([r['title'] for r in everything[1:3]], ['Episode 1', 'Episode 1'])
        self.assertTrue(all('order' not in r for r in everything))

    def test_episodes_without_a_usable_export_id_or_receipt_are_listed_locally_and_never_matched_to_a_cloud_copy(self):
        archive, value = self.episode(1); receipt = self.cloud_copy(archive, value)
        sidecar = archive.with_suffix('.json')
        def cloud_total():
            page = self.catalog.page(self.workspace, True, location='google', refresh=True)
            return page['total'], len(self.catalog.page(self.workspace, True, location='device')['files'])
        self.assertEqual(cloud_total(), (3, 3))
        receipt.write_text('{ not json')
        self.assertEqual(cloud_total(), (0, 3))
        receipt.write_text(json.dumps({'state':'verified'}))
        self.assertEqual(cloud_total(), (0, 3))
        self.cloud_copy(archive, value)
        without_id = {k:v for k,v in value.items() if k != 'export_id'}
        sidecar.write_text(json.dumps(without_id))
        self.assertEqual(cloud_total(), (0, 3))

    def test_cloud_copies_from_one_previous_connection_share_a_location_with_their_cover_and_unknown_receipt_files_are_ignored(self):
        library_id, first_target = str(uuid.uuid4()), str(uuid.uuid4())
        extra = ({'name':'cover.png', 'sha256':'0'*64}, {'name':'notes.txt', 'sha256':'1'*64}, {'name':'../escape.cbz', 'sha256':'2'*64})
        for number, target in ((1, first_target), (2, first_target), (3, str(uuid.uuid4()))):
            archive, value = self.episode(number)
            self.cloud_copy(archive, value, current=False, ids=(library_id, target), extra=extra if number == 1 else ())
        summary = self.catalog.page(self.workspace, True, summary=True, refresh=True)
        previous = {g['label']:g for g in summary['locations'] if g['id'].startswith('google-')}
        self.assertEqual(sorted(previous), ['Google Drive (previous connection 1)', 'Google Drive (previous connection 2)'])
        self.assertEqual([previous[k]['episodes'] for k in sorted(previous)], [2, 1])
        self.assertFalse(any(g['connected'] for g in previous.values()))
        files = self.catalog.page(self.workspace, True, location=previous['Google Drive (previous connection 1)']['id'])['files']
        self.assertEqual(sorted(r['kind'] for r in files),
                         sorted(['cbz', 'episode_metadata', 'series_metadata', 'cover', 'cbz', 'episode_metadata', 'series_metadata']))
        self.assertEqual({r['filename'] for r in files}, {'episode.cbz', 'episode.json', 'series.json', 'cover.png'})
        self.assertTrue(all(r['id'] is None for r in files))
