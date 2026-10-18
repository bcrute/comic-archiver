import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from comic_archive.archive import Store
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from test_jobs import AccountAdapter


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library'
        self.adapter=AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'subscribed')
        with patch('builtins.print'), patch('comic_archive.jobs.Store',Store):archive_discovered(self.adapter,self.root,limit=1)
        self.archive=next(self.root.glob('*/*.cbz'))

    def test_private_archive_shaped_files_cannot_suppress_a_queued_episode(self):
        private=self.root/'.private'
        shutil.copytree(self.archive.parent,private)
        self.archive.unlink();self.archive.with_suffix('.json').unlink()
        with Inventory(self.root) as inventory:
            inventory.reconcile()
            self.assertEqual([row['id'] for row in inventory.pending()],['81'])
            row=inventory.db.execute("SELECT status FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(row['status'],'queued')

    def test_pinned_hidden_outside_and_traversal_paths_are_requeued_without_reading_them(self):
        for relative in ('/tmp/PRIVATE.cbz','../PRIVATE.cbz','.private/x.cbz'):
            with Inventory(self.root) as inventory:
                inventory.db.execute("UPDATE episodes SET status='archived',archive_path=? WHERE id='81'",(relative,))
                inventory.db.commit()
                # Hide the valid public archive temporarily, so automatic
                # adoption cannot obscure the invalid-path recovery result.
                temporary=self.archive.with_suffix('.held')
                self.archive.rename(temporary)
                try:
                    inventory.reconcile()
                    row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
                    self.assertEqual(tuple(row),('queued','archive_path_invalid'))
                finally:temporary.rename(self.archive)

    def test_valid_page_hashes_with_wrong_episode_identity_do_not_preserve_archived_status(self):
        sidecar=self.archive.with_suffix('.json')
        metadata=json.loads(sidecar.read_text());metadata['source_episode_id']='999'
        sidecar.write_text(json.dumps(metadata))
        with Inventory(self.root) as inventory:
            inventory.reconcile()
            row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(tuple(row),('queued','archive_identity_mismatch'))

    def test_non_object_metadata_is_requeued_without_crashing_the_resume_scan(self):
        self.archive.with_suffix('.json').write_text('[]')
        with Inventory(self.root) as inventory:
            inventory.reconcile()
            row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(tuple(row),('queued','archive_corrupt'))

    def test_an_unknown_image_quality_in_a_sidecar_requeues_the_episode_instead_of_failing_the_scan(self):
        # A neutral sidecar, whose image quality is checked before its private record is read.
        self.archive.with_suffix('.json').write_text(json.dumps({'schema_version':2,'export_id':'0'*8+'-0000'*3+'-'+'0'*12,
            'title':'E','format':'cbz','content_type':'comic','image_profile':'PRIVATE','images_recompressed':False}))
        with Inventory(self.root) as inventory:
            inventory.reconcile();inventory.reconcile()  # The archived row, then the folder scan alone.
            row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(tuple(row),('queued','archive_corrupt'))

    def test_numeric_series_id_in_older_sidecars_can_be_reconciled_without_redownloading(self):
        sidecar=self.archive.with_suffix('.json')
        metadata=json.loads(sidecar.read_text());metadata.pop('source_series_id');metadata['series_id']='42'
        sidecar.write_text(json.dumps(metadata))
        with Inventory(self.root) as inventory:
            inventory.mark('81','queued')
            inventory.reconcile()
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1})
        with patch('builtins.print'):
            result=archive_discovered(self.adapter,self.root)
        self.assertEqual(result['archived_episode_ids'],[])


    def test_symlinked_archives_are_never_adopted_or_kept_as_saved(self):
        outside=self.root.parent/'outside';outside.mkdir()
        sidecar=self.archive.with_suffix('.json')
        moved,moved_sidecar=outside/self.archive.name,outside/sidecar.name
        shutil.move(self.archive,moved);shutil.move(sidecar,moved_sidecar)
        self.archive.symlink_to(moved);sidecar.symlink_to(moved_sidecar)
        for _ in range(2):
            with Inventory(self.root) as inventory:
                inventory.reconcile()
                row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
                self.assertEqual(tuple(row),('queued','archive_path_invalid'))
                self.assertEqual([r['id'] for r in inventory.pending()],['81'])
        self.assertTrue(moved.is_file());self.assertTrue(moved_sidecar.is_file())


    def test_evicted_archive_stays_saved_only_with_a_matching_cloud_receipt_and_sidecar(self):
        sidecar=self.archive.with_suffix('.json');good=sidecar.read_text()
        wrong=json.dumps(json.loads(good)|{'source_episode_id':'999'})
        receipt={'eviction_requested':True}
        cases=[('receipt and sidecar match',receipt,good,('archived',None)),
               ('no receipt',None,good,('queued','archive_missing')),
               ('sidecar of another episode',receipt,wrong,('queued','archive_missing')),
               ('sidecar lost',receipt,None,('queued','archive_missing'))]
        for label,found,text,expected in cases:
            self.archive.unlink(missing_ok=True)
            if text is None:sidecar.unlink(missing_ok=True)
            else:sidecar.write_text(text)
            with self.subTest(label),patch('comic_archive.cloud.offloaded',return_value=found),Inventory(self.root) as inventory:
                inventory.db.execute("UPDATE episodes SET status='archived',error_code=NULL WHERE id='81'");inventory.db.commit()
                inventory.reconcile()
                row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
                self.assertEqual(tuple(row),expected)


    def test_archived_row_without_a_stored_archive_location_is_requeued_instead_of_trusted(self):
        with Inventory(self.root) as inventory:
            inventory.db.execute("UPDATE episodes SET status='archived',archive_path=NULL,error_code=NULL WHERE id='81'")
            inventory.db.commit()
            # The public archive is still present, but a row with no pinned location cannot vouch for it.
            temporary=self.archive.with_suffix('.held');self.archive.rename(temporary)
            try:
                inventory.reconcile()
                row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
                self.assertEqual(tuple(row),('queued','archive_path_invalid'))
            finally:temporary.rename(self.archive)


if __name__=='__main__':unittest.main()
