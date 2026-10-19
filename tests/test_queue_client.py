import json
import tempfile
import unittest
from pathlib import Path
from support import PRIVATE_FILE
from comic_archive.models import ArchiveError
from comic_archive.queue_client import submit_job, submit_inspection, task_status


class QueueClientTests(unittest.TestCase):
    def test_cloud_tasks_reject_paths_credentials_and_unbounded_tests_before_queueing(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True)
            for operation,options in [('cloud_test',{'provider':'b2','episodes':100}),('cloud_test',{'provider':[]}),
                    ('cloud_test',{'provider':'b2','password':'PRIVATE'}),('cloud_cleanup',{'test_id':'../outside'})]:
                with self.assertRaises(ArchiveError):submit_inspection(root,operation,options)
            self.assertEqual(list((root/'worker/requests').iterdir()),[])
            for operation,options in [('cloud_test',{'provider':'b2'}),('cloud_cleanup',{'test_id':'f257c685-2a39-4e16-8dc0-353338e2f7d7'})]:
                task=submit_inspection(root,operation,options)['task_id'];path=root/'worker/requests'/f'{task}.json'
                self.assertEqual(json.loads(path.read_text()),{'operation':operation,'args':options})
                self.assertEqual(path.stat().st_mode & 0o777,PRIVATE_FILE)

    def test_free_view_is_explicit_sync_scope_not_a_purchase_operation_or_default(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True)
            task=submit_job(root,'sync',{'categories':['free_episodes'],'free_only':True,'scan_limit':1})['task_id']
            value=json.loads((root/'worker/requests'/f'{task}.json').read_text())
            self.assertEqual(value['args']['categories'],['free_episodes']);self.assertTrue(value['args']['free_only'])
            default=submit_job(root,'sync')['task_id']
            self.assertNotIn('categories',json.loads((root/'worker/requests'/f'{default}.json').read_text())['args'])
            with self.assertRaises(ArchiveError):submit_job(root,'purchase',{'categories':['free_episodes']})

    def test_go_can_queue_each_sites_own_lists(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True)
            for categories in (['unlocked'],['subscribed','recent']):
                task=submit_job(root,'sync',{'categories':categories})['task_id']
                self.assertEqual(json.loads((root/'worker/requests'/f'{task}.json').read_text())['args']['categories'],categories)

    def test_watched_only_scope_cannot_include_account_lists_or_history(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True)
            for options in ({'refresh_lists':None},{'refresh_lists':False,'categories':['recent']},
                            {'refresh_lists':False,'include_history':True}):
                with self.assertRaises(ArchiveError):submit_job(root,'sync',options)
            task=submit_job(root,'sync',{'refresh_lists':False,'scan_limit':1})['task_id']
            self.assertEqual(json.loads((root/'worker/requests'/f'{task}.json').read_text())['args'],{'refresh_lists':False,'scan_limit':1})

    def test_reserved_scheduler_job_is_idempotent_and_cannot_replace_another_request(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True);(root/'worker/responses').mkdir()
            task='job-'+'a'*24
            first=submit_job(root,'sync',{'free_only':True,'limit':1},_task_id=task)
            self.assertEqual(submit_job(root,'sync',{'free_only':True,'limit':1},_task_id=task),first)
            with self.assertRaises(ArchiveError):submit_job(root,'sync',{'free_only':True,'limit':2},_task_id=task)
            with self.assertRaises(ArchiveError):submit_job(root,'sync',{},_task_id='../outside')
            (root/'worker/responses'/(task+'.json')).write_text(json.dumps({'task_id':task,'status':'ok'}))
            (root/'worker/requests'/(task+'.json')).unlink()
            self.assertEqual(submit_job(root,'sync',{'free_only':True,'limit':1},_task_id=task)['status'],'ok')
            self.assertEqual(list((root/'worker/requests').glob('*.json')),[])

    def test_jobs_are_fixed_and_status_tracks_queue_and_response(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / 'worker/requests').mkdir(parents=True)
            (root / 'worker/responses').mkdir()
            result = submit_job(root, 'sync', {'free_only':True,'max_pages':1})
            task_id = result['task_id']
            self.assertEqual(task_status(root,task_id)['status'],'queued')
            (root / 'worker/status.json').write_text(json.dumps({'state':'busy','task_id':task_id}))
            self.assertEqual(task_status(root,task_id)['status'],'running')
            request = root / 'worker/requests' / (task_id + '.json')
            self.assertEqual(request.stat().st_mode & 0o777,PRIVATE_FILE)
            self.assertEqual(json.loads(request.read_text()),{'operation':'sync','args':{'free_only':True,'max_pages':1}})
            (root / 'worker/responses' / (task_id+'.json')).write_text(json.dumps({'task_id':task_id,'status':'ok','result':{'downloaded_pages':1}}))
            self.assertEqual(task_status(root,task_id)['status'],'ok')

    def test_rejects_unsafe_operations_paths_and_options(self):
        with tempfile.TemporaryDirectory() as work:
            for operation,options in [('purchase',{}),('sync',{'output':'/tmp'}),
                    ('sync',{'max_seconds':float('nan')}),('sync',{'limit':True}),
                    ('sync',{'categories':['purchased']}),('sync',{'free_only':'yes'})]:
                with self.assertRaises(ArchiveError):
                    submit_job(work,operation,options)
            with self.assertRaises(ArchiveError):
                task_status(work,'../outside')

    def test_only_fixed_cloud_inspections_are_accepted_and_history_cannot_resume_a_cursor(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root/'worker/requests').mkdir(parents=True)
            for operation,options in [('purchase',{}),('inspect_transaction_history',{'max_pages':3}),
                    ('check_session',{}),('cloud_upload',{'provider':'b2'})]:
                with self.assertRaises(ArchiveError):
                    submit_inspection(root,operation,options)
            with self.assertRaises(ArchiveError):
                submit_job(root,'sync',{'history_resume':True})
            self.assertEqual(list((root/'worker/requests').iterdir()),[])
            task=submit_inspection(root,'cloud_upload')['task_id']
            path=root/'worker/requests'/f'{task}.json'
            self.assertEqual(json.loads(path.read_text()),{'operation':'cloud_upload','args':{}})
            self.assertEqual(path.stat().st_mode & 0o777,PRIVATE_FILE)

    def test_audit_jobs_accept_no_options_and_retired_jobs_are_refused(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);requests=root/'worker/requests';requests.mkdir(parents=True)
            with self.assertRaises(ArchiveError):submit_job(root,'audit_account',{'output':'/tmp/outside'})
            for operation in ('enrich_series','archive_watched_series','archive_series_url'):
                with self.subTest(operation=operation),self.assertRaises(ArchiveError):submit_job(root,operation,{'id':'42'})
            self.assertEqual(list(requests.iterdir()),[])
            task=submit_job(root,'audit_account')['task_id']
            self.assertEqual(json.loads((requests/f'{task}.json').read_text()),{'operation':'audit_account','args':{}})

    def test_reader_recovery_probe_needs_one_numeric_episode_and_limit_one(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);requests=root/'worker/requests';requests.mkdir(parents=True)
            for options in ({'retry_episode_id':'0','limit':1},{'retry_episode_id':81,'limit':1},
                    {'retry_episode_id':'../81','limit':1},{'retry_episode_id':'81'},{'retry_episode_id':'81','limit':2}):
                with self.subTest(options=options),self.assertRaises(ArchiveError):submit_job(root,'archive_discovered',options)
            with self.assertRaises(ArchiveError):submit_job(root,'sync',{'retry_episode_id':'81','limit':1})
            self.assertEqual(list(requests.iterdir()),[])
            task=submit_job(root,'archive_discovered',{'retry_episode_id':'81','limit':1})['task_id']
            self.assertEqual(json.loads((requests/f'{task}.json').read_text())['args'],{'retry_episode_id':'81','limit':1})

    def test_sync_fast_metadata_needs_scan_only_and_history_pages_are_bounded(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);requests=root/'worker/requests';requests.mkdir(parents=True)
            for options in ({'metadata_speed':'fast'},{'metadata_speed':'fast','scan_only':False},{'metadata_speed':'warp'},
                    {'include_history':True,'history_pages':0},{'include_history':True,'history_pages':501},
                    {'include_history':True,'history_pages':True},{'include_history':True,'history_pages':'5'}):
                with self.subTest(options=options),self.assertRaises(ArchiveError):submit_job(root,'sync',options)
            self.assertEqual(list(requests.iterdir()),[])
            for options in ({'metadata_speed':'fast','scan_only':True},{'include_history':True,'history_pages':500}):
                task=submit_job(root,'sync',options)['task_id']
                self.assertEqual(json.loads((requests/f'{task}.json').read_text())['args'],options)

    def test_jobs_are_refused_while_no_worker_has_prepared_the_queue(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)
            with self.assertRaisesRegex(ArchiveError,'Start the downloader'):submit_job(root,'sync')
            with self.assertRaisesRegex(ArchiveError,'Start the downloader'):submit_inspection(root,'cloud_upload')
            self.assertEqual(list(root.iterdir()),[])

    def test_reserved_job_never_replaces_or_trusts_an_unsafe_or_damaged_queued_request(self):
        task='job-'+'b'*24
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);requests=root/'worker/requests';requests.mkdir(parents=True);(root/'worker/responses').mkdir()
            path=requests/f'{task}.json';outside=root/'outside.json'
            wanted={'operation':'sync','args':{'limit':1}}
            outside.write_text(json.dumps(wanted))
            for name in ('symlink','oversized','damaged'):
                with self.subTest(name):
                    path.unlink(missing_ok=True)
                    if name=='symlink':path.symlink_to(outside)
                    # The oversized request is valid and matching, so only its size can refuse it.
                    else:path.write_text(json.dumps(wanted)+' '*16385 if name=='oversized' else '{broken')
                    before=path.read_bytes()
                    with self.assertRaisesRegex(ArchiveError,'invalid' if name=='damaged' else 'unsafe'):
                        submit_job(root,'sync',{'limit':1},_task_id=task)
                    self.assertEqual(path.read_bytes(),before)
                    self.assertEqual(path.is_symlink(),name=='symlink')
            self.assertEqual(json.loads(outside.read_text()),wanted)

    def test_unsafe_or_invalid_worker_responses_are_refused_not_returned(self):
        task='job-'+'c'*24
        valid={'task_id':task,'status':'ok','result':{'downloaded_pages':1}}
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);responses=root/'worker/responses';responses.mkdir(parents=True)
            (root/'worker/requests').mkdir()
            response=responses/f'{task}.json';outside=root/'outside.json'
            outside.write_text(json.dumps(valid))
            response.symlink_to(outside)
            with self.assertRaisesRegex(ArchiveError,'unsafe'):task_status(root,task)
            response.unlink()
            response.write_bytes(b' '*(2*1024*1024+1))
            with self.assertRaisesRegex(ArchiveError,'unsafe'):task_status(root,task)
            for text in ('not json','[]',json.dumps(valid|{'task_id':'job-'+'d'*24}),json.dumps(valid|{'status':'weird'})):
                with self.subTest(text=text):
                    response.write_text(text)
                    with self.assertRaisesRegex(ArchiveError,'invalid schema'):task_status(root,task)
            response.unlink()
            moved=root/'worker/moved';responses.rename(moved)
            responses.symlink_to(moved)
            (moved/f'{task}.json').write_text(json.dumps(valid))
            with self.assertRaisesRegex(ArchiveError,'unsafe'):task_status(root,task)

    def test_a_queued_job_stays_queued_when_the_worker_status_is_unreadable(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'worker/requests').mkdir(parents=True);(root/'worker/responses').mkdir()
            task=submit_job(root,'sync',{'limit':1})['task_id']
            status=root/'worker/status.json'
            outside=root/'outside.json'
            outside.write_text(json.dumps({'state':'busy','task_id':task}))
            for name,text in (('broken','{broken'),('not an object','[]'),('other task',json.dumps({'state':'busy','task_id':'job-'+'e'*24}))):
                with self.subTest(name):
                    status.write_text(text)
                    self.assertEqual(task_status(root,task)['status'],'queued')
            status.unlink();status.symlink_to(outside)
            self.assertEqual(task_status(root,task)['status'],'queued')
            with self.assertRaises(ArchiveError):task_status(root,'job-'+'f'*24)
