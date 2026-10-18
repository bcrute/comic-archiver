import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from unittest.mock import Mock, patch

from support import PRIVATE_FILE, helper_program
from comic_archive import cloud
from comic_archive.archive import sidecar_path
from comic_archive.audit import audit_library
from comic_archive.inventory import Inventory
from comic_archive.cloud_errors import CloudCleanupError, CloudTransferError, safe_code
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.network import NetworkPolicyHold
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter, png


class FakeTransfer:
    files = {}
    fail_provider = None
    fail_delete = False
    fail_upload = False

    def __init__(self, provider, target):
        self.provider = provider

    def upload(self, source, destination, expected):
        data = Path(source).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ArchiveError('changed input')
        if self.provider == self.fail_provider or self.fail_upload:
            raise ArchiveError('synthetic authentication/space failure')
        self.files[(self.provider,destination)] = data

    def upload_bytes(self, data, destination):
        if self.provider == self.fail_provider or self.fail_upload:
            raise ArchiveError('synthetic authentication/space failure')
        self.files[(self.provider,destination)] = bytes(data)

    def sha1s(self, path):
        return {(key[len(path)+1:] if key != path else key.rsplit('/',1)[-1]): hashlib.sha1(data).hexdigest()
                for (provider,key),data in self.files.items()
                if provider == self.provider and (key == path or key.startswith(path + '/'))}

    def delete_upload(self, path):
        self.files.pop((self.provider,path),None)

    def remote(self, path):return path

    def delete_test_file(self, path):
        if self.fail_delete:raise ArchiveError('synthetic cleanup outage')
        self.files.pop((self.provider,path),None)

    def run(self, command, path, *args, **kwargs):
        assert command == 'lsf'
        return ('\n'.join(key.rsplit('/',1)[-1] for provider,key in self.files
                          if provider == self.provider and key.rsplit('/',1)[0] == path)).encode()


class CloudTests(unittest.TestCase):
    def test_an_outbox_record_without_the_apps_folder_is_not_retried(self):
        self.publish(); self.ready(); FakeTransfer.fail_upload = True
        with self.assertRaises(ArchiveError): self.publish('82')
        receipt = next((self.root / '.archive-state/cloud/outbox/google').glob('*.json'))
        record = json.loads(receipt.read_text())
        self.assertEqual(record.pop('remote_root'), 'Comic-Archive')
        cloud._write(receipt, record)
        FakeTransfer.fail_upload = False
        before = dict(FakeTransfer.files)
        with self.assertRaises(CloudTransferError): cloud.upload_pending(self.root)
        self.assertEqual(FakeTransfer.files, before)
        self.assertEqual(json.loads(receipt.read_text())['state'], 'failed')

    def test_cleanup_keeps_original_connection_after_account_or_client_changes(self):
        self.publish(); self.configured(); FakeTransfer.fail_delete = True
        with self.assertRaises(ArchiveError): cloud.smoke_test(self.root, 'google', episodes=1)
        receipt = next((self.root/'.archive-state/cloud/tests').glob('*.json'))
        old = cloud.settings(self.root)['targets']['google']
        second = self.base/'another.conf'; second.write_text('[google]\ntype=drive\n'); second.chmod(0o600)
        cloud.configure_target(self.root, 'google', config=second)
        FakeTransfer.fail_delete = False
        from comic_archive.cloud import transfer_for
        with patch('comic_archive.cloud.transfer_for', wraps=transfer_for) as connect:
            cloud.cleanup_test(self.root, receipt.stem)
        self.assertEqual(connect.call_args.args[2]['config'], old['config'])
        self.assertEqual(connect.call_args.args[2]['target_id'], old['target_id'])
        self.assertFalse(FakeTransfer.files)
        self.assertNotIn(str(self.config), json.dumps(cloud.status(self.root)))

    def test_legacy_cleanup_receipt_is_pinned_before_connection_is_replaced(self):
        self.publish(); self.configured(); FakeTransfer.fail_delete = True
        with self.assertRaises(ArchiveError): cloud.smoke_test(self.root, 'google', episodes=1)
        receipt = next((self.root/'.archive-state/cloud/tests').glob('*.json'))
        record = json.loads(receipt.read_text()); record.pop('connection')
        cloud._write(receipt, record)
        old = cloud.settings(self.root)['targets']['google']
        self.configured()
        self.assertEqual(json.loads(receipt.read_text())['connection']['target_id'], old['target_id'])

    def test_classified_upload_failure_is_durable_retains_original_and_clears_after_verified_retry(self):
        from comic_archive.cloud_errors import CloudTransferError
        self.publish();self.ready()
        with patch.object(FakeTransfer,'upload',side_effect=CloudTransferError('cloud_space')):
            with self.assertRaises(CloudTransferError):self.publish('82')
        receipt=next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))
        record=json.loads(receipt.read_text());archive=self.root/record['archive'];before=archive.read_bytes()
        self.assertEqual(record['state'],'failed');self.assertEqual(record['error_code'],'cloud_space')
        self.assertEqual(cloud.status(self.root)['targets'][0]['failure_reasons'],{'cloud_space':1})
        receipt.write_text(json.dumps(record|{'error_code':{'token':'PRIVATE'}}))
        status=cloud.status(self.root)
        self.assertEqual(status['targets'][0]['failure_reasons'],{'cloud_unknown':1})
        self.assertNotIn('PRIVATE',json.dumps(status))
        receipt.write_text(json.dumps(record))
        result=cloud.upload_pending(self.root)
        self.assertEqual(result['local_archives_deleted'],1);self.assertFalse(archive.exists())
        self.assertIn(before,FakeTransfer.files.values())
        self.assertNotIn('error_code',json.loads(receipt.read_text()))
        self.assertEqual(cloud.status(self.root)['targets'][0]['failure_reasons'],{})

    def test_classified_test_cleanup_is_retryable_without_deleting_local_original(self):
        from comic_archive.cloud_errors import CloudCleanupError, CloudTransferError
        archive,_=self.publish();before=archive.read_bytes();self.configured()
        with patch.object(FakeTransfer,'delete_test_file',side_effect=CloudTransferError('cloud_login_required')):
            with self.assertRaises(CloudCleanupError):cloud.smoke_test(self.root,'google',episodes=1)
        cleanup=cloud.status(self.root)['pending_test_cleanup'][0]
        self.assertEqual(cleanup['error_code'],'cloud_login_required')
        self.assertEqual(archive.read_bytes(),before)
        cloud.cleanup_test(self.root,cleanup['test_id'])
        self.assertEqual(cloud.status(self.root)['pending_test_cleanup'],[])
        self.assertEqual(archive.read_bytes(),before)

    def test_network_hold_retains_its_type_and_records_safe_cloud_recovery_hint(self):
        from comic_archive.network import NetworkPolicyHold
        self.publish();self.ready()
        with patch.object(FakeTransfer,'upload',side_effect=NetworkPolicyHold('PRIVATE')):
            with self.assertRaises(NetworkPolicyHold):self.publish('82')
        self.assertEqual(cloud.status(self.root)['targets'][0]['failure_reasons'],{'cloud_network_held':1})
        self.assertNotIn('PRIVATE',json.dumps(cloud.status(self.root)))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'Library'
        self.config = self.base / 'rclone.conf';self.config.write_text('[google]\ntype=drive\n[proton]\ntype=protondrive\n')
        self.config.chmod(0o600)
        FakeTransfer.files = {};FakeTransfer.fail_provider = None;FakeTransfer.fail_delete = False;FakeTransfer.fail_upload = False
        transfer = patch.object(cloud,'Transfer',FakeTransfer);transfer.start();self.addCleanup(transfer.stop)
        self.series = Series('42','Synthetic',source='tapas')

    def publish(self, key='81', novel=False, cover=False):
        episode = Episode(key,'42','Chapter '+key,int(key), () if novel else (Page(1,'https://cdn.test/a',False),),
                          'tapas_free',content_type='novel' if novel else 'comic',text_html='<p>Synthetic text.</p>' if novel else None)
        with closing(PublicationStore(self.root)) as store:
            folder = store.archive(Manifest(self.series,(episode,)),MemoryAdapter(),progress=lambda _:None)
            path = store.episode_path(self.series,episode,folder)
            if cover:store.write_series_metadata(self.series,cover=png(1))
        return path, episode

    def configured(self, provider='google'):
        return cloud.configure_target(self.root,provider,config=self.config,remote=provider)

    def ready(self, provider='google'):
        self.configured(provider)
        cloud.smoke_test(self.root,provider,episodes=1)
        cloud.enable(self.root,provider,True)

    def test_unconfigured_cloud_is_read_only_and_upload_does_not_create_a_library(self):
        self.assertFalse(cloud.status(self.root)['targets'][0]['connected'])
        self.assertEqual(cloud.upload_pending(self.root)['uploaded_episodes'],0)
        self.assertFalse(self.root.exists())

    def test_connection_is_private_disabled_until_tested_and_rejects_unsafe_names(self):
        result = self.configured()
        self.assertFalse(result['targets'][0]['enabled'])
        self.assertNotIn(str(self.config),json.dumps(result))
        with self.assertRaises(ArchiveError):cloud.enable(self.root,'google',True)
        cloud.enable(self.root,'google',False)
        self.assertEqual((self.root/'.archive-state/cloud/targets.json').stat().st_mode & 0o777,PRIVATE_FILE)
        for name in ('../google',':local:','--secret','',None):
            if name is None:continue
            with self.assertRaises(ArchiveError):cloud.configure_target(self.root,'google',config=self.config,remote=name)
        for provider in ('sftp','local',None):
            with self.assertRaises(ArchiveError):cloud.configure_target(self.root,provider,config=self.config)
        with self.assertRaises(ArchiveError):cloud.enable(self.root,'unknown',True)
        with self.assertRaises(ArchiveError):cloud.enable(self.root,'google',1)
        with self.assertRaises(ArchiveError):cloud.enable(self.base/'fresh','google',True)

    def test_smoke_uploads_comic_novel_and_cover_then_deletes_only_its_cloud_files(self):
        first,_ = self.publish(cover=True);second,_ = self.publish('82',novel=True)
        originals = {p:p.read_bytes() for p in (first,second,sidecar_path(first),sidecar_path(second))}
        self.configured('proton')
        FakeTransfer.files[('proton','unrelated/private.txt')] = b'keep'
        result = cloud.smoke_test(self.root,'proton',episodes=2)
        self.assertEqual(result['verified_episodes'],2);self.assertTrue(result['test_files_deleted'])
        self.assertEqual(FakeTransfer.files,{('proton','unrelated/private.txt'):b'keep'})
        self.assertEqual({p:p.read_bytes() for p in originals},originals)
        self.assertTrue(cloud.status(self.root)['targets'][1]['tested'])
        self.assertFalse(cloud.status(self.root)['targets'][1]['enabled'])
        self.assertEqual(cloud.cleanup_test(self.root,result['test_id']),{'test_files_deleted':True})

    def test_failed_test_upload_cleans_partial_files_and_does_not_mark_connection_tested(self):
        first,_ = self.publish();self.configured()
        FakeTransfer.fail_upload = True
        with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'google',episodes=1)
        self.assertTrue(first.exists());self.assertFalse(cloud.status(self.root)['targets'][0]['tested'])
        self.assertFalse(FakeTransfer.files)

    def test_cleanup_failure_keeps_exact_journal_and_can_resume_without_broad_deletion(self):
        self.publish();self.configured();FakeTransfer.fail_delete = True
        with self.assertRaisesRegex(ArchiveError,'cleanup is pending'):cloud.smoke_test(self.root,'google',episodes=1)
        receipt = next((self.root/'.archive-state/cloud/tests').glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['state'],'cleanup_pending')
        FakeTransfer.fail_delete = False
        self.assertTrue(cloud.cleanup_test(self.root,receipt.stem)['test_files_deleted'])
        self.assertFalse(FakeTransfer.files)
        self.assertFalse(cloud.status(self.root)['targets'][0]['tested'])

    def test_cleanup_rejects_modified_scope_and_invalid_ids_without_deleting_anything(self):
        self.publish();self.configured();FakeTransfer.fail_delete = True
        with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'google',episodes=1)
        receipt = next((self.root/'.archive-state/cloud/tests').glob('*.json'))
        value = json.loads(receipt.read_text());saved = dict(FakeTransfer.files)
        for files in (['unrelated/private.txt'],[value['prefix']+'/../private.txt'],['x']*13):
            receipt.write_text(json.dumps(value | {'files':files}))
            with self.assertRaises(ArchiveError):cloud.cleanup_test(self.root,receipt.stem)
            self.assertEqual(FakeTransfer.files,saved)
        with self.assertRaises(ArchiveError):cloud.cleanup_test(self.root,'../outside')

    def test_live_test_count_is_explicit_bounded_and_needs_completed_episodes(self):
        self.publish();self.configured()
        for count in (0,4,True,'2'):
            with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'google',episodes=count)
        with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'proton',episodes=1)
        with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'google',episodes=2)
        self.assertFalse(cloud.status(self.root)['targets'][0]['tested']);self.assertFalse(FakeTransfer.files)

    def test_automatic_upload_deletes_only_after_verified_receipt_and_never_uploads_private_state(self):
        original,_ = self.publish();self.ready()
        archive,episode = self.publish('82')
        self.assertTrue(original.exists());self.assertFalse(archive.exists());self.assertTrue(sidecar_path(archive).is_file())
        receipt = cloud.offloaded(self.root,archive)
        self.assertTrue(receipt['eviction_requested']);self.assertEqual(receipt['state'],'verified')
        self.assertEqual({key.rsplit('/',1)[-1] for _,key in FakeTransfer.files},{'episode.cbz','episode.json','series.json'})
        data = b'\n'.join(FakeTransfer.files.values())
        self.assertNotIn(b'cdn.test',data);self.assertNotIn(b'rclone.conf',data)
        self.assertNotIn(b'source_episode_id',data)
        with closing(PublicationStore(self.root)) as store:
            adapter = Mock();store.archive(Manifest(self.series,(episode,)),adapter,progress=lambda _:None)
            adapter.retrieve.assert_not_called()
            self.assertEqual(store.series_episode_count(self.series,archive.parent),2)

    def test_cloud_only_space_plan_needs_episode_staging_and_retains_a_shared_partial_page(self):
        from contextlib import closing
        from comic_archive.archive import Store, digest
        from comic_archive.storage import storage_plan
        self.publish();self.ready()
        with closing(Store(self.root)) as store:
            pending = Episode('83','42','Unfinished',83,(Page(1,'https://cdn.test/other',False),),'tapas_free')
            store.remember_page('tapas',pending,pending.pages[0],png(1))
        archive,_ = self.publish('82')
        self.assertFalse(archive.exists())
        self.assertTrue((self.root/'.archive-cache'/digest(png(1))).is_file())
        plan = storage_plan(self.root,episode_count=1000)
        self.assertEqual(plan['storage_destination'],'cloud')
        self.assertEqual(plan['estimated_additional_local_bytes'],0)
        self.assertEqual(plan['estimated_required_free_bytes'],plan['staging_bytes'] + plan['reserve_bytes'])

    def test_failed_upload_retains_archive_then_retry_deletes_without_source_requests(self):
        self.publish();self.ready();FakeTransfer.fail_provider = 'google'
        with self.assertRaises(ArchiveError):self.publish('82')
        archive = next(p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name)
        before = archive.read_bytes()
        self.assertEqual(cloud.status(self.root)['targets'][0]['failed'],1)
        FakeTransfer.fail_provider = None
        report = cloud.upload_pending(self.root,limit=1)
        self.assertEqual(report['uploaded_episodes'],1);self.assertEqual(report['local_archives_deleted'],1)
        self.assertFalse(archive.exists())
        self.assertIn(before,FakeTransfer.files.values())
        self.assertEqual(cloud.upload_pending(self.root)['uploaded_episodes'],0)

    def test_two_targets_require_both_copies_before_local_deletion(self):
        self.publish();self.ready();self.ready('proton');FakeTransfer.fail_provider = 'proton'
        with self.assertRaises(ArchiveError):self.publish('82')
        archive = next(p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name)
        self.assertTrue(archive.exists())
        FakeTransfer.fail_provider = None
        result = cloud.upload_pending(self.root)
        self.assertEqual(result['uploaded_episodes'],1);self.assertFalse(archive.exists())
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],1)
        self.assertEqual(cloud.status(self.root)['targets'][1]['verified'],1)

    def test_inventory_restart_recovers_deletion_before_job_mark_and_audit_distinguishes_local_cloud(self):
        self.publish();self.ready()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Synthetic',[{'id':'82','title':'Chapter 82','order':82,'access_state':'tapas_free'}],'fixture')
            inventory.mark('82','downloading')
        archive,_ = self.publish('82')
        with Inventory(self.root) as inventory:
            inventory.reconcile(full=True)
            self.assertEqual(inventory.pending(),[])
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='82'").fetchone()[0],'archived')
            inventory.reconcile()
            self.assertEqual(inventory.pending(),[])
        audit = audit_library(self.root)
        self.assertEqual(audit['cloud_uploaded_inventory_episode_count'],1)
        self.assertEqual(audit['missing_recorded_accessible_episode_count'],0)
        self.assertFalse(audit['cloud_availability_checked'])
        self.assertFalse(audit['episodes'][0]['archive_verified'])
        sidecar_path(archive).write_text('{}')
        self.assertIsNone(cloud.offloaded(self.root,archive))
        with Inventory(self.root) as inventory:
            inventory.reconcile();self.assertEqual(len(inventory.pending()),1)

    def test_audit_flags_an_offloaded_episode_whose_inventory_row_is_no_longer_archived(self):
        self.publish();self.ready()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Synthetic',[{'id':'82','title':'Chapter 82','order':82,'access_state':'tapas_free'}],'fixture')
            inventory.mark('82','downloading')
        self.publish('82')
        with Inventory(self.root) as inventory:
            inventory.reconcile(full=True)
            inventory.db.execute("UPDATE episodes SET status='queued' WHERE id='82'");inventory.db.commit()
        audit = audit_library(self.root)
        self.assertEqual(audit['inventory_index_problems'],[{'source_episode_id':'82','reason':'inventory_index_out_of_sync'}])
        self.assertIn('inventory_index_out_of_sync',audit['coverage_blockers'])
        self.assertFalse(audit['observed_snapshot_archived'])
        self.assertEqual(audit['cloud_uploaded_inventory_episode_count'],1)
        episode = {row['source_episode_id']:row for row in audit['episodes']}['82']
        self.assertTrue(episode['cloud_upload_verified']);self.assertFalse(episode['archive_verified'])

    @patch('builtins.print')
    def test_actual_archive_job_marks_cloud_only_episode_complete_and_restart_does_not_fetch_source(self, output):
        from comic_archive.jobs import archive_discovered
        from test_jobs import AccountAdapter
        self.publish();self.ready()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Synthetic',[{'id':'82','title':'E82','order':2,'access_state':'tapas_unlocked'}],'fixture')
        adapter = AccountAdapter()
        result = archive_discovered(adapter,self.root,limit=1)
        self.assertEqual(result['archived_episode_ids'],['82'])
        self.assertEqual(adapter.downloads,[1,2])
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))),1)
        again = archive_discovered(adapter,self.root,limit=1)
        self.assertEqual(again['archived_episode_ids'],[]);self.assertEqual(adapter.downloads,[1,2])
        audit = audit_library(self.root)
        self.assertEqual(audit['cloud_uploaded_inventory_episode_count'],1)
        self.assertEqual(audit['missing_recorded_accessible_episode_count'],0)

    def test_reconnected_cloud_does_not_turn_historical_eviction_into_accidental_source_loss(self):
        self.publish();self.ready();archive,_ = self.publish('82')
        self.configured()
        self.assertTrue(cloud.offloaded(self.root,archive))
        self.assertFalse(cloud.status(self.root)['targets'][0]['enabled'])

    def test_retry_after_reconnecting_keeps_an_offloaded_episode_saved_without_local_bytes(self):
        from comic_archive.hosted import Workspace
        from comic_archive.saved_library import Catalog
        self.publish(); self.ready('b2'); archive, episode = self.publish('82')
        receipt = next((self.root / '.archive-state/cloud/outbox/b2').glob('*.json'))
        previous = json.loads(receipt.read_text())
        # An upgrade from 0.1.4 starts with only the outbox receipt, without the new history folder.
        shutil.rmtree(self.root / '.archive-state/cloud/history')
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Synthetic', [{'id':'82', 'title':episode.title, 'order':episode.order,
                                                  'access_state':'tapas_free'}], 'fixture')
            inventory.mark('82', 'archived', path=archive)
        self.ready('b2')
        remote = dict(FakeTransfer.files)
        self.assertEqual(cloud.upload_pending(self.root)['uploaded_episodes'], 0)
        self.assertEqual(json.loads(receipt.read_text()), previous)
        self.assertEqual(FakeTransfer.files, remote)
        self.assertTrue(cloud.offloaded(self.root, archive))
        with Inventory(self.root) as inventory:
            inventory.reconcile()
            self.assertEqual(inventory.status()['episodes_by_status'], {'archived':1})
        catalog = Catalog(b'review-fixture').page(Workspace('a'*32, self.base), True, kind='episodes')
        self.assertEqual(catalog['episode_count'], 2)
        previous_location = next(g for g in catalog['locations'] if g['id'].startswith('b2-'))
        self.assertEqual(previous_location['episodes'], 1)
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=AssertionError('No source download'))
        with closing(PublicationStore(self.root)) as store:
            store.archive(Manifest(self.series, (episode,)), adapter, progress=lambda _: None)
        adapter.retrieve.assert_not_called()

    def test_a_failed_upload_to_another_connection_preserves_the_previous_verified_copy(self):
        from comic_archive.hosted import Workspace
        from comic_archive.saved_library import Catalog
        self.publish(); self.ready()
        with patch.object(cloud, 'evict_uploaded', return_value={'local_archives_deleted':0, 'freed_bytes':0}):
            archive, _ = self.publish('82')
        self.ready()
        cloud.queue_episode(self.root, archive)
        FakeTransfer.fail_upload = True
        with self.assertRaises(CloudTransferError):
            cloud.upload_pending(self.root)
        catalog = Catalog(b'review-fixture')
        workspace = Workspace('a'*32, self.base)
        value = catalog.page(workspace, True, kind='episodes', refresh=True)
        previous = next(g for g in value['locations'] if g['id'].startswith('google-'))
        self.assertEqual(previous['episodes'], 1)
        self.assertTrue(archive.is_file())
        FakeTransfer.fail_upload = False
        cloud.upload_pending(self.root)
        records = cloud.receipts_for(self.root, archive, 'google')
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r['target_id'] for r in records}), 2)
        self.assertTrue(cloud.offloaded(self.root, archive))
        value = catalog.page(workspace, True, kind='episodes', refresh=True)
        self.assertEqual(next(g for g in value['locations'] if g['id'] == 'google')['episodes'], 1)
        self.assertEqual(next(g for g in value['locations'] if g['id'].startswith('google-'))['episodes'], 1)

    def test_saved_history_remains_visible_when_the_retry_entry_is_removed(self):
        from comic_archive.hosted import Workspace
        from comic_archive.saved_library import Catalog
        self.publish(); self.ready(); archive, _ = self.publish('82')
        receipt = next((self.root / '.archive-state/cloud/outbox/google').glob('*.json'))
        receipt.unlink()
        self.assertTrue(cloud.offloaded(self.root, archive))
        value = Catalog(b'review-fixture').page(Workspace('a'*32, self.base), True, kind='episodes')
        self.assertEqual(value['episode_count'], 2)
        # A changed sidecar still cannot vouch for any old cloud copy.
        sidecar_path(archive).write_text('{}')
        self.assertFalse(cloud.offloaded(self.root, archive))

    def test_legacy_cloud_only_tar_is_reused_without_source_download(self):
        import zipfile
        from comic_archive.publication import import_metadata, write_compressed_comic
        self.publish();self.ready()
        cloud.enable(self.root,'google',False)
        cbz,episode=self.publish('82');old=cbz.with_suffix('.tar.zst')
        with closing(PublicationStore(self.root)) as store:
            metadata=import_metadata(sidecar_path(cbz))|{'format':'tar.zst'}
            with zipfile.ZipFile(cbz) as comic:
                write_compressed_comic(old,metadata,lambda page:comic.open(page['file']))
            cbz.unlink();store.publish_metadata(sidecar_path(old),metadata)
            store.db.execute('UPDATE episode_paths SET path=? WHERE path=?',(old.relative_to(self.root).as_posix(),cbz.relative_to(self.root).as_posix()))
            store.db.commit()
        cloud.enable(self.root,'google',True);cloud.queue_episode(self.root,old);cloud.upload_pending(self.root)
        self.assertFalse(old.exists());self.assertTrue(cloud.offloaded(self.root,old))
        adapter=MemoryAdapter();adapter.retrieve=Mock(side_effect=AssertionError('No source requests'))
        with closing(PublicationStore(self.root)) as store:
            store.archive(Manifest(self.series,(episode,)),adapter,progress=lambda _:None)
            self.assertEqual(store.episode_path(self.series,episode),old)
        adapter.retrieve.assert_not_called()
        self.assertFalse(cbz.exists())

    def test_receipt_corruption_and_changed_local_bytes_hold_deletion(self):
        self.publish();self.ready();FakeTransfer.fail_provider = 'google'
        with self.assertRaises(ArchiveError):self.publish('82')
        archive = next(p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name)
        path = next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))
        value = json.loads(path.read_text())
        path.write_text(json.dumps(value | {'archive':'../outside.tar.zst'}))
        FakeTransfer.fail_provider = None
        with self.assertRaises(ArchiveError):cloud.upload_pending(self.root)
        self.assertTrue(archive.exists())
        path.write_text(json.dumps(value));cloud.upload_pending(self.root)
        saved = json.loads(path.read_text())
        restored = next(data for (_,key),data in FakeTransfer.files.items() if key.endswith('episode.cbz'))
        archive.write_bytes(restored + b'damage')
        with self.assertRaises(ArchiveError):cloud.evict_uploaded(self.root)
        self.assertTrue(archive.exists())
        path.write_text(json.dumps(saved | {'files':[]}))
        self.assertIsNone(cloud._receipt_for(self.root, archive, 'google'))
        # The separate, still-valid history remains evidence until it too is damaged.
        self.assertTrue(cloud.offloaded(self.root, archive))
        for history in (self.root / '.archive-state/cloud/history/google').glob('*/*.json'):
            history.write_text(json.dumps(saved | {'files':[]}))
        self.assertIsNone(cloud.offloaded(self.root,archive))

    def test_existing_upload_is_explicit_bounded_and_does_not_enable_another_provider(self):
        first,_ = self.publish();self.publish('82');self.ready()
        result = cloud.upload_existing(self.root,limit=1,provider='google')
        self.assertEqual(result['uploaded_episodes'],1);self.assertFalse(first.exists())
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))),1)
        for limit in (0,101,True,'2'):
            with self.assertRaises(ArchiveError):cloud.upload_existing(self.root,limit=limit)
        for limit in (0,True,'2'):
            with self.assertRaises(ArchiveError):cloud.upload_pending(self.root,limit=limit)
        with self.assertRaises(ArchiveError):cloud.upload_pending(self.root,provider='local')

    def test_settings_symlinks_malformed_json_and_wrong_target_fields_are_rejected(self):
        self.configured();path = self.root/'.archive-state/cloud/targets.json';saved = path.read_text()
        for text in ('[]','{',json.dumps({'schema_version':1}),saved.replace('"enabled": false','"enabled": 1')):
            path.write_text(text)
            with self.assertRaises(ArchiveError):cloud.settings(self.root)
        path.write_text('x'*(1024**2+1))
        with self.assertRaises(ArchiveError):cloud.settings(self.root)
        path.unlink();path.symlink_to(self.config)
        with self.assertRaises(ArchiveError):cloud.settings(self.root)

    @unittest.skipIf(os.name == 'nt', 'Windows keeps no permission bits; the file is private through the profile folder.')
    def test_config_readable_by_others_is_refused(self):
        self.publish();self.ready()
        self.config.chmod(0o644)
        with self.assertRaises(ArchiveError):cloud.configure_target(self.root,'google',config=self.config)

    def test_reconnection_resets_test_approval_and_linked_config_is_refused(self):
        self.publish();self.ready()
        old = cloud.settings(self.root)['targets']['google']['target_id']
        self.configured()
        target = cloud.settings(self.root)['targets']['google']
        self.assertNotEqual(target['target_id'],old);self.assertFalse(target['tested']);self.assertFalse(target['enabled'])
        link = self.base/'link.conf';link.symlink_to(self.config)
        with self.assertRaises(ArchiveError):cloud.configure_target(self.root,'google',config=link)

    def test_interrupted_deletion_after_receipt_is_recovered_idempotently(self):
        self.publish();self.ready()
        unlink = Path.unlink
        def fail_archive(path,*args,**kwargs):
            if 'Chapter 82' in path.name and path.name.endswith('.cbz'):raise OSError('synthetic interruption')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_archive), self.assertRaises(OSError):self.publish('82')
        archive = next(p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name)
        self.assertTrue(cloud.offloaded(self.root,archive))
        self.assertEqual(cloud.upload_pending(self.root)['local_archives_deleted'],1)
        self.assertEqual(cloud.upload_pending(self.root)['local_archives_deleted'],0)

    def publish_page(self, key, color):
        class ColoredAdapter:
            def retrieve(self, page): return png(int(page.location.rsplit('/',1)[1]))
        episode = Episode(key,'42','Chapter '+key,int(key),(Page(1,f'https://cdn.test/{color}',False),),'tapas_free')
        with closing(PublicationStore(self.root)) as store:
            folder = store.archive(Manifest(self.series,(episode,)),ColoredAdapter(),progress=lambda _:None)
            return store.episode_path(self.series,episode,folder)

    def failed_receipt(self):
        """A library with one completed episode whose upload failed and was retained locally."""
        self.publish();self.ready();FakeTransfer.fail_provider = 'google'
        with self.assertRaises(ArchiveError):self.publish('82')
        FakeTransfer.fail_provider = None
        archive = next(p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name)
        return archive, next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))

    def test_receipt_from_another_library_or_for_a_changed_episode_uploads_nothing(self):
        archive, path = self.failed_receipt();value = json.loads(path.read_text())
        for change in ({'library_id':str(uuid.uuid4())},{'version':'0'*64}):
            path.write_text(json.dumps(value | change))
            with self.assertRaises(ArchiveError):cloud.upload_pending(self.root)
            self.assertEqual(FakeTransfer.files,{});self.assertTrue(archive.exists())
            self.assertEqual(json.loads(path.read_text())['state'],'failed')

    def test_symlinked_receipt_is_refused_instead_of_followed(self):
        archive, path = self.failed_receipt()
        elsewhere = self.base/'elsewhere.json';elsewhere.write_text(path.read_text());path.unlink();path.symlink_to(elsewhere)
        with self.assertRaises(ArchiveError):cloud.status(self.root)
        with self.assertRaises(ArchiveError):cloud.upload_pending(self.root)
        self.assertEqual(FakeTransfer.files,{});self.assertTrue(archive.exists())

    def test_reconnecting_requeues_receipts_made_for_the_previous_connection(self):
        self.publish();self.ready()
        with patch.object(cloud,'evict_uploaded',return_value={'local_archives_deleted':0,'freed_bytes':0}):
            archive,_ = self.publish('82')
        path = next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))
        self.assertEqual(json.loads(path.read_text())['state'],'verified')
        self.assertTrue(archive.exists())
        self.ready()  # a new connection: the earlier account's upload no longer counts
        FakeTransfer.files.clear()
        cloud.queue_episode(self.root,archive)
        record = json.loads(path.read_text())
        self.assertEqual(record['state'],'pending')
        self.assertEqual(record['target_id'],cloud.settings(self.root)['targets']['google']['target_id'])
        google = cloud.status(self.root)['targets'][0]
        self.assertEqual((google['pending'],google['verified']),(1,0))
        self.assertEqual(cloud.upload_pending(self.root)['local_archives_deleted'],1)
        self.assertFalse(archive.exists())
        self.assertTrue(any(key.endswith('episode.cbz') for _,key in FakeTransfer.files))

    def test_unverified_or_misplaced_archives_are_not_queued_for_upload(self):
        first,_ = self.publish();self.ready()
        first.write_bytes(b'corrupt')
        with self.assertRaises(ArchiveError):cloud.queue_episode(self.root,first)
        with self.assertRaises(ArchiveError):cloud.queue_episode(self.root,self.root/'.archive-state'/'x.cbz')
        self.assertEqual(list((self.root/'.archive-state/cloud/outbox').glob('*/*.json')),[])

    def test_upload_limit_leaves_the_remaining_episodes_pending_and_local(self):
        self.publish();self.ready();FakeTransfer.fail_provider = 'google'
        for key in ('82','83'):
            with self.assertRaises(ArchiveError):self.publish(key)
        FakeTransfer.fail_provider = None
        local = lambda: [p for p in self.root.glob('*/*.cbz') if 'Chapter 82' in p.name or 'Chapter 83' in p.name]
        self.assertEqual(len(local()),2)
        report = cloud.upload_pending(self.root,limit=1)
        self.assertEqual((report['uploaded_episodes'],report['local_archives_deleted']),(1,1))
        self.assertEqual(len(local()),1)
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],1)
        self.assertEqual(cloud.upload_pending(self.root)['uploaded_episodes'],1)
        self.assertEqual(local(),[])

    def test_existing_upload_with_a_larger_limit_covers_every_episode_and_only_the_selected_provider(self):
        first,_ = self.publish();second,_ = self.publish('82');self.ready();self.ready('proton')
        result = cloud.upload_existing(self.root,limit=5,provider='google')
        self.assertEqual(result['uploaded_episodes'],2)
        self.assertFalse(first.exists() or second.exists())
        self.assertEqual({provider for provider,_ in FakeTransfer.files},{'google'})
        proton = cloud.status(self.root)['targets'][1]
        self.assertEqual((proton['pending'],proton['verified']),(0,0))

    def test_local_deletion_needs_every_required_receipt_for_the_current_connection(self):
        self.publish();self.ready()
        with patch.object(cloud,'evict_uploaded',return_value={'local_archives_deleted':0,'freed_bytes':0}):
            archive,_ = self.publish('82')
        original = archive.read_bytes()
        receipt = next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))
        value = json.loads(receipt.read_text());self.assertEqual(value['state'],'verified')
        for targets in ([],['bogus'],'google'):
            receipt.write_text(json.dumps(value | {'required_targets':targets}))
            with self.assertRaises(ArchiveError):cloud.evict_uploaded(self.root)
            self.assertEqual(archive.read_bytes(),original)
        receipt.write_text(json.dumps(value | {'required_targets':['google','proton']}))
        self.assertEqual(cloud.evict_uploaded(self.root)['local_archives_deleted'],0)
        self.assertEqual(archive.read_bytes(),original)
        receipt.write_text(json.dumps(value))
        archive.write_bytes(b'corrupt')
        with self.assertRaises(ArchiveError):cloud.evict_uploaded(self.root)
        self.assertEqual(archive.read_bytes(),b'corrupt')
        archive.write_bytes(original)
        self.configured()  # a reconnected account cannot vouch for what the old connection uploaded
        self.assertEqual(cloud.evict_uploaded(self.root)['local_archives_deleted'],0)
        self.assertEqual(archive.read_bytes(),original)

    def test_a_damaged_cloud_receipt_is_not_evidence_that_an_episode_is_safely_offloaded(self):
        self.publish();self.ready();archive,_ = self.publish('82')
        self.assertFalse(archive.exists())
        receipt = next((self.root/'.archive-state/cloud/outbox/google').glob('*.json'))
        value = json.loads(receipt.read_text());files = value['files']
        self.assertTrue(cloud.offloaded(self.root,archive))
        damaged = {'unparseable time':{'verified_at':'yesterday'},'time without zone':{'verified_at':'2026-01-01T00:00:00'},
                   'missing time':{'verified_at':None},
                   'malformed hash':{'files':[files[0] | {'sha256':'not-a-hash'},*files[1:]]},
                   'other archive':{'files':[files[0] | {'path':'Synthetic/other.cbz'},*files[1:]]},
                   'other sidecar bytes':{'files':[files[0],files[1] | {'sha256':'0'*64},*files[2:]]},
                   'other version':{'version':'0'*64}}
        for name,change in damaged.items():
            with self.subTest(name):
                receipt.write_text(json.dumps(value | change))
                self.assertIsNone(cloud._receipt_for(self.root, archive, 'google'))
                for history in (self.root / '.archive-state/cloud/history/google').glob('*/*.json'):
                    history.write_text(json.dumps(value | change))
                self.assertIsNone(cloud.offloaded(self.root,archive))
        receipt.write_text(json.dumps(value));self.assertTrue(cloud.offloaded(self.root,archive))
        self.assertIsNone(cloud.offloaded(self.root,self.root/'.archive-state'/'x.cbz'))
        self.assertIsNone(cloud.offloaded(self.root,self.root/'a'/'b'/'x.cbz'))

    def test_cleanup_that_cannot_prove_deletion_stays_pending_and_resumes(self):
        self.publish();self.configured();FakeTransfer.fail_delete = True
        with self.assertRaises(ArchiveError):cloud.smoke_test(self.root,'google',episodes=1)
        receipt = next((self.root/'.archive-state/cloud/tests').glob('*.json'));FakeTransfer.fail_delete = False
        with patch.object(FakeTransfer,'delete_test_file'):  # the provider says deleted, yet the listing still shows the file
            with self.assertRaises(CloudCleanupError):cloud.cleanup_test(self.root,receipt.stem)
        self.assertEqual(json.loads(receipt.read_text())['state'],'cleanup_pending');self.assertTrue(FakeTransfer.files)
        with patch.object(FakeTransfer,'delete_test_file',side_effect=NetworkPolicyHold('held')):
            with self.assertRaises(NetworkPolicyHold):cloud.cleanup_test(self.root,receipt.stem)
        value = json.loads(receipt.read_text());self.assertEqual(value['error_code'],'cloud_network_held')
        receipt.write_text(json.dumps(value | {'provider':'proton'}))
        with self.assertRaises(ArchiveError):cloud.cleanup_test(self.root,receipt.stem)
        self.assertTrue(FakeTransfer.files)
        receipt.write_text(json.dumps(value))
        self.assertTrue(cloud.cleanup_test(self.root,receipt.stem)['test_files_deleted'])
        self.assertFalse(FakeTransfer.files)

    def test_dashboard_cloud_actions_accept_only_a_provider_or_one_recorded_test_identity(self):
        identity = str(uuid.uuid4())
        cloud.validate_test_action('cloud_test',{'provider':'google'});cloud.validate_test_action('cloud_cleanup',{'test_id':identity})
        for operation,options in (('cloud_test',{'provider':'elsewhere'}),('cloud_test',{'provider':['google']}),
                                  ('cloud_test',{'provider':'google','path':'/tmp/x'}),('cloud_test',{}),
                                  ('cloud_cleanup',{'test_id':'../'+identity}),('cloud_cleanup',{'test_id':identity,'provider':'google'}),
                                  ('cloud_cleanup',{'test_id':7}),('cloud_delete',{'provider':'google'})):
            with self.subTest(operation=operation,options=options),self.assertRaises(ArchiveError):cloud.validate_test_action(operation,options)

    def test_offloading_removes_the_episodes_own_cached_pages_but_keeps_shared_and_other_pages(self):
        from comic_archive.archive import digest
        from comic_archive.storage import configure
        configure(self.root,purge_completed_cache=False)
        self.publish();control = self.publish_page('83',3)
        cache = self.root/'.archive-cache'
        self.assertTrue((cache/digest(png(3))).is_file())  # without cloud offload the completed pages stay cached
        self.ready()
        archive = self.publish_page('82',2)
        self.assertFalse(archive.exists())
        self.assertFalse((cache/digest(png(2))).exists())
        self.assertTrue((cache/digest(png(3))).is_file());self.assertTrue(control.exists())


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name)/'config';self.config.write_text('[google]\ntype=drive\n');self.config.chmod(0o600)
        self.target = {'config':str(self.config),'remote':'google'}

    def command(self, args, **kwargs):
        self.assertFalse(kwargs['check']);self.assertNotIn('PASSWORD_PRIVATE',' '.join(args))
        self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)
        kwargs['stdout'].write(b'google: drive\n')
        return subprocess.CompletedProcess(args,0)

    def test_provider_type_is_checked_and_secret_environment_overrides_are_removed(self):
        with patch('comic_archive.cloud.shutil.which',return_value='/fake/rclone'), patch('comic_archive.cloud.subprocess.run',side_effect=self.command) as run, patch.dict(os.environ,{'RCLONE_REMOTE_GOOGLE_TYPE':'local','RCLONE_LOG_LEVEL':'DEBUG','RCLONE_CONFIG_PASS':'PASSWORD_PRIVATE'}):
            cloud.Transfer('google',self.target)
            env = run.call_args.kwargs['env']
            self.assertNotIn('RCLONE_LOG_LEVEL',env);self.assertNotIn('RCLONE_REMOTE_GOOGLE_TYPE',env)
            self.assertEqual(env['RCLONE_CONFIG_PASS'],'PASSWORD_PRIVATE')
            with self.assertRaises(ArchiveError):cloud.Transfer('proton',self.target)
            with self.assertRaises(ArchiveError):cloud.Transfer('local',self.target)
        with patch('comic_archive.cloud.shutil.which',return_value=None),self.assertRaises(ArchiveError):cloud.Transfer('google',self.target)

    def test_failed_timeout_and_oversized_subprocess_output_are_sanitized(self):
        transfer = object.__new__(cloud.Transfer);transfer.binary='/fake/rclone';transfer.config=self.config
        for code in (1,5,9):
            with patch('comic_archive.cloud.subprocess.run',return_value=subprocess.CompletedProcess([],code)):
                with self.assertRaises(ArchiveError) as error:transfer.run('copyto','source','google:path')
                self.assertNotIn(str(self.config),str(error.exception))
        for exception in (OSError('TOKEN_PRIVATE'),subprocess.TimeoutExpired('TOKEN_PRIVATE',1)):
            with patch('comic_archive.cloud.subprocess.run',side_effect=exception),self.assertRaises(ArchiveError):transfer.run('listremotes')
        def oversized(args,**kwargs):
            kwargs['stdout'].write(b'x'*65537);return subprocess.CompletedProcess(args,0)
        with patch('comic_archive.cloud.subprocess.run',side_effect=oversized),self.assertRaises(ArchiveError):transfer.run('listremotes')
        for code in (3,4):
            with patch('comic_archive.cloud.subprocess.run',return_value=subprocess.CompletedProcess([],code)):
                self.assertEqual(transfer.run('lsf','google:test',missing_ok=True),b'')

    def test_uploaded_byte_verification_is_required_and_copy_never_syncs_or_deletes(self):
        transfer = object.__new__(cloud.Transfer);transfer.target={'remote':'google'};transfer.provider='google';transfer.full_readback=True
        transfer.run = Mock(side_effect=[b'',b'0'*64+b'  episode.cbz\n'])
        transfer.upload(Path('episode.cbz'),'Comic-Archive/test/episode.cbz','0'*64)
        self.assertEqual(transfer.run.call_args_list[0].args[0],'copyto')
        self.assertIn('--immutable',transfer.run.call_args_list[0].args)
        self.assertIn('--download',transfer.run.call_args_list[1].args)
        transfer.run = Mock(side_effect=[b'',b'1'*64])
        with self.assertRaises(ArchiveError):transfer.upload(Path('episode.cbz'),'test/episode.cbz','0'*64)
        transfer.run = Mock();transfer.delete_test_file('test/episode.cbz')
        transfer.run.assert_called_once_with('deletefile','google:test/episode.cbz')

    def test_local_connect_uses_interactive_auth_without_credentials_in_arguments_or_final_output(self):
        root = self.config.parent/'Library'
        with patch('comic_archive.cloud.shutil.which',return_value='/fake/rclone'), patch('comic_archive.cloud.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run, patch('comic_archive.cloud.configure_target',return_value={'connected':True}):
            self.assertEqual(cloud.connect(root,'proton'),{'connected':True})
            command = run.call_args.args[0]
            self.assertIn('--all',command);self.assertIn('--no-output',command);self.assertIn('protondrive',command)
            self.assertNotIn('stdout',run.call_args.kwargs)
            self.assertEqual((root/'.archive-state/cloud/rclone.conf').stat().st_mode&0o777,PRIVATE_FILE)
            run.return_value = subprocess.CompletedProcess([],1)
            with self.assertRaises(ArchiveError):cloud.connect(root,'proton')
            run.side_effect = OSError('secret')
            with self.assertRaises(ArchiveError):cloud.connect(root,'proton')
        with self.assertRaises(ArchiveError):cloud.connect(root,'local')


    def test_guarded_run_checks_the_network_policy_first_and_classifies_failures(self):
        folder = self.config.parent;marker = folder/'launched';code = folder/'code'
        script = helper_program(folder/'rclone-fixture', 'import sys\nfrom pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("x")\nsys.stderr.write("storageQuotaExceeded")\n'
            f'sys.stdout.write("listing")\nsys.exit(int(Path({str(code)!r}).read_text()))\n')
        transfer = object.__new__(cloud.Transfer);transfer.binary = str(script);transfer.config = self.config;transfer.guard = Mock()
        code.write_text('0')
        self.assertEqual(transfer.run('lsf','google:test'),b'listing')
        self.assertTrue(marker.exists());self.assertTrue(transfer.guard.check.called)
        code.write_text('7')
        with self.assertRaises(CloudTransferError) as caught:transfer.run('copyto','source','google:test')
        self.assertEqual(safe_code(caught.exception),'cloud_space')
        code.write_text('3')
        self.assertEqual(transfer.run('lsf','google:test',missing_ok=True),b'')
        with self.assertRaises(ArchiveError):transfer.run('lsf','google:test')
        marker.unlink();transfer.guard.check.side_effect = NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold):transfer.run('lsf','google:test')
        self.assertFalse(marker.exists())

    def test_google_hash_listing_failure_falls_back_to_a_download_but_a_network_hold_does_not(self):
        transfer = object.__new__(cloud.Transfer);transfer.provider = 'google';transfer.target = {'remote':'google'}
        expected = 'a'*64
        for first in (ArchiveError('listing unavailable'),b'UNSUPPORTED  episode.cbz'):
            transfer.run = Mock(side_effect=[first,expected.encode()])
            transfer.verify('own/episode.cbz',expected)
            self.assertIn('--download',transfer.run.call_args.args)
        transfer.run = Mock(side_effect=NetworkPolicyHold('held'))
        with self.assertRaises(NetworkPolicyHold):transfer.verify('own/episode.cbz',expected)
        self.assertEqual(transfer.run.call_count,1)

    def test_local_connect_refuses_a_symlinked_config_without_starting_sign_in(self):
        root = self.config.parent/'Library';folder = root/'.archive-state/cloud';folder.mkdir(parents=True)
        outside = self.config.parent/'outside.conf'  # a dangling link: touching it would create a file outside the library
        (folder/'rclone.conf').symlink_to(outside)
        with patch('comic_archive.cloud.shutil.which',return_value='/fake/rclone'), patch('comic_archive.cloud.subprocess.run') as run:
            with self.assertRaises(ArchiveError):cloud.connect(root,'proton')
        run.assert_not_called();self.assertFalse(outside.exists())
