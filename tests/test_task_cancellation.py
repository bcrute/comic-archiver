import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from support import PRIVATE_FILE
from comic_archive.archive import atomic_json
from comic_archive.errors import LibraryBusy
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_job
from comic_archive.task_cancellation import (admission_lock, cancelled_result, end_unstarted,
    queued_stop_requested, request_running_stop, started_stop_requested)


class QueuedStopTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.project=Path(temporary.name);self.session=self.project/'.comic-session'
        self.root=self.session/'worker';(self.root/'requests').mkdir(parents=True)
        (self.root/'responses').mkdir()
        atomic_json(self.root/'status.json',{'state':'ready','worker_version':35})
        self.task=submit_job(self.session,'sync',{'max_pages':1})['task_id']

    def test_admission_and_stop_cannot_cross_the_same_lock(self):
        held=threading.Event();release=threading.Event()
        def admit():
            with admission_lock(self.session):
                held.set();release.wait(5)
                atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task})
        worker=threading.Thread(target=admit);worker.start()
        try:
            self.assertTrue(held.wait(3))
            with self.assertRaises(LibraryBusy):request_running_stop(self.session,self.task)
            self.assertFalse((self.root/'cancellations').exists())
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(request_running_stop(self.session,self.task)['status'],'cancellation_requested')

    def test_bad_id_or_unsafe_marker_is_refused_and_never_followed(self):
        for task in ('../PRIVATE','job-'+('a'*23),'PRIVATE'):
            with self.assertRaises(ArchiveError):request_running_stop(self.session,task)
            with self.assertRaises(ArchiveError):queued_stop_requested(self.session,task)
        marker=self.root/'cancellations'/f'{self.task}.json';marker.parent.mkdir()
        outside=self.project/'outside';outside.write_text('PRIVATE');marker.symlink_to(outside)
        with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)
        self.assertEqual(outside.read_text(),'PRIVATE');marker.unlink()
        marker.write_text(json.dumps({'document_type':'queued_comic_import_stop','task_id':'wrong'}))
        with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)
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

    def test_damaged_stop_marker_is_refused_instead_of_being_read_as_a_stop_or_no_stop(self):
        marker=self.root/'cancellations'/f'{self.task}.json';marker.parent.mkdir()
        self.assertFalse(queued_stop_requested(self.session,self.task))
        for name,content in (('oversized','x'*2000),('not an object','[]'),('broken','{broken')):
            with self.subTest(name):
                marker.write_text(content)
                with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)
                self.assertEqual(marker.read_text(),content)
                marker.unlink()
        marker.mkdir()
        with self.assertRaises(ArchiveError):queued_stop_requested(self.session,self.task)

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

    def test_a_stop_left_with_a_running_task_is_read_only_by_the_worker_process_running_it(self):
        library=self.project/'Library';marker=self.root/'cancellations'/f'{self.task}.json'
        self.assertEqual(request_running_stop(self.session,self.task)['status'],'not_running')  # Still queued.
        self.assertFalse(marker.exists());self.assertFalse(started_stop_requested(library))
        atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task,'pid':os.getpid()})
        self.assertFalse(started_stop_requested(library))
        result=request_running_stop(self.session,self.task)
        self.assertEqual(result,{'task_id':self.task,'status':'cancellation_requested'})
        self.assertEqual(request_running_stop(self.session,self.task),result)
        self.assertEqual(marker.stat().st_mode&0o777,PRIVATE_FILE)
        self.assertTrue(started_stop_requested(library))
        atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task,'pid':os.getpid()+1})
        self.assertFalse(started_stop_requested(library))  # Another process's task.
        atomic_json(self.root/'responses'/f'{self.task}.json',{'task_id':self.task,'status':'ok','result':{}})
        self.assertEqual(request_running_stop(self.session,self.task)['status'],'not_running')
        self.assertFalse(started_stop_requested(self.project/'elsewhere'/'Library'))  # No worker there.

    def test_a_task_its_worker_was_running_when_it_stopped_reports_its_use_as_unknown(self):
        library=self.project/'Library';receipt=self.root/'responses'/f'{self.task}.json'
        atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':self.task,'pid':1})
        self.assertTrue(end_unstarted(self.session,library,self.task))
        result=json.loads(receipt.read_bytes())['result']
        self.assertEqual((result['status'],result['cancelled_before_start'],result['usage_unreported']),('cancelled',False,True))
        self.assertEqual(result['downloaded_pages'],0)
        # A check of what is saved has no allowance to count.
        check=submit_job(self.session,'audit_account',{})['task_id']
        atomic_json(self.root/'status.json',{'state':'busy','worker_version':35,'task_id':check,'pid':1})
        self.assertTrue(end_unstarted(self.session,library,check))
        self.assertEqual(json.loads((self.root/'responses'/f'{check}.json').read_bytes())['result'],
                         {'status':'cancelled','cancelled_before_start':True})
