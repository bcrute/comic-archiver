import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive.archive import verify
from comic_archive.cancellation import request_stop, RunCancellation, RunCancelled
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered, sync
from comic_archive.models import ArchiveError
from comic_archive.monitoring import library_status
from test_jobs import AccountAdapter


class CancellationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'Library'
        self.adapter=AccountAdapter()
        # Keep the cancellation checkpoint on episode 81, now a paid unlock.
        self.adapter.access.update({'81':'tapas_unlocked','82':'tapas_free'})
        with Inventory(self.root) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'First','order':1,'access_state':'tapas_unlocked'},
                {'id':'82','title':'Second','order':2,'access_state':'tapas_free'}],'subscribed')

    def start(self,operation):
        results=[]
        def run():
            try:results.append(operation())
            except BaseException as error:results.append(error)
        thread=threading.Thread(target=run);thread.start()
        self.addCleanup(lambda:thread.join(timeout=5))
        return thread,results

    def stop(self):
        status=library_status(self.root)
        run=status['last_run']['id']
        self.assertTrue(status['progress']['cancellation_supported'])
        self.assertEqual(request_stop(self.root,run),{'run_id':run,'status':'cancellation_requested'})
        self.assertTrue(library_status(self.root)['progress']['cancellation_requested'])
        self.assertEqual(request_stop(self.root,run)['status'],'cancellation_requested')
        return run

    @patch('builtins.print')
    def test_stop_after_first_page_keeps_cache_and_resume_downloads_only_missing_page(self,output):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        retrieve=self.adapter.retrieve
        def waiting(page,max_bytes=None):
            data=retrieve(page,max_bytes)
            entered.set()
            if not release.wait(5):raise RuntimeError('Test checkpoint timed out')
            return data
        self.adapter.retrieve=waiting
        thread,results=self.start(lambda:archive_discovered(self.adapter,self.root,limit=1))
        self.assertTrue(entered.wait(5));self.stop();release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertIsInstance(results[0],dict)
        self.assertEqual(results[0]['status'],'cancelled')
        self.assertEqual(results[0]['downloaded_pages'],1)
        self.assertEqual(list(self.root.glob('*/*.cbz')),[])
        self.assertIsNone(self.adapter.cancel_check)
        with Inventory(self.root,read_only=True) as inventory:
            row=inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='81'").fetchone()
            self.assertEqual(tuple(row),('queued','cancelled'))
        self.adapter.retrieve=retrieve
        resumed=archive_discovered(self.adapter,self.root,limit=1)
        self.assertEqual(resumed['archived_episode_ids'],['81'])
        self.assertEqual(resumed['downloaded_pages'],1)
        self.assertEqual(self.adapter.downloads,[1,2])
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_completed_episode_remains_verified_when_next_episode_is_stopped(self,output):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        capture=self.adapter.capture_automatic
        def waiting(url):
            if url.endswith('/82'):
                entered.set()
                if not release.wait(5):raise RuntimeError('Test checkpoint timed out')
            return capture(url)
        self.adapter.capture_automatic=waiting
        thread,results=self.start(lambda:archive_discovered(self.adapter,self.root,limit=2))
        self.assertTrue(entered.wait(5));self.stop();release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(results[0]['status'],'cancelled')
        self.assertEqual(results[0]['archived_episode_ids'],['81'])
        self.assertEqual(self.adapter.downloads,[1,2])
        self.assertEqual(verify(self.root),[])
        self.assertEqual(library_status(self.root)['episodes_by_status'],{'archived':1,'queued':1})

    @patch('builtins.print')
    def test_metadata_stop_retains_observations_without_opening_a_reader(self,output):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        inspect=self.adapter.inspect_series
        def waiting(key,strict_order=False):
            entered.set()
            if not release.wait(5):raise RuntimeError('Test checkpoint timed out')
            return inspect(key,strict_order)
        self.adapter.inspect_series=waiting
        thread,results=self.start(lambda:sync(self.adapter,self.root,refresh_lists=False,scan_limit=1))
        self.assertTrue(entered.wait(5));self.stop();release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(results[0]['status'],'cancelled')
        self.assertEqual(self.adapter.downloads,[])
        self.assertFalse(any(isinstance(call,tuple) and call[0]=='capture' for call in self.adapter.calls))
        self.assertEqual(library_status(self.root)['series_without_scan_checkpoint_count'],0)

    def test_stale_finished_invalid_or_older_code_runs_cannot_be_stopped(self):
        with Inventory(self.root) as inventory:run=inventory.start_run()
        for key in ('../private','/tmp/private','f'*24,None,run):
            with self.assertRaises(ArchiveError):request_stop(self.root,key)
        progress=self.root/'.archive-state/progress.json'
        progress.write_text(json.dumps({'run_id':run,'status':'running','cancellation_supported':True}))
        with Inventory(self.root) as inventory:
            inventory.finish_run(run,'limited',{})
        self.assertEqual(request_stop(self.root,run),{'run_id':run,'status':'limited','stop_requested':False})
        self.assertFalse((self.root/'.archive-state/cancellations').exists())

    def test_unsafe_stop_marker_never_reads_or_overwrites_outside_files(self):
        with Inventory(self.root) as inventory:run=inventory.start_run()
        (self.root/'.archive-state/progress.json').write_text(json.dumps({
            'run_id':run,'status':'running','cancellation_supported':True}))
        marker=RunCancellation(self.root,run)
        marker.path.parent.mkdir()
        outside=self.root.parent/'private';outside.write_text('PRIVATE')
        marker.path.symlink_to(outside)
        with self.assertRaises(ArchiveError):marker.check()
        with self.assertRaises(ArchiveError):request_stop(self.root,run)
        self.assertEqual(outside.read_text(),'PRIVATE')


    def test_unreadable_stop_marker_is_reported_as_invalid_not_as_a_stop(self):
        marker=RunCancellation(self.root,'a'*24);marker.path.parent.mkdir()
        self.assertFalse(marker.requested())
        marker.path.mkdir()
        contents={'oversized':'x'*2000,'not an object':'[]','broken':'{broken',
                  'another run':json.dumps({'document_type':'comic_import_stop','run_id':'b'*24}),
                  'another kind':json.dumps({'document_type':'queued_comic_import_stop','run_id':'a'*24})}
        for name,content in [('directory',None)]+list(contents.items()):
            with self.subTest(name):
                if content is not None:
                    if marker.path.is_dir():marker.path.rmdir()
                    marker.path.write_text(content)
                with self.assertRaises(ArchiveError) as caught:marker.check()
                self.assertNotIsInstance(caught.exception,RunCancelled)
                self.assertIn('invalid',str(caught.exception))


if __name__=='__main__':unittest.main()
