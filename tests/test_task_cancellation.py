import json
from pathlib import Path
import tempfile
import threading
import unittest

from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive.archive import atomic_json
from comic_archive.errors import LibraryBusy
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_job
from comic_archive.task_cancellation import (admission_lock, can_stop_queued,
    cancelled_result, end_unstarted, queued_stop_requested, request_queued_stop)


class QueuedStopTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.project=Path(temporary.name);self.session=self.project/'.comic-session'
        self.root=self.session/'worker';(self.root/'requests').mkdir(parents=True)
        (self.root/'responses').mkdir()
        atomic_json(self.root/'status.json',{'state':'ready','worker_version':35})
        self.task=submit_job(self.session,'sync',{'max_pages':1})['task_id']

    def test_stop_is_durable_idempotent_and_preserves_the_request(self):
        request=self.root/'requests'/f'{self.task}.json';before=request.read_bytes()
        self.assertTrue(can_stop_queued(self.session,self.task))
        result=request_queued_stop(self.session,self.task)
        self.assertEqual(result,{'task_id':self.task,'status':'cancellation_requested'})
        self.assertEqual(request_queued_stop(self.session,self.task),result)
        self.assertEqual(request.read_bytes(),before)
        self.assertTrue(queued_stop_requested(self.session,self.task))
        marker=self.root/'cancellations'/f'{self.task}.json'
        self.assertEqual(marker.stat().st_mode & 0o777,PRIVATE_FILE)
        self.assertEqual(marker.parent.stat().st_mode & 0o777,PRIVATE_FOLDER)

    def test_unsupported_workers_and_non_import_tasks_do_not_get_markers(self):
        for version in (34,None,True,'35'):
            atomic_json(self.root/'status.json',{'state':'ready','worker_version':version})
            self.assertFalse(can_stop_queued(self.session,self.task))
            with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
        atomic_json(self.root/'status.json',{'state':'ready','worker_version':35})
        for operation in ('configure_daily','check_session','purchase',['sync']):
            atomic_json(self.root/'requests'/f'{self.task}.json',{'operation':operation,'args':{}})
            self.assertFalse(can_stop_queued(self.session,self.task))
            with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
        self.assertFalse((self.root/'cancellations').exists())

    def test_admitted_or_finished_task_is_not_replaced_or_stopped(self):
        atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task})
        self.assertFalse(can_stop_queued(self.session,self.task))
        self.assertEqual(request_queued_stop(self.session,self.task)['status'],'already_started')
        atomic_json(self.root/'responses'/f'{self.task}.json',{'task_id':self.task,'status':'ok','result':{}})
        self.assertEqual(request_queued_stop(self.session,self.task)['status'],'already_finished')
        self.assertFalse((self.root/'cancellations').exists())

    def test_admission_and_stop_cannot_cross_the_same_lock(self):
        held=threading.Event();release=threading.Event()
        def admit():
            with admission_lock(self.session):
                held.set();release.wait(5)
                atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task})
        worker=threading.Thread(target=admit);worker.start()
        try:
            self.assertTrue(held.wait(3))
            with self.assertRaises(LibraryBusy):request_queued_stop(self.session,self.task)
            self.assertFalse((self.root/'cancellations').exists())
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(request_queued_stop(self.session,self.task)['status'],'already_started')

    def test_bad_id_or_unsafe_marker_never_removes_queued_work(self):
        for task in ('../PRIVATE','job-'+('a'*23),'PRIVATE'):
            with self.assertRaises(ArchiveError):request_queued_stop(self.session,task)
        marker=self.root/'cancellations'/f'{self.task}.json';marker.parent.mkdir()
        outside=self.project/'outside';outside.write_text('PRIVATE');marker.symlink_to(outside)
        with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
        self.assertEqual(outside.read_text(),'PRIVATE');marker.unlink()
        marker.write_text(json.dumps({'document_type':'queued_comic_import_stop','task_id':'wrong'}))
        with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
        self.assertTrue((self.root/'requests'/f'{self.task}.json').is_file())

    def test_cancelled_receipt_reads_inventory_without_creating_a_run(self):
        library=self.project/'Library'
        with Inventory(library) as inventory:
            inventory.observe('42','S',[{'id':'81','title':'E','order':1,'access_state':'tapas_free'}],'fixture')
            inventory.scan_candidates()
        index=library/'.archive-state/inventory.sqlite3';before=index.read_bytes()
        result=cancelled_result(library,'sync')
        self.assertEqual(result['status'],'cancelled');self.assertTrue(result['cancelled_before_start'])
        self.assertEqual(result['downloaded_pages'],0);self.assertEqual(result['pending_episode_count'],1)
        self.assertEqual(result['inventory']['scan_remaining_series_count'],1)
        self.assertFalse(result['inventory_complete']);self.assertEqual(index.read_bytes(),before)
        with Inventory(library,read_only=True) as inventory:
            self.assertEqual(inventory.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],0)

    def test_symlinked_worker_state_or_request_blocks_a_stop_without_being_followed(self):
        status=self.root/'status.json';request=self.root/'requests'/f'{self.task}.json'
        outside=self.project/'outside.json'
        for name,path,content in (('status',status,{'state':'ready','worker_version':35}),
                                  ('request',request,{'operation':'sync','args':{}})):
            with self.subTest(name):
                original=path.read_bytes();outside.write_text(json.dumps(content))
                path.unlink();path.symlink_to(outside)
                self.assertFalse(can_stop_queued(self.session,self.task))
                with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
                self.assertFalse((self.root/'cancellations').exists())
                self.assertEqual(json.loads(outside.read_text()),content)
                path.unlink();path.write_bytes(original)
        self.assertTrue(can_stop_queued(self.session,self.task))

    def test_damaged_stop_marker_is_refused_instead_of_being_read_as_a_stop_or_no_stop(self):
        marker=self.root/'cancellations'/f'{self.task}.json';marker.parent.mkdir()
        self.assertFalse(queued_stop_requested(self.session,self.task))
        for name,content in (('oversized','x'*2000),('not an object','[]'),('broken','{broken')):
            with self.subTest(name):
                marker.write_text(content)
                with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)
                with self.assertRaises(ArchiveError):request_queued_stop(self.session,self.task)
                self.assertEqual(marker.read_text(),content)
                marker.unlink()
        marker.mkdir()
        with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)

    def test_a_task_that_already_has_a_response_can_no_longer_be_stopped(self):
        self.assertTrue(can_stop_queued(self.session,self.task))
        atomic_json(self.root/'responses'/f'{self.task}.json',{'task_id':self.task,'status':'ok','result':{}})
        self.assertFalse(can_stop_queued(self.session,self.task))

    def test_a_task_no_worker_got_to_is_given_the_receipt_of_a_stopped_one_and_other_tasks_are_left_alone(self):
        library=self.project/'Library';receipt=self.root/'responses'/f'{self.task}.json'
        self.assertTrue(end_unstarted(self.session,library,self.task))
        value=json.loads(receipt.read_bytes())
        self.assertEqual((value['task_id'],value['status'],value['result']),(self.task,'ok',cancelled_result(library,'sync')))
        self.assertEqual(receipt.stat().st_mode&0o777,PRIVATE_FILE)
        atomic_json(receipt,{'task_id':self.task,'status':'ok','result':{'status':'finished'}})
        self.assertFalse(end_unstarted(self.session,library,self.task))  # It has ended; its receipt stays.
        self.assertEqual(json.loads(receipt.read_bytes())['result'],{'status':'finished'})
        self.assertFalse(end_unstarted(self.session,library,'job-'+'0'*24))  # Reserved and never queued.
        self.assertEqual(len(list((self.root/'responses').iterdir())),1)
        # A check of what is saved has nothing to count.
        check=submit_job(self.session,'audit_account',{})['task_id']
        self.assertTrue(end_unstarted(self.session,library,check))
        self.assertEqual(json.loads((self.root/'responses'/f'{check}.json').read_bytes())['result'],
                         {'status':'cancelled','cancelled_before_start':True})
        with self.assertRaises(ArchiveError):end_unstarted(self.session,library,'../x')
        self.assertFalse(library.exists())

    def test_only_import_operations_get_a_cancelled_receipt(self):
        library=self.project/'Library'
        for operation in ('check_session','configure_daily','purchase',None):
            with self.subTest(operation=operation),self.assertRaises(ArchiveError):cancelled_result(library,operation)
        self.assertFalse(library.exists())
