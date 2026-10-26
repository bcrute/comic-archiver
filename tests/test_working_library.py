"""Working-copy adoption, with actual writers/readers and disposable source data.

These do not prove a system document capture, picker, controller switch or phone
journey. Those callers remain part of the same unsubmitted Android feature block.
"""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from comic_archive import cloud, cloud_history, sites
from comic_archive.archive import Store, sidecar_path, verify
from comic_archive.errors import AuthenticationError
from comic_archive.inventory import Inventory
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, import_metadata, write_compressed_comic
from comic_archive.saved_library import Catalog
from comic_archive.working_library import (LibraryImportError, LOCATION_MARKER, RECEIPT, prepare_snapshot,
                                          prepared_snapshot)
from comic_archive.workspace import Workspace
from test_archive import MemoryAdapter, png


class WorkingLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'
        self.output = self.base / 'prepared'

    def publish(self, *, site=sites.DEFAULT, legacy=False, novel=False, bind=True, key='81'):
        root = sites.folder(self.source, site) / 'Library'
        series = Series('a-slug', 'Saved series', source=site.key, platform_id='42')
        episode = Episode(key, series.id, 'Saved chapter', int(key)-80,
                          () if novel else (Page(1, 'https://cdn.test/a', False),), site.free,
                          content_type='novel' if novel else 'comic', text_html='<p>Saved text.</p>' if novel else None)
        with closing((Store if legacy else PublicationStore)(root)) as store:
            folder = store.archive(Manifest(series, (episode,)), MemoryAdapter(), progress=lambda _: None)
            archive = store.episode_path(series, episode, folder)
        with Inventory(root) as inventory:
            if bind:
                inventory.bind_account('11')
            inventory.observe('42', series.title, [{'id': key, 'title': episode.title, 'order': episode.order,
                                                    'access_state': site.free}], 'subscribed')
            inventory.mark(key, 'archived', path=archive)
        return root, archive, series, episode

    @staticmethod
    def hashes(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def cloud_receipt(self, root, archive, *, remove=True):
        library_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        cloud._write(root / '.archive-state/cloud/targets.json', {
            'schema_version': 1, 'library_id': library_id, 'targets': {'b2': {
                'remote': 'private_remote', 'config': '/PRIVATE/credentials.conf',
                'enabled': True, 'tested': True, 'target_id': target_id}}})
        record = cloud._bundle(root, archive) | {
            'library_id': library_id, 'target_id': target_id, 'state': 'verified',
            'archive': archive.relative_to(root).as_posix(), 'remote_root': cloud.CLOUD_ROOT,
            'verified_at': '2026-10-22T12:00:00+00:00', 'eviction_requested': remove,
            'connection': {'config': '/PRIVATE/credentials.conf', 'key': 'PRIVATE_SECRET'}}
        cloud_history.retain(root, 'b2', record)
        if remove:
            archive.unlink()
        return record

    def assert_refused(self, code=None):
        with self.assertRaises(LibraryImportError) as caught:
            prepare_snapshot(self.source, self.output)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertFalse((self.output / RECEIPT).exists())

    def test_reading_progress_and_future_records_follow_verified_copies_without_locks_or_partial_writes(self):
        root, archive, _, _ = self.publish()
        fixture = Path(__file__).parent / 'fixtures/local-reading-v1/payload/.archive-state/reading'
        original = next(fixture.glob('*.json'))
        reading = root / '.archive-state/reading'
        reading.mkdir()
        record_name = original.name
        raw = original.read_bytes()
        (reading / record_name).write_bytes(raw)
        (reading / 'writer.lock').write_bytes(b'LOCK')
        future = b'{"v":99,"future":"retained"}'
        (reading / ('f' * 64 + '.json')).write_bytes(future)
        (reading / (record_name + '.tmp')).write_bytes(b'PARTIAL')
        before = self.hashes(self.source)
        prepare_snapshot(self.source, self.output)
        copied = self.output / 'Library/.archive-state/reading'
        self.assertEqual((copied / record_name).read_bytes(), raw)
        self.assertEqual((copied / ('f' * 64 + '.json')).read_bytes(), future)
        self.assertFalse((copied / 'writer.lock').exists())
        self.assertFalse((copied / (record_name + '.tmp')).exists())
        self.assertEqual(self.hashes(self.source), before)

    def test_oversized_or_symlinked_reading_state_is_refused_without_touching_the_source(self):
        root, _, _, _ = self.publish()
        reading = root / '.archive-state/reading'
        reading.mkdir()
        path = reading / ('a' * 64 + '.json')
        path.write_bytes(b'x' * (64 * 1024 + 1))
        before = self.hashes(self.source)
        self.assert_refused()
        self.assertEqual(self.hashes(self.source), before)
        path.unlink()
        outside = self.base / 'private-record'
        outside.write_text('PRIVATE retained')
        path.symlink_to(outside)
        self.assert_refused()
        self.assertEqual(outside.read_text(), 'PRIVATE retained')

    def test_neutral_comic_keeps_bytes_ids_binding_and_publication_paths_without_source_state(self):
        root, archive, series, episode = self.publish()
        (self.source / 'session').mkdir()
        (self.source / 'session/cookies.json').write_text('PRIVATE_SECRET')
        (root / '.archive-state/tasks.json').write_text('PRIVATE_QUEUE')
        (root / '.archive-state/shared.json').write_text('PRIVATE_LINK')
        with Inventory(root) as inventory:
            before_binding = dict(inventory.db.execute('SELECT key,value FROM settings'))
            inventory.db.execute("INSERT INTO settings VALUES ('speed','slow')")
            inventory.db.execute("INSERT INTO runs VALUES ('PRIVATE_RUN','2026-10-21T00:00:00+00:00',NULL,'running',NULL)")
            inventory.db.commit()
        before = self.hashes(self.source)
        source_public = sidecar_path(archive).read_bytes()
        result = prepare_snapshot(self.source, self.output)
        self.assertEqual(self.hashes(self.source), before)
        self.assertEqual((result.sites[0].episodes, result.sites[0].account_bound), (1, True))
        target = self.output / 'Library'
        saved = next(target.glob('*/*.cbz'))
        self.assertEqual(saved.read_bytes(), archive.read_bytes())
        self.assertEqual(sidecar_path(saved).read_bytes(), source_public)
        self.assertEqual(import_metadata(sidecar_path(saved)), import_metadata(sidecar_path(archive)))
        self.assertEqual(verify(target), [])
        with Inventory(target) as inventory:
            self.assertEqual(dict(inventory.db.execute('SELECT key,value FROM settings')), before_binding)
            self.assertEqual(inventory.account_binding_state('11'), 'matches')
            with self.assertRaises(AuthenticationError):
                inventory.bind_account('different-account')
            self.assertEqual(inventory.status()['episodes_by_status'], {'archived': 1})
            self.assertEqual(inventory.pending(), [])
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        with closing(PublicationStore(target)) as store:
            self.assertEqual(store.series_folder(series), saved.parent)
            self.assertEqual(store.episode_path(series, episode), saved)
        self.assertFalse((self.output / 'session').exists())
        self.assertFalse((target / '.archive-state/tasks.json').exists())
        self.assertFalse((target / '.archive-state/shared.json').exists())

    def test_legacy_metadata_becomes_neutral_without_changing_archive_bytes_or_inventing_ids(self):
        root, archive, _, _ = self.publish(legacy=True)
        source_metadata = import_metadata(sidecar_path(archive))
        prepare_snapshot(self.source, self.output)
        target = self.output / 'Library'
        saved = next(target.glob('*/*.cbz'))
        self.assertEqual(saved.read_bytes(), archive.read_bytes())
        public = json.loads(sidecar_path(saved).read_text())
        self.assertEqual(public['schema_version'], 2)
        self.assertNotIn('source_episode_id', public)
        private = import_metadata(sidecar_path(saved))
        self.assertEqual(private['source_episode_id'], source_metadata['source_episode_id'])
        self.assertEqual(private['series_id'], source_metadata['series_id'])
        self.assertEqual(verify(target), [])
        self.assertEqual(verify(root), [])

    def test_actual_epub_is_verified_and_adopted(self):
        _, archive, series, episode = self.publish(novel=True)
        prepare_snapshot(self.source, self.output)
        target = self.output / 'Library'
        saved = next(target.glob('*/*.epub'))
        self.assertEqual(saved.read_bytes(), archive.read_bytes())
        self.assertEqual(verify(target), [])
        with closing(PublicationStore(target)) as store:
            self.assertEqual(store.episode_path(series, episode), saved)

    def test_cover_and_series_provenance_are_preserved_in_the_new_private_copy(self):
        root, archive, series, _ = self.publish()
        with closing(PublicationStore(root)) as store:
            store.write_series_metadata(series, cover=png(2))
        metadata = import_metadata(archive.parent / 'series.json')
        prepare_snapshot(self.source, self.output)
        saved = next((self.output / 'Library').glob('*/*.cbz'))
        self.assertEqual((saved.parent / 'cover.png').read_bytes(), png(2))
        self.assertEqual(import_metadata(saved.parent / 'series.json'), metadata)
        self.assertEqual(verify(self.output / 'Library'), [])

    def test_legacy_compressed_comic_keeps_its_format_and_publisher_identity(self):
        root, archive, series, episode = self.publish()
        metadata = import_metadata(sidecar_path(archive)) | {'format': 'tar.zst'}
        with zipfile.ZipFile(archive) as comic:
            pages = {name: comic.read(name) for name in comic.namelist()}
        compressed = archive.with_suffix('.tar.zst')
        write_compressed_comic(compressed, metadata, lambda page: io.BytesIO(pages[page['file']]))
        with closing(PublicationStore(root)) as store:
            store.publish_metadata(sidecar_path(compressed), metadata)
        archive.unlink()
        with Inventory(root) as inventory:
            inventory.mark(episode.id, 'archived', path=compressed)
        prepare_snapshot(self.source, self.output)
        target = self.output / 'Library'
        saved = next(target.glob('*/*.tar.zst'))
        self.assertEqual(saved.read_bytes(), compressed.read_bytes())
        self.assertEqual(list(target.glob('*/*.cbz')), [])
        self.assertEqual(verify(target), [])
        with closing(PublicationStore(target)) as store:
            self.assertEqual(store.episode_path(series, episode), saved)

    def test_providers_with_same_numeric_episode_ids_keep_independent_libraries_and_bindings(self):
        for site in sites.SITES:
            self.publish(site=site)
        result = prepare_snapshot(self.source, self.output)
        self.assertEqual([r.key for r in result.sites], [s.key for s in sites.SITES])
        self.assertEqual(sum(r.episodes for r in result.sites), 3)
        for site in sites.SITES:
            target = sites.folder(self.output, site) / 'Library'
            self.assertEqual(verify(target), [])
            with Inventory(target) as inventory:
                self.assertEqual(inventory.account_binding_state('11'), 'matches')
                self.assertEqual(inventory.status()['episodes_by_status'], {'archived': 1})

    def test_verified_cloud_only_history_is_visible_disconnected_without_credentials_or_retry_state(self):
        root, archive, series, episode = self.publish()
        record = self.cloud_receipt(root, archive)
        before = self.hashes(self.source)
        with patch.object(cloud, 'transfer_for', side_effect=AssertionError('No network during adoption')):
            result = prepare_snapshot(self.source, self.output)
        self.assertEqual(self.hashes(self.source), before)
        self.assertEqual(result.sites[0].cloud_only, 1)
        target = self.output / 'Library'
        self.assertEqual(cloud.settings(target), {'schema_version': 1, 'library_id': record['library_id'], 'targets': {}})
        self.assertFalse((target / '.archive-state/cloud/outbox').exists())
        sidecar = next(p for p in target.glob('*/*.json') if p.name != 'series.json')
        saved = sidecar.with_suffix('.cbz')
        self.assertFalse(saved.exists())
        receipts = cloud.receipts_for(target, saved, 'b2')
        self.assertEqual(len(receipts), 1)
        self.assertNotIn('connection', receipts[0])
        self.assertNotIn('PRIVATE', json.dumps(receipts))
        self.assertEqual(receipts[0]['version'], record['version'])
        page = Catalog(b'fixture').page(Workspace('fixture', self.output), True, kind='episodes')
        self.assertEqual(page['episode_count'], 1)
        self.assertEqual(page['files'][0]['kind'], 'cbz')
        self.assertIsNone(page['files'][0]['id'])
        remote = next(g for g in page['locations'] if g['id'] != 'device')
        self.assertFalse(remote['connected'])
        with Inventory(target) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'], {'archived': 1})
            self.assertEqual(inventory.pending(), [])
        with closing(PublicationStore(target)) as store:
            self.assertEqual(store.episode_path(series, episode), saved)
            self.assertTrue(store.episode_is_offloaded(series, episode, saved))

    def test_verified_outbox_only_record_is_retained_as_history_without_a_retry(self):
        root, archive, _, _ = self.publish()
        record = self.cloud_receipt(root, archive)
        shutil.rmtree(root / '.archive-state/cloud/history')
        cloud._write(root / '.archive-state/cloud/outbox/b2' / (record['episode_id'] + '.json'), record)
        prepare_snapshot(self.source, self.output)
        self.assertTrue(list((self.output / 'Library/.archive-state/cloud/history/b2').glob('*/*.json')))
        self.assertFalse((self.output / 'Library/.archive-state/cloud/outbox').exists())

    def test_a_pending_upload_is_not_evidence_for_a_missing_local_file(self):
        root, archive, _, _ = self.publish()
        record = self.cloud_receipt(root, archive)
        shutil.rmtree(root / '.archive-state/cloud/history')
        cloud._write(root / '.archive-state/cloud/outbox/b2' / (record['episode_id'] + '.json'), record | {'state': 'pending'})
        self.assert_refused('incomplete')

    def test_cloud_history_without_its_library_identity_is_not_silently_dropped(self):
        root, archive, _, _ = self.publish()
        self.cloud_receipt(root, archive, remove=False)
        (root / '.archive-state/cloud/targets.json').unlink()
        self.assert_refused('incomplete')

    def test_absent_or_damaged_cloud_receipt_does_not_erase_a_missing_episode(self):
        for state in ('absent', 'bad-hash', 'wrong-library', 'no-eviction'):
            with self.subTest(state=state), tempfile.TemporaryDirectory(dir=self.base) as name:
                self.source, self.output = Path(name) / 'source', Path(name) / 'output'
                root, archive, _, _ = self.publish()
                record = self.cloud_receipt(root, archive)
                history = next((root / '.archive-state/cloud/history/b2').glob('*/*.json'))
                if state == 'absent':
                    history.unlink()
                else:
                    if state == 'bad-hash':
                        record['files'][1]['sha256'] = '0' * 64
                    elif state == 'wrong-library':
                        record['library_id'] = str(uuid.uuid4())
                    else:
                        record['eviction_requested'] = False
                    history.write_text(json.dumps(record))
                self.assert_refused('incomplete')

    def test_neutral_export_without_private_provenance_refuses_working_adoption(self):
        root, _, _, _ = self.publish()
        shutil.rmtree(root / '.archive-state/provenance')
        self.assert_refused()

    def test_partial_and_mismatched_account_bindings_are_refused(self):
        for change in ("DELETE FROM settings WHERE key='account_binding'",
                       "UPDATE settings SET value='PRIVATE_INVALID' WHERE key='account_salt'"):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.base) as name:
                self.source, self.output = Path(name) / 'source', Path(name) / 'output'
                root, _, _, _ = self.publish()
                with Inventory(root) as inventory:
                    inventory.db.execute(change); inventory.db.commit()
                self.assert_refused()

    def test_a_valid_uppercase_legacy_salt_keeps_the_existing_account_binding(self):
        root, _, _, _ = self.publish()
        with Inventory(root) as inventory:
            inventory.db.execute("UPDATE settings SET value=upper(value) WHERE key='account_salt'")
            inventory.db.commit()
        prepare_snapshot(self.source, self.output)
        with Inventory(self.output / 'Library') as inventory:
            self.assertEqual(inventory.account_binding_state('11'), 'matches')

    def test_staged_wal_binding_is_read_without_writing_the_input_or_copying_a_running_job(self):
        root, _, _, _ = self.publish(bind=False)
        with Inventory(root) as active_source:
            active_source.bind_account('11')
            before = self.hashes(self.source)
            result = prepare_snapshot(self.source, self.output)
            self.assertEqual(self.hashes(self.source), before)
        self.assertTrue(result.sites[0].account_bound)
        with Inventory(self.output / 'Library') as inventory:
            self.assertEqual(inventory.account_binding_state('11'), 'matches')

    def test_a_view_or_corrupt_database_is_not_installed_into_the_new_core(self):
        for kind in ('view', 'corrupt'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.base) as name:
                self.source, self.output = Path(name) / 'source', Path(name) / 'output'
                root, _, _, _ = self.publish()
                path = root / '.archive-state/inventory.sqlite3'
                if kind == 'view':
                    with closing(sqlite3.connect(path)) as db:
                        db.executescript("DROP TABLE settings; CREATE VIEW settings AS SELECT 'account_salt' AS key, 'PRIVATE' AS value;")
                else:
                    path.write_bytes(b'PRIVATE not a database')
                self.assert_refused()

    def test_archived_source_row_without_its_verified_archive_is_incomplete(self):
        root, _, _, _ = self.publish()
        with Inventory(root) as inventory:
            inventory.observe('42', 'Saved series', [{'id': '82', 'title': 'Lost', 'order': 2,
                                                     'access_state': 'tapas_free'}], 'fixture')
            inventory.mark('82', 'archived', path=root / 'missing/lost.cbz')
        self.assert_refused('incomplete')

    def test_an_unbound_portable_library_does_not_gain_an_invented_account(self):
        self.publish(bind=False)
        result = prepare_snapshot(self.source, self.output)
        self.assertFalse(result.sites[0].account_bound)
        with Inventory(self.output / 'Library') as inventory:
            self.assertEqual(inventory.account_binding_state('11'), 'unbound')

    def test_empty_location_creates_an_empty_held_inventory(self):
        (self.source / 'Library').mkdir(parents=True)
        result = prepare_snapshot(self.source, self.output)
        self.assertEqual((result.sites[0].series, result.sites[0].episodes), (0, 0))
        with Inventory(self.output / 'Library') as inventory:
            self.assertEqual(inventory.pending(), [])
            self.assertEqual(inventory.status()['episodes_by_status'], {})

    def test_duplicate_episode_or_series_identity_cannot_merge_unrelated_files(self):
        root, archive, _, _ = self.publish(legacy=True)
        duplicate = root / 'Different name'
        shutil.copytree(archive.parent, duplicate)
        self.assert_refused()

    def test_mixed_provider_metadata_in_one_library_is_refused(self):
        root, archive, _, _ = self.publish(legacy=True)
        sidecar = sidecar_path(archive)
        value = json.loads(sidecar.read_text())
        value['source'] = 'webtoon'
        sidecar.write_text(json.dumps(value))
        self.assert_refused()

    def test_no_identity_is_guessed_from_a_plausible_archive_filename(self):
        _, archive, _, _ = self.publish(legacy=True)
        sidecar = sidecar_path(archive)
        value = json.loads(sidecar.read_text()); value.pop('source_episode_id')
        sidecar.write_text(json.dumps(value))
        self.assert_refused()

    def test_corrupt_comic_or_novel_cannot_be_marked_complete(self):
        for novel in (False, True):
            with self.subTest(novel=novel), tempfile.TemporaryDirectory(dir=self.base) as name:
                self.source, self.output = Path(name) / 'source', Path(name) / 'output'
                _, archive, _, _ = self.publish(novel=novel)
                archive.write_bytes(b'PRIVATE broken archive')
                self.assert_refused()

    def test_unfinished_and_unrecognized_series_files_are_not_an_empty_success(self):
        for filename in ('episode.cbz.tmp', 'uncatalogued.cbz', 'unexpected.json', 'nested'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(dir=self.base) as name:
                self.source, self.output = Path(name) / 'source', Path(name) / 'output'
                _, archive, _, _ = self.publish()
                path = archive.parent / filename
                if filename == 'nested':
                    path.mkdir()
                else:
                    path.write_text('PRIVATE')
                self.assert_refused()

    def test_relevant_symlinks_are_refused_and_their_targets_are_untouched(self):
        _, archive, _, _ = self.publish()
        outside = self.base / 'outside.cbz'; archive.rename(outside)
        before = outside.read_bytes()
        archive.symlink_to(outside)
        self.assert_refused()
        self.assertEqual(outside.read_bytes(), before)

    def test_existing_destination_and_overlapping_roots_are_not_overwritten(self):
        self.publish()
        self.output.mkdir(); sentinel = self.output / 'keep'; sentinel.write_text('retained')
        self.assert_refused('destination')
        self.assertEqual(sentinel.read_text(), 'retained')
        for destination in (self.source, self.source / 'nested', self.base):
            with self.subTest(destination=destination), self.assertRaises(LibraryImportError):
                prepare_snapshot(self.source, destination)

    def test_cancellation_mid_copy_leaves_no_ready_receipt_and_keeps_the_source(self):
        self.publish()
        before = self.hashes(self.source)

        def cancel():
            if list(self.output.glob('Library/*/*.cbz')):
                raise LibraryImportError('cancelled')

        with self.assertRaises(LibraryImportError) as caught:
            prepare_snapshot(self.source, self.output, check=cancel)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(self.hashes(self.source), before)
        self.assertFalse((self.output / RECEIPT).exists())

    def test_source_mutation_after_copy_refuses_to_publish_a_receipt(self):
        _, archive, _, _ = self.publish()
        changed = False

        def mutate():
            nonlocal changed
            if not changed and list(self.output.glob('Library/*/*.cbz')) and list(self.output.glob('Library/*/*.json')):
                changed = True
                with sidecar_path(archive).open('a') as stream:
                    stream.write('\n')

        with self.assertRaises(LibraryImportError) as caught:
            prepare_snapshot(self.source, self.output, check=mutate)
        self.assertTrue(changed)
        self.assertEqual(caught.exception.code, 'changed')
        self.assertFalse((self.output / RECEIPT).exists())

    def test_space_failure_has_a_safe_message_and_no_ready_output(self):
        self.publish()
        before = self.hashes(self.source)
        with patch('comic_archive.working_library._copy', side_effect=OSError('PRIVATE disk path full')):
            self.assert_refused()
        self.assertEqual(self.hashes(self.source), before)

    def test_completed_preparation_is_recoverable_only_with_the_same_output_bytes(self):
        self.publish()
        result = prepare_snapshot(self.source, self.output)
        self.assertEqual(prepared_snapshot(self.output), result)
        saved = next(self.output.glob('Library/*/*.cbz'))
        saved.write_bytes(b'changed')
        with self.assertRaises(LibraryImportError):
            prepared_snapshot(self.output)

    def test_added_private_files_or_corrupt_receipt_do_not_become_a_recoverable_import(self):
        self.publish()
        prepare_snapshot(self.source, self.output)
        added = self.output / 'session'; added.write_text('PRIVATE')
        with self.assertRaises(LibraryImportError):
            prepared_snapshot(self.output)
        added.unlink()
        path = self.output / RECEIPT
        value = json.loads(path.read_text()); value['sites'][0]['episodes'] += 1
        path.write_text(json.dumps(value))
        with self.assertRaises(LibraryImportError):
            prepared_snapshot(self.output)

    def test_recovery_after_native_marker_write_requires_the_same_ticket_source_and_digest(self):
        self.publish()
        result = prepare_snapshot(self.source, self.output)
        ticket, source_key = str(uuid.uuid4()), 'a' * 64
        marker = self.output / LOCATION_MARKER
        marker.write_text(json.dumps({'v': 1, 'id': ticket, 'source': source_key, 'digest': result.digest}))
        self.assertEqual(prepared_snapshot(self.output, ticket=ticket, source_key=source_key), result)
        for arguments in ({}, {'ticket': str(uuid.uuid4()), 'source_key': source_key},
                          {'ticket': ticket, 'source_key': 'b' * 64}):
            with self.subTest(arguments=arguments), self.assertRaises(LibraryImportError):
                prepared_snapshot(self.output, **arguments)
