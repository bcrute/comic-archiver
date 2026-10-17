import json
import tempfile
import unittest
from pathlib import Path

from comic_archive.inventory import Inventory
from comic_archive.monitoring import library_status


class MonitoringTests(unittest.TestCase):
    def test_cooling_summary_exposes_only_bounded_wait_and_retains_private_fields(self):
        from comic_archive.monitoring import _progress
        for invalid in (True,0,-1,301,'PRIVATE'):
            self.assertEqual(_progress({'status':'limited','asset_retry_after_seconds':invalid}),{'status':'limited'})
        self.assertEqual(_progress({'status':'paused','asset_retry_after_seconds':300}),{'status':'paused'})
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)/'Library'
            with Inventory(root) as inventory:
                run=inventory.start_run()
                inventory.finish_run(run,'limited',{'status':'limited','asset_retry_after_seconds':300,'cookies':'PRIVATE'})
            (root/'.archive-state/progress.json').write_text(json.dumps({'run_id':run,'status':'limited'}))
            value=library_status(root)
            self.assertEqual(value['progress'],{'status':'limited','asset_retry_after_seconds':300})
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_paused_summary_supplies_validated_reason_without_private_error_details(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)/'Library'
            with Inventory(root) as inventory:
                run = inventory.start_run()
                inventory.finish_run(run, 'paused', {'status':'paused','pause_reason':'source_refused',
                    'source_http_status':403,'source_response_kind':'challenge','headers':{'PRIVATE':'COOKIE'}})
            progress = root/'.archive-state/progress.json'
            progress.write_text(json.dumps({'run_id':run,'status':'paused','downloaded_pages':2,
                                            'message':'PRIVATE_COOKIE'}))
            view = library_status(root)
            self.assertEqual(view['progress'], {'status':'paused','downloaded_pages':2,
                                               'pause_reason':'source_refused','source_http_status':403,
                                               'source_response_kind':'challenge'})
            self.assertNotIn('PRIVATE', json.dumps(view))
            self.assertEqual(set(view['last_run']),{'id','started_at','finished_at','status'})

    def test_pause_fields_reject_unknown_codes_booleans_and_untrusted_reasons(self):
        from comic_archive.monitoring import _progress
        for code in (True, '403', 200, -1):
            self.assertEqual(_progress({'status':'paused','pause_reason':'source_refused',
                                        'source_http_status':code}), {'status':'paused'})
        self.assertEqual(_progress({'status':'paused','pause_reason':'PRIVATE_COOKIE'}), {'status':'paused'})
        for kind in ('PRIVATE_COOKIE',True,{},[]):
            self.assertEqual(_progress({'status':'paused','pause_reason':'source_refused',
                'source_http_status':403,'source_response_kind':kind}),
                {'status':'paused','pause_reason':'source_refused','source_http_status':403})
        self.assertEqual(_progress({'status':'running','source_response_kind':'challenge'}),{'status':'running'})

    def test_scan_progress_exposes_only_completed_integer_counts(self):
        from comic_archive.monitoring import _progress
        fields={'status':'running','phase':'scan','series_checks_completed':2,
                'successful_series_checks':1,'held_series_checks':1,'series_check_target_count':10}
        self.assertEqual(_progress(fields|{'response':'PRIVATE'}),fields)
        invalid=fields|{'series_checks_completed':True,'successful_series_checks':-1,
                        'held_series_checks':'PRIVATE','series_check_target_count':float('nan')}
        self.assertEqual(_progress(invalid),{'status':'running','phase':'scan'})

    def test_progress_is_scoped_to_current_job_and_omits_unknown_fields(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)/'Library'
            with Inventory(root) as inventory:
                run=inventory.start_run()
            progress=root/'.archive-state/progress.json'
            progress.write_text(json.dumps({'run_id':run,'status':'running','phase':'scan',
                'series_id':'42','downloaded_pages':2,'downloaded_bytes':3,
                'message':'PRIVATE cookie','authorization':'PRIVATE','source_slug':'bad?PRIVATE'}))
            result=library_status(root)
            self.assertEqual(result['progress'],{'status':'running','phase':'scan',
                'series_id':'42','downloaded_pages':2,'downloaded_bytes':3})
            self.assertNotIn('PRIVATE',json.dumps(result))
            progress.write_text(json.dumps({'run_id':run,'status':[],'phase':{},'category':[]}))
            self.assertEqual(library_status(root)['progress'],{})
            progress.write_text(json.dumps({'run_id':'other-job','status':'running'}))
            self.assertIsNone(library_status(root)['progress'])

    def test_reading_list_progress_exposes_counts_without_private_response_fields(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)/'Library'
            with Inventory(root) as inventory:
                run=inventory.start_run()
                inventory.observe('42','Watched but unscanned',[],'subscribed')
            progress=root/'.archive-state/progress.json'
            progress.write_text(json.dumps({'run_id':run,'status':'running','phase':'account_list',
                'category':'subscribed','reading_list_page_count':3,'observed_library_series_count':45,
                'url':'PRIVATE_SESSION','response':'PRIVATE_SESSION'}))
            result=library_status(root)
            self.assertEqual(result['progress'],{'status':'running','phase':'account_list','category':'subscribed',
                'reading_list_page_count':3,'observed_library_series_count':45})
            self.assertEqual(result['series_without_scan_checkpoint_count'],1)
            self.assertEqual(result['series_without_episode_observations_count'],1)
            self.assertNotIn('PRIVATE',json.dumps(result))
            # Another site's list is shown by its own name.
            progress.write_text(json.dumps({'run_id':run,'status':'running','phase':'account_list','category':'unlocked'}))
            self.assertEqual(library_status(root)['progress']['category'],'unlocked')

    def test_bad_progress_and_worker_links_do_not_escape_the_workspace(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)/'Library'
            with Inventory(root):
                pass
            progress=root/'.archive-state/progress.json'
            progress.write_text('{broken')
            session=Path(work)/'session'
            (session/'worker').mkdir(parents=True)
            outside=Path(work)/'outside'
            outside.write_text(json.dumps({'state':'busy','task_id':'PRIVATE'}))
            (session/'worker/status.json').symlink_to(outside)
            result=library_status(root,session)
            self.assertIsNone(result['worker'])
            self.assertIsNone(result['progress'])

    def test_worker_status_contains_only_a_fixed_state_and_task_id(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)/'Library'
            with Inventory(root):
                pass
            session=Path(work)/'session'
            (session/'worker').mkdir(parents=True)
            (session/'worker/status.json').write_text(json.dumps({'state':'busy','task_id':'example-1','cookie':'PRIVATE'}))
            result=library_status(root,session)
            self.assertEqual(result['worker'],{'reported_state':'busy','task_id':'example-1'})
            self.assertNotIn('PRIVATE',json.dumps(result))
            # A downloader that is still starting says so; the page does not take it for one that is offline.
            (session/'worker/status.json').write_text(json.dumps({'state':'starting'}))
            self.assertEqual(library_status(root,session)['worker'],{'reported_state':'starting'})
            (session/'worker/status.json').write_text(json.dumps({'state':'busy','task_id':'example-1','cookie':'PRIVATE'}))
            (session/'worker/progress.json').write_text(json.dumps({'task_id':'example-1','phase':'history',
                'history_page_count':50,'observed_history_series_count':7,'cursor':'PRIVATE'}))
            result=library_status(root,session)
            self.assertEqual(result['worker']['progress'],{'phase':'history','history_page_count':50,'observed_history_series_count':7})
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_episode_and_series_identities_are_exposed_only_when_well_formed(self):
        from comic_archive.monitoring import _progress
        self.assertEqual(_progress({'episode_id':'81','series_id':'42','source_slug':'my-series_1','cookie':'PRIVATE'}),
                         {'episode_id':'81','series_id':'42','phase':'archive','source_slug':'my-series_1'})
        self.assertEqual(_progress({'episode_id':'81','phase':'scan'}),{'episode_id':'81','phase':'scan'})
        for invalid in ('0','8x',81,'../81','1'*21):
            self.assertEqual(_progress({'episode_id':invalid}),{})
        for invalid in ('../outside','a b','x'*101,'',5):
            self.assertEqual(_progress({'source_slug':invalid}),{})

    def test_network_pause_exposes_only_a_known_gateway_status(self):
        from comic_archive.monitoring import _progress
        paused={'status':'paused','pause_reason':'network_unavailable'}
        self.assertEqual(_progress(paused),paused)
        self.assertEqual(_progress(paused|{'source_http_status':503}),paused|{'source_http_status':503})
        for code in (500,403,'503',True):
            self.assertEqual(_progress(paused|{'source_http_status':code}),{'status':'paused'})
        self.assertEqual(_progress({'status':'paused','pause_reason':'source_refused'}),{'status':'paused'})

    def test_stored_summary_that_is_not_an_object_adds_no_pause_details(self):
        for status,summary in (('paused',None),('limited',['PRIVATE'])):
            with self.subTest(status=status,summary=summary),tempfile.TemporaryDirectory() as work:
                root=Path(work)/'Library'
                with Inventory(root) as inventory:
                    run=inventory.start_run()
                    inventory.finish_run(run,status,summary)
                (root/'.archive-state/progress.json').write_text(json.dumps({'run_id':run,'status':status}))
                view=library_status(root)
                self.assertEqual(view['progress'],{'status':status})
                self.assertNotIn('PRIVATE',json.dumps(view))

    def test_damaged_backlog_checkpoint_does_not_break_the_status_view(self):
        with tempfile.TemporaryDirectory() as work:
            project=Path(work);root=project/'Library'
            with Inventory(root):
                pass
            for name in ('.comic-session','session'):
                with self.subTest(session=name):
                    worker=project/name/'worker'
                    for folder in ('requests','responses'):(worker/folder).mkdir(parents=True,exist_ok=True)
                    (worker/'backlog.json').write_text('{broken')
                    (worker/'status.json').write_text(json.dumps({'state':'ready'}))
                    result=library_status(root,project/name)
                    self.assertIsNone(result['source_recovery'])
                    self.assertEqual(result['worker'],{'reported_state':'ready'})
