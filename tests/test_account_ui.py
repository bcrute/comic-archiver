from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import account_ui, download_control, sites
from comic_archive.app_actions import Actions, Refused
from comic_archive.cancellation import RunCancellation
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered, job_progress
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError
from comic_archive.worker import private_json
from test_jobs import AccountAdapter


class AccountUITests(unittest.TestCase):
    def test_unreadable_choices_hold_safely_and_can_be_replaced_without_starting_downloads(self):
        with Inventory(self.workspace.library) as inventory:
            inventory.db.execute("INSERT INTO settings VALUES ('download_types','{PRIVATE')")
            inventory.db.commit()
        value = account_ui.status(self.workspace)
        self.assertEqual((value['status'], value['code'], value['download_types']), ('held', 'controller_failed', None))
        self.assertNotIn('PRIVATE', json.dumps(value))
        choices = dict(bought=True, free=False, limited=False, unclassified=False)
        fixed = self.registry.choose_download_types(LOCAL_OWNER, self.workspace.id, choices)
        self.assertEqual(fixed['download_types'], choices)
        self.assertFalse(fixed['configured'])
        self.assertFalse(list((self.queue / 'requests').iterdir()))

    def test_download_types_are_saved_without_starting_an_idle_account(self):
        choices = dict(bought=True, free=False, limited=False, unclassified=False)
        changed = self.registry.choose_download_types(LOCAL_OWNER, self.workspace.id, choices)
        self.assertEqual(changed['download_types'], choices)
        self.assertFalse(changed['configured'])
        self.assertEqual(changed['speed'], 'fast')
        self.assertFalse(list((self.queue / 'requests').iterdir()))
        with Inventory(self.workspace.library, read_only=True) as inventory:
            self.assertEqual(inventory.status()['download_types'], choices)

    def test_running_type_change_waits_for_receipt_preserves_files_and_excludes_the_old_backlog(self):
        from comic_archive.download_selection import TYPES
        task, _ = self.running_batch()
        saved = self.workspace.library / 'kept.cbz'
        saved.write_bytes(b'existing archive')
        choices = dict(bought=True, free=False, limited=False, unclassified=False)

        def stopped(_):
            self.assertEqual(account_ui.download_types(self.workspace), dict.fromkeys(TYPES, True))
            self.assertEqual(self.state()['status'], 'stopped')
            self.cancelled_batch(task)

        with patch.object(account_ui.time, 'sleep', side_effect=stopped):
            changed = self.registry.choose_download_types(LOCAL_OWNER, self.workspace.id, choices)
        self.assertEqual((changed['status'], changed['download_types']), ('running', choices))
        self.assertEqual(saved.read_bytes(), b'existing archive')
        self.assertIsNone(self.outstanding())
        with Inventory(self.workspace.library, read_only=True) as inventory:
            self.assertEqual(inventory.pending(), [])

    def test_failed_type_change_keeps_old_preferences_and_stays_paused(self):
        from comic_archive.download_selection import TYPES
        task, _ = self.running_batch()
        choices = dict(bought=True, free=False, limited=False, unclassified=False)
        with patch.object(account_ui, 'SPEED_CHANGE_WAIT_SECONDS', 0), self.assertRaises(ArchiveError):
            self.registry.choose_download_types(LOCAL_OWNER, self.workspace.id, choices)
        self.assertEqual(account_ui.download_types(self.workspace), dict.fromkeys(TYPES, True))
        self.assertEqual(self.state()['status'], 'stopped')
        self.assertEqual(self.outstanding(), task)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.registry=LocalWorkspaceRegistry(self.root)
        self.workspace=self.registry.resolve(LOCAL_OWNER,self.registry.workspace_id)
        self.queue=self.workspace.session/'worker'
        (self.queue/'requests').mkdir(parents=True);(self.queue/'responses').mkdir()
        private_json(self.queue/'status.json',{'state':'ready'})
        private_json(self.workspace.session/'state.json',{'cookies':[],'origins':[]})
        # What the window's calls go through: the open site's library, as the app's runtime holds it.
        runtime=SimpleNamespace(registry=self.registry,site=sites.DEFAULT,folder=self.workspace.library)
        self.actions=Actions(lambda:runtime)

    def state(self):
        """The download's own record, as the controller reads it."""
        db=download_control._connect(self.queue)
        try:return download_control._state(db)
        finally:db.close()

    def change(self,sql,*args):
        db=download_control._connect(self.queue)
        try:db.execute(sql,args)
        finally:db.close()

    def started(self):
        return download_control.status(self.workspace.session,self.workspace.library)['started_at']

    def outstanding(self):
        return download_control.outstanding(self.workspace.session,self.workspace.library)

    def tick(self):
        download_control.worker_tick(self.workspace.session,self.workspace.library)

    def test_go_pause_and_signin_reach_only_this_librarys_own_account_and_queue_nothing_themselves(self):
        for name,method_name in [('go','start_account'),('signin','start_site_signin'),('pause','pause_account')]:
            with patch.object(self.registry,method_name,return_value={'status':'running'}) as action:
                self.assertEqual(getattr(self.actions,name)(),{'status':'running'})
                action.assert_called_once_with(LOCAL_OWNER,self.workspace.id)
        # Another owner's or another library's account is never reached.
        for owner,workspace_id in (('someone-else',self.workspace.id),(LOCAL_OWNER,'0'*32)):
            with self.assertRaisesRegex(ArchiveError,'unavailable for this owner'):
                self.registry.start_account(owner,workspace_id)
        self.assertFalse(list((self.queue/'requests').iterdir()))

    def test_native_pause_callback_observes_the_saved_stop_before_any_renderer_is_interrupted(self):
        account_ui.start(self.workspace)
        seen = []
        account_ui.pause(self.workspace, on_stop_requested=lambda: seen.append(
            download_control.status(self.workspace.session, self.workspace.library)['status']))
        self.assertEqual(seen, ['stopped'])

    def test_explicit_pause_can_interrupt_a_reader_even_before_an_account_plan_exists(self):
        seen = []
        account_ui.pause(self.workspace, on_stop_requested=lambda: seen.append('stopped'))
        self.assertEqual(seen, ['stopped'])
        self.assertFalse(list((self.queue / 'requests').iterdir()))

    def test_go_is_durable_account_wide_and_duplicate_go_does_not_start_another_run(self):
        before=self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertFalse(before['configured']);self.assertFalse((self.queue/download_control.STATE_FILE).exists())
        result=self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['status'],'running');self.assertEqual(result['phase'],'lists')
        saved=self.state()
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(self.state(),saved)
        self.assertFalse(download_control._pace(self.workspace.session)[0]['free_only'])
        self.assertFalse(list((self.queue/'requests').glob('*.json')))
        self.tick()
        request=json.loads(next((self.queue/'requests').glob('*.json')).read_text())
        self.assertEqual(request['args']['categories'],['subscribed'])
        self.assertNotIn('url',request['args']);self.assertNotIn('id',request['args'])

    def pace(self,options):
        return (options.get('speed','normal'),options.get('download_workers',1),options['request_interval'],
                options['max_bytes'])

    def next_pace(self):
        """The pace the download's next step is asked for at."""
        return self.pace(download_control._pace(self.workspace.session)[0])

    def test_go_runs_at_the_fast_speed_until_the_person_chooses_another(self):
        self.assertEqual((account_ui.DESKTOP_REQUEST_INTERVAL,account_ui.DESKTOP_BROWSER),(1.0,'qt'))
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        task,_=self.running_batch()
        request=json.loads((self.queue/'requests'/(task+'.json')).read_text())
        # The pace the earlier Go ran at against Tapas.
        self.assertEqual(self.pace(request['args']),('fast',3,0.25,8*1024**3))
        self.assertIs(request['args']['free_only'],False)

    def test_a_chosen_speed_is_kept_and_go_continues_the_saved_download_at_it(self):
        for chosen,pace in (('slow',('slow',2,1.0,1024**3)),('medium',('medium',4,0.5,1024**3)),
                            ('fast',('fast',3,0.25,8*1024**3))):
            self.assertEqual(self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,chosen)['speed'],chosen)
            # Kept with the library, so the next start of the app finds it.
            again=LocalWorkspaceRegistry(self.root)
            self.assertEqual(again.account_status(LOCAL_OWNER,again.workspace_id)['speed'],chosen)
            self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
            self.assertEqual(self.next_pace(),pace)
            self.assertEqual(self.state()['stage'],'lists')
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)

    def test_an_unknown_speed_is_refused_without_stopping_a_running_download(self):
        with self.assertRaises(ArchiveError):
            self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'ludicrous')
        task, run = self.running_batch()
        before = self.state()
        with self.assertRaises(ArchiveError):
            self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'ludicrous')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.outstanding(), task)
        self.assertFalse(RunCancellation(self.workspace.library, run).requested())

    def test_a_speed_file_this_app_cannot_read_means_the_fast_speed_and_go_still_works(self):
        for content in (b'ludicrous\n',b'\xff\xfe'):
            (self.workspace.session/account_ui.SPEED_FILE).write_bytes(content)
            self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(self.next_pace()[0],'fast')

    def test_a_speed_file_that_is_a_link_is_never_written_or_read_through(self):
        # A library folder from elsewhere could hold links here to a file of the person's own.
        mine=self.root/'notes.txt';mine.write_text('slow\n')
        for name in (account_ui.SPEED_FILE+'.new',account_ui.SPEED_FILE):
            (self.workspace.session/name).symlink_to(mine)
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        self.assertEqual(self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'medium')['speed'],'medium')
        self.assertEqual(mine.read_text(),'slow\n')
        self.assertFalse((self.workspace.session/account_ui.SPEED_FILE).is_symlink())
        if account_ui.NOFOLLOW:
            # A regular file can be moved and replaced by a link between the
            # initial inspection and open. Linking the same inode bypasses an
            # identity comparison, so the no-follow open must still refuse it.
            path = self.workspace.session / account_ui.SPEED_FILE
            path.write_text('slow\n')
            kept = self.root / 'original-speed.txt'
            original_open = os.open
            changed = False

            def swap_before_open(filename, flags, *args, **kwargs):
                nonlocal changed
                if Path(filename) == path and not changed:
                    changed = True
                    path.rename(kept)
                    path.symlink_to(kept)
                return original_open(filename, flags, *args, **kwargs)

            with patch('comic_archive.account_ui.os.open', side_effect=swap_before_open):
                self.assertEqual(account_ui.speed(self.workspace), 'fast')
            self.assertTrue(changed)
            self.assertEqual(kept.read_text(), 'slow\n')

    def test_a_speed_that_cannot_be_saved_is_reported_and_the_earlier_choice_stands(self):
        self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'medium')
        with patch('os.replace',side_effect=OSError):
            with self.assertRaisesRegex(ArchiveError,'could not be saved'):
                self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'slow')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'medium')

    def test_the_windows_speed_choice_takes_one_known_speed_and_is_refused_in_words_otherwise(self):
        with self.assertRaises(Refused) as refused:self.actions.speed('ludicrous')
        self.assertEqual(refused.exception.code,'request_unavailable')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['speed'],'fast')
        self.assertEqual(self.actions.speed('slow')['speed'],'slow')
        self.assertEqual(self.actions.poll(cloud=False,network=False)['account']['speed'],'slow')
        # The same action can change a running download between worker steps.
        self.actions.go()
        self.assertEqual(self.actions.speed('fast')['status'], 'running')
        self.assertEqual(self.registry.account_status(LOCAL_OWNER, self.workspace.id)['speed'], 'fast')

    def test_pause_then_go_keeps_the_same_pending_discovery_task(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.tick()
        before=self.outstanding()
        self.assertIsNotNone(before)
        self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(self.outstanding(),before)
        self.tick()
        self.assertEqual([p.stem for p in (self.queue/'requests').glob('*.json')],[before])

    def test_first_signin_and_go_work_before_any_worker_has_reported(self):
        (self.queue/'status.json').unlink();(self.workspace.session/'state.json').unlink()
        with patch.object(self.registry.site_signin,'start',return_value={'status':'running'}) as start:
            self.assertEqual(self.registry.start_site_signin(LOCAL_OWNER,self.workspace.id),{'status':'running'})
        start.assert_called_once_with()
        private_json(self.workspace.session/'state.json',{})
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)

    def test_go_refuses_missing_session_busy_worker_or_signin_without_dispatch(self):
        (self.workspace.session/'state.json').unlink()
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        private_json(self.workspace.session/'state.json',{})
        private_json(self.queue/'status.json',{'state':'busy'})
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        private_json(self.queue/'status.json',{'state':'ready'})
        with patch.object(self.registry.site_signin,'status',return_value={'status':'running'}):
            with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.registry.cloud_signin._state={'status':'running'}
        with self.assertRaises(ArchiveError):self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertFalse((self.queue/download_control.STATE_FILE).exists())

    def test_public_account_status_never_projects_private_receipts_or_unknown_errors(self):
        value={'configured':True,'status':'held','phase':'verify','code':'PRIVATE_RAW_MESSAGE',
               'outstanding':'PRIVATE_TOKEN','started_at':'PRIVATE','audit':{'verified_inventory_episode_count':7,
                'missing_recorded_accessible_episode_count':0,'episode_id':'PRIVATE_SOURCE_ID',
                'inventory_index_problem_count':True,'unindexed_archive_count':-1}}
        with patch.object(download_control,'status',return_value=value):result=account_ui.status(self.workspace)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('code',result)
        self.assertEqual(result['audit'],{'verified_inventory_episode_count':7,'missing_recorded_accessible_episode_count':0})
        self.assertFalse(result['coverage_verified'])
        for key,unknown in (('status','waiting'),('phase','PRIVATE')):
            with patch.object(download_control,'status',return_value={'configured':True,'status':'running',key:unknown}):
                self.assertNotIn('PRIVATE',json.dumps(account_ui.status(self.workspace)))
                self.assertNotEqual(account_ui.status(self.workspace).get(key),unknown)

    def test_purchase_history_warnings_reach_the_dashboard_without_private_records(self):
        value = {'configured': True, 'status': 'running', 'phase': 'series', 'purchase_history': {
            'status': 'partial', 'code': 'history_rows_unverified', 'pages': 1, 'discovered': 9,
            'unresolved': 0, 'transactions': 'PRIVATE', 'cursor': 'PRIVATE'}}
        with patch.object(download_control, 'status', return_value=value):
            result = account_ui.status(self.workspace)
        self.assertEqual(result['purchase_history'], {'status': 'partial', 'code': 'history_rows_unverified',
                                                     'pages': 1, 'discovered': 9, 'unresolved': 0})
        self.assertNotIn('PRIVATE', json.dumps(result))
        value['purchase_history'].update(code='PRIVATE', pages=True, discovered=-1, unresolved='PRIVATE')
        with patch.object(download_control, 'status', return_value=value):
            self.assertEqual(account_ui.status(self.workspace)['purchase_history'], {'status': 'partial'})

    def test_each_reason_the_whole_download_held_reaches_the_page_so_it_can_say_what_to_do(self):
        for code in sorted(download_control.HOLDS):
            with patch.object(download_control,'status',return_value={'configured':True,'status':'held','code':code}):
                self.assertEqual(account_ui.status(self.workspace)['code'],code)
        with patch.object(download_control,'status',side_effect=ArchiveError('PRIVATE')):
            shown=account_ui.status(self.workspace)
        self.assertEqual((shown['status'],shown['code']),('held','controller_failed'))

    def test_verification_activity_uses_only_the_current_local_worker_checkpoint(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        started=self.started()
        worker=self.workspace.session/'worker';worker.mkdir(exist_ok=True)
        private_json(worker/'status.json',{'state':'busy','task_id':'audit-1'})
        path=worker/'progress.json'
        private_json(path,{'status':'running','phase':'verify','task_id':'audit-1'})
        os.utime(path,(started+80,started+80))
        with patch.object(download_control,'status',return_value={'configured':True,'status':'running',
                          'phase':'verify','started_at':started}), patch('comic_archive.account_ui.time.time',return_value=started+90):
            result=account_ui.status(self.workspace)
            self.assertEqual(result['progress_age_seconds'],10)
            private_json(worker/'status.json',{'state':'busy','task_id':'other'})
            self.assertNotEqual(account_ui.status(self.workspace).get('progress_age_seconds'),10)

    def test_transfer_counts_completed_batches_and_only_the_current_tasks_checkpoint(self):
        account_ui.start(self.workspace)
        started = self.started()
        stamp = lambda seconds: datetime.fromtimestamp(started + seconds, timezone.utc).isoformat()
        def task(number, published, finished, count, outcome='ok'):
            task_id = 'job-' + f'{number:024x}'
            self.change('INSERT INTO tasks (task_id,kind,operation,options,reserved_at,published_at,finished_at,'
                        'outcome,bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', task_id, 'download',
                        'archive_discovered', '{}', stamp(published), stamp(published),
                        stamp(finished) if finished is not None else None, outcome, count)
            return task_id
        task(1, -100, -90, 900)  # An earlier account scan is outside this counter.
        task(2, 1, 10, 100)
        task(3, 11, 15, 20, 'error')  # Bytes already transferred still count after a partial failure.
        current = task(4, 16, None, None)
        path = self.workspace.library / '.archive-state/progress.json'
        def checkpoint(count, changed, task_id=current):
            private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': task_id})
            private_json(path, {'status': 'running', 'phase': 'metadata', 'downloaded_bytes': count})
            os.utime(path, (started + changed, started + changed))
        def total():
            value = account_ui.status(self.workspace)
            self.assertNotIn('_transfer', value)
            self.assertNotIn(current, json.dumps(value))
            self.assertEqual(value['transfer']['scope'], started)
            return value['transfer']['downloaded_bytes']
        with patch('comic_archive.account_ui.time.time', return_value=started + 600):
            checkpoint(30, 20)
            self.assertEqual(total(), 150)
            private_json(self.queue / 'responses' / (current + '.json'),
                         {'task_id': current, 'status': 'ok', 'result': {'downloaded_bytes': 30}})
            private_json(self.queue / 'status.json', {'state': 'ready'})
            self.assertEqual(total(), 150)  # Worker finished; controller has not settled its receipt yet.
            self.change('UPDATE tasks SET finished_at=?, bytes=? WHERE task_id=?', stamp(80), 30, current)
            self.assertEqual(total(), 150)  # Completion replaces the checkpoint, never adds it twice.
            following = task(5, 90, None, None)
            self.assertEqual(total(), 150)  # Old task's progress cannot belong to this task.
            checkpoint(5, 100)
            self.assertEqual(total(), 150)  # Fresh file, but the worker belongs to another task.
            checkpoint(5, 20, following)
            self.assertEqual(total(), 150)  # Matching worker, stale checkpoint.
            checkpoint(5, 100, following)
            self.assertEqual(total(), 155)
            for invalid in (True, -1, '5'):
                checkpoint(invalid, 100, following)
                self.assertEqual(total(), 150)
            checkpoint(5, 601, following)
            self.assertEqual(total(), 150)  # A future checkpoint is not current progress.
        self.assertFalse(list((self.queue / 'requests').glob('*.json')))

    def test_list_progress_and_activity_come_from_existing_checkpoints_without_dispatch(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        started=self.started()
        self.change("UPDATE lists SET read_at=?, complete=0, observed_series_count=45 WHERE category='subscribed'",
                    datetime.now(timezone.utc).isoformat())
        path=self.workspace.library/'.archive-state/progress.json'
        private_json(path,{'status':'running','phase':'account_list','category':'updated',
                           'reading_list_page_count':2,'message':'PRIVATE_COOKIE','source_slug':'PRIVATE_SOURCE'})
        os.utime(path,(started+20,started+20))
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            result=self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['lists'],{'checked':1,'total':5,'current':'updated'})
        self.assertEqual(result['elapsed_seconds'],90)
        self.assertEqual(result['progress_age_seconds'],70)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertFalse(list((self.queue/'requests').glob('*.json')))
        os.utime(path,(started-1,started-1))
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))
        path.unlink();outside=self.root/'private-progress';outside.write_text('{"status":"running"}')
        path.symlink_to(outside)
        with patch('comic_archive.account_ui.time.time',return_value=started+90):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))

    def test_the_clock_counts_the_present_stretch_of_work_not_the_time_since_the_run_first_began(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        began=datetime.fromtimestamp(self.started(),timezone.utc)-timedelta(hours=14)
        self.change('UPDATE state SET stretch_started_at=?',began.isoformat())
        path=self.workspace.library/'.archive-state/progress.json'
        private_json(path,{'status':'running','phase':'account_list','category':'subscribed'})
        # The program was closed an hour into the run and is opened again now.
        os.utime(path,(began.timestamp()+3600,began.timestamp()+3600))
        reopened=LocalWorkspaceRegistry(self.root);opened=reopened.working_since
        self.assertGreater(opened,began.timestamp()+3600)
        with patch('comic_archive.account_ui.time.time',return_value=opened+30):
            result=reopened.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(result['elapsed_seconds'],30)
        self.assertNotIn('progress_age_seconds',result)
        os.utime(path,(opened+10,opened+10))
        with patch('comic_archive.account_ui.time.time',return_value=opened+30):
            self.assertEqual(reopened.account_status(LOCAL_OWNER,self.workspace.id)['progress_age_seconds'],20)
        # Go after a pause begins a new stretch.
        reopened.working_since=opened-5000
        self.assertEqual(reopened.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(reopened.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertGreaterEqual(reopened.working_since,opened)
        with patch('comic_archive.account_ui.time.time',return_value=max(reopened.working_since,self.started())+7.5):
            self.assertEqual(reopened.account_status(LOCAL_OWNER,self.workspace.id)['elapsed_seconds'],7)

    def test_a_step_that_ended_and_a_wait_before_trying_again_are_not_reported_as_a_stall(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        path=self.workspace.library/'.archive-state/progress.json'
        started=self.started()
        private_json(path,{'status':'paused','pause_reason':'network_unavailable'});os.utime(path,(started+10,started+10))
        at=lambda seconds:datetime.fromtimestamp(started+seconds,timezone.utc).isoformat()
        status=lambda:self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        now=datetime.fromtimestamp(started+20,timezone.utc)
        with patch('comic_archive.account_ui.time.time',return_value=started+20), \
                patch('comic_archive.download_control._now',return_value=now):
            result=status()
            self.assertEqual(result['between_steps_seconds'],10)
            self.assertNotIn('progress_age_seconds',result);self.assertNotIn('retry',result)
            for code,until,retry in (('connection',at(79.5),{'reason':'connection','seconds':60}),
                                     # The Wi-Fi-only wait is worded by the page from its own network view.
                                     ('network_policy',at(319.5),{'reason':'connection','seconds':300}),
                                     ('source',at(319.5),{'reason':'source','seconds':300}),
                                     # The wait before images that did not arrive are tried again is not a stall either.
                                     ('images',at(49.5),{'reason':'images','seconds':30}),
                                     ('idle',at(919.5),{'reason':'idle','seconds':900}),
                                     # A wait that is over is acted on at the next look.
                                     ('connection',at(20),None)):
                self.change("UPDATE state SET status='waiting', code=?, wait_until=?",code,until)
                shown=status()
                self.assertEqual((shown['status'],shown.get('retry')),('running',retry),code)
                self.assertNotIn('code',shown)
            # A download the person paused is not about to try anything.
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
            self.assertNotIn('retry',status())
            # The wait the site asked for, which Go cannot cut short: the page says how long instead of Go failing.
            for after,wait in ((at(619.5),600),(at(20),None),(None,None)):
                self.change("UPDATE state SET status='held', code='source_refused', go_after=?",after)
                self.assertEqual(status().get('go_after_seconds'),wait,after)
                self.assertNotIn('retry',status())

    def test_activity_rejects_future_malformed_and_finished_progress(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        path=self.workspace.library/'.archive-state/progress.json'
        started=self.started()
        for content in ('{bad','{}','{"status":"finished"}'):
            path.write_text(content);os.utime(path,(started+10,started+10))
            with patch('comic_archive.account_ui.time.time',return_value=started+20):
                self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))
        private_json(path,{'status':'running'})
        os.utime(path,(started+30,started+30))
        with patch('comic_archive.account_ui.time.time',return_value=started+20):
            self.assertNotIn('progress_age_seconds',self.registry.account_status(LOCAL_OWNER,self.workspace.id))

    def test_go_after_a_paused_speed_change_starts_a_fresh_step_at_the_chosen_speed(self):
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        # The downloader has stopped the step, but its receipt is not settled yet.
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.registry.choose_speed(LOCAL_OWNER,self.workspace.id,'medium')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        # Changing pace settles the cancelled step before a new one can be queued.
        self.assertIsNone(self.outstanding())
        self.assertEqual(self.next_pace(),('medium',4,0.5,1024**3))
        self.tick()
        again=self.outstanding()
        self.assertNotIn(again,(None,task))
        request=json.loads((self.queue/'requests'/(again+'.json')).read_text())
        self.assertEqual((request['args']['download_workers'],request['args']['request_interval']),(4,0.5))
        self.assertEqual(request['args']['speed'],'medium')

    def running_batch(self):
        """A download batch the downloader has started: published request, busy worker, recorded run."""
        library=self.workspace.library
        with Inventory(library) as inventory:
            inventory.observe('42','Fixture',[{'id':'81','title':'One','order':1,'access_state':'tapas_free'}],'fixture')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.tick()
        task=self.outstanding()
        if task is None or not (self.queue/'requests'/(task+'.json')).is_file():self.fail('no batch was published')
        private_json(self.queue/'status.json',{'state':'busy','task_id':task})
        with Inventory(library) as inventory:run=inventory.start_run()
        job_progress(library,run,phase='archive')
        return task,run

    def end_batch(self,task,value,request=True):
        private_json(self.queue/'responses'/(task+'.json'),{'task_id':task,**value})
        if request:(self.queue/'requests'/(task+'.json')).unlink()
        private_json(self.queue/'status.json',{'state':'ready'})

    def cancelled_batch(self, task):
        from comic_archive.task_cancellation import cancelled_result
        result = cancelled_result(self.workspace.library, 'archive_discovered')
        result['cancelled_before_start'] = False
        self.end_batch(task, {'status': 'ok', 'result': result})

    def test_running_speed_change_waits_for_terminal_work_then_queues_only_the_new_pace(self):
        task, run = self.running_batch()
        saved = self.workspace.library / 'kept.cbz'; saved.write_bytes(b'kept archive')
        self.change('UPDATE state SET failed_retries=1, network_waits=2, source_probes=1')
        before = self.state()
        old_request = json.loads((self.queue / 'requests' / (task + '.json')).read_text())

        def finish(_):
            self.assertTrue(RunCancellation(self.workspace.library, run).requested())
            self.assertEqual(self.state()['status'], 'stopped')
            self.assertEqual(account_ui.speed(self.workspace), 'fast')
            self.assertEqual(json.loads((self.queue / 'requests' / (task + '.json')).read_text()), old_request)
            self.cancelled_batch(task)
        with patch.object(account_ui.time, 'sleep', side_effect=finish):
            changed = self.actions.speed('medium')
        self.assertEqual((changed['status'], changed['speed']), ('running', 'medium'))
        self.assertIsNone(self.outstanding())
        for key in ('stretch_started_at', 'failed_retries', 'network_waits', 'source_probes'):
            self.assertEqual(self.state()[key], before[key], key)
        self.assertEqual(saved.read_bytes(), b'kept archive')
        self.tick()
        fresh = self.outstanding(); self.assertNotEqual(fresh, task)
        request = json.loads((self.queue / 'requests' / (fresh + '.json')).read_text())
        self.assertEqual(self.pace(request['args']), ('medium', 4, 0.5, 1024**3))
        self.assertEqual([p.stem for p in (self.queue / 'requests').glob('*.json')], [fresh])

    def test_a_terminal_receipt_cannot_resume_until_the_writer_releases_the_library(self):
        from comic_archive.locking import LibraryLock
        task, _ = self.running_batch()
        held, release = threading.Event(), threading.Event()
        def writer():
            with LibraryLock(self.workspace.library):
                held.set()
                release.wait(3)
        thread = threading.Thread(target=writer); thread.start()
        try:
            self.assertTrue(held.wait(3))
            self.cancelled_batch(task)
            def finish(_):
                self.assertEqual(account_ui.speed(self.workspace), 'fast')
                self.assertEqual(self.state()['status'], 'stopped')
                release.set(); thread.join(3)
                self.assertFalse(thread.is_alive())
            with patch.object(account_ui.time, 'sleep', side_effect=finish) as slept:
                changed = self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'medium')
            slept.assert_called_once()
            self.assertEqual((changed['status'], changed['speed']), ('running', 'medium'))
        finally:
            release.set(); thread.join(3)

    def test_a_queued_old_pace_is_cancelled_before_a_fresh_task_can_use_the_new_speed(self):
        task, _ = self.running_batch()
        private_json(self.queue / 'status.json', {'state': 'ready'})
        def stopped(_):
            from comic_archive.task_cancellation import queued_stop_requested
            self.assertTrue(queued_stop_requested(self.workspace.session, task))
            self.assertEqual(self.state()['status'], 'stopped')
            self.cancelled_batch(task)
        with patch.object(account_ui.time, 'sleep', side_effect=stopped):
            self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'slow')
        self.assertIsNone(self.outstanding())
        self.tick()
        fresh = self.outstanding(); self.assertNotEqual(fresh, task)
        request = json.loads((self.queue / 'requests' / (fresh + '.json')).read_text())
        self.assertEqual(self.pace(request['args']), ('slow', 2, 1.0, 1024**3))

    def test_a_request_sent_before_its_publication_commit_is_stopped_before_replacement(self):
        task, run = self.running_batch()
        self.change('UPDATE tasks SET published_at=NULL WHERE task_id=?', task)
        def stopped(_):
            self.assertTrue(RunCancellation(self.workspace.library, run).requested())
            self.assertEqual(account_ui.speed(self.workspace), 'fast')
            self.assertEqual(self.outstanding(), task)
            self.cancelled_batch(task)
        with patch.object(account_ui.time, 'sleep', side_effect=stopped) as slept:
            changed = self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'medium')
        slept.assert_called_once()
        self.assertEqual((changed['status'], changed['speed']), ('running', 'medium'))
        self.assertIsNone(self.outstanding())

    def test_speed_change_timeout_keeps_the_old_choice_and_the_download_paused(self):
        task, _ = self.running_batch()
        with patch.object(account_ui, 'SPEED_CHANGE_WAIT_SECONDS', 0):
            with self.assertRaisesRegex(ArchiveError, 'still stopping'):
                self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'slow')
        self.assertEqual((self.state()['status'], account_ui.speed(self.workspace)), ('stopped', 'fast'))
        self.assertEqual(self.outstanding(), task)
        self.assertFalse((self.queue / 'responses' / (task + '.json')).exists())

    def test_the_speed_action_reports_a_timeout_and_its_real_paused_state(self):
        self.running_batch()
        with patch.object(account_ui, 'SPEED_CHANGE_WAIT_SECONDS', 0):
            with self.assertRaises(Refused) as refused:
                self.actions.speed('medium')
        self.assertEqual(refused.exception.code, 'speed_change_failed')
        self.assertIn('still stopping', str(refused.exception))
        self.assertEqual(self.actions.account()['status'], 'stopped')
        self.assertEqual(self.actions.account()['speed'], 'fast')

    def test_speed_save_failure_after_cancellation_does_not_restart_the_download(self):
        task, _ = self.running_batch()
        with patch.object(account_ui.time, 'sleep', side_effect=lambda _: self.cancelled_batch(task)), \
                patch.object(account_ui, 'choose_speed', side_effect=ArchiveError('Could not save speed')):
            with self.assertRaisesRegex(ArchiveError, 'could not be saved'):
                self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'slow')
        self.assertEqual((self.state()['status'], account_ui.speed(self.workspace)), ('stopped', 'fast'))
        self.tick()
        self.assertIsNone(self.outstanding())
        self.assertFalse(list((self.queue / 'requests').glob('*.json')))

    def test_a_speed_save_error_never_exposes_the_underlying_path_or_secret(self):
        task, _ = self.running_batch()
        with patch.object(account_ui.time, 'sleep', side_effect=lambda _: self.cancelled_batch(task)), \
                patch.object(account_ui, 'choose_speed', side_effect=OSError('PRIVATE_PATH_AND_KEY')):
            with self.assertRaises(Refused) as refused:
                self.actions.speed('slow')
        self.assertEqual(refused.exception.code, 'speed_change_failed')
        self.assertIn('could not be saved', str(refused.exception))
        self.assertNotIn('PRIVATE', str(refused.exception))
        self.assertEqual(self.actions.account()['status'], 'stopped')

    def test_changing_speed_on_an_already_paused_task_does_not_start_it(self):
        task, _ = self.running_batch()
        self.registry.pause_account(LOCAL_OWNER, self.workspace.id)
        with patch.object(account_ui.time, 'sleep', side_effect=lambda _: self.cancelled_batch(task)):
            changed = self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'slow')
        self.assertEqual((changed['status'], changed['speed']), ('stopped', 'slow'))
        self.assertIsNone(self.outstanding())
        self.tick()
        self.assertFalse(list((self.queue / 'requests').glob('*.json')))

    def test_changing_speed_does_not_shorten_a_provider_wait_or_clear_its_retries(self):
        self.registry.start_account(LOCAL_OWNER, self.workspace.id)
        until = download_control._stamp(download_control._now() + timedelta(minutes=10))
        self.change("UPDATE state SET status='waiting', code='source', wait_until=?, source_probes=2, failed_retries=1", until)
        changed = self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'medium')
        self.assertEqual(changed['speed'], 'medium')
        self.assertEqual((self.state()['status'], self.state()['code'], self.state()['wait_until']), ('waiting', 'source', until))
        self.assertEqual((self.state()['source_probes'], self.state()['failed_retries']), (2, 1))
        self.tick()
        self.assertIsNone(self.outstanding())

    def test_a_provider_refusal_arriving_during_speed_change_keeps_its_retry_after(self):
        task, _ = self.running_batch()
        def refused(_):
            self.end_batch(task, {'status': 'error', 'error_code': 'source_refused',
                                 'result': {'pause_reason': 'source_refused', 'retry_after_seconds': 600}})
        with patch.object(account_ui.time, 'sleep', side_effect=refused):
            changed = self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'slow')
        self.assertEqual(changed['speed'], 'slow')
        self.assertEqual(self.state()['status'], 'waiting')
        self.assertGreaterEqual(changed['retry']['seconds'], 599)
        self.tick()
        self.assertIsNone(self.outstanding())

    def test_closing_during_a_speed_change_never_resumes_or_saves_the_new_speed(self):
        task, _ = self.running_batch()
        def closing(_):
            self.cancelled_batch(task)
            self.registry._closing.set()
        with patch.object(account_ui.time, 'sleep', side_effect=closing):
            with self.assertRaisesRegex(ArchiveError, 'closing'):
                self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'medium')
        self.assertEqual((self.state()['status'], account_ui.speed(self.workspace)), ('stopped', 'fast'))

    def test_selecting_the_current_speed_does_not_interrupt_the_task(self):
        task, run = self.running_batch()
        before = self.state()
        self.registry.choose_speed(LOCAL_OWNER, self.workspace.id, 'fast')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.outstanding(), task)
        self.assertFalse(RunCancellation(self.workspace.library, run).requested())

    def test_pause_stops_the_batch_in_progress_instead_of_letting_it_run_out(self):
        _,run=self.running_batch()
        with patch.object(account_ui.time,'sleep') as nap:
            self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        nap.assert_not_called()
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())
        self.assertEqual(self.state()['status'],'stopped')

    def test_pause_asks_again_while_a_batch_that_just_started_cannot_be_stopped_yet(self):
        _,run=self.running_batch()
        (self.workspace.library/'.archive-state/progress.json').unlink()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0),patch.object(account_ui.time,'sleep') as nap:
            self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        nap.assert_not_called()
        self.assertFalse(RunCancellation(self.workspace.library,run).requested())
        naps=[]
        def nap(seconds):
            naps.append(seconds);job_progress(self.workspace.library,run,phase='archive')
        with patch.object(account_ui.time,'sleep',nap):
            self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(naps,[0.25])
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())

    def test_pause_before_a_batch_has_recorded_its_run_stops_it_as_soon_as_it_does(self):
        library=self.workspace.library
        task,earlier=self.running_batch()
        with Inventory(library) as inventory:inventory.finish_run(earlier,'finished',{})
        # The downloader has taken the task; the progress it shows is still an earlier run's.
        private_json(self.queue/'status.json',{'state':'busy','task_id':task,'pid':os.getpid()})
        with patch.object(account_ui.time,'sleep') as nap:
            self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        nap.assert_not_called()
        self.assertTrue((self.queue/'cancellations'/(task+'.json')).is_file())
        self.assertFalse(RunCancellation(library,earlier).requested())
        adapter=AccountAdapter()
        with patch('builtins.print'):result=archive_discovered(adapter,library,limit=1)
        self.assertEqual((result['status'],adapter.calls),('cancelled',[]))

    def test_go_after_a_batch_stopped_by_pause_continues_without_a_hold(self):
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        # The stop that receipt answers is the Pause that Go has overruled: the download goes on.
        self.ticks()
        value=self.state()
        self.assertEqual((value['status'],value['code'],value['failed_retries']),('running',None,0))
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertNotIn(self.outstanding(),(None,task))

    def test_a_step_cancelled_while_the_download_runs_stops_it_until_go(self):
        task,run=self.running_batch()
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.ticks()
        self.assertEqual(self.waiting_for_go(),('stopped',[]))

    def test_go_continues_a_paused_download_of_another_site_past_that_sites_own_lists(self):
        (self.workspace.session/'site').write_text('tappytoon\n')
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        # Tappytoon's three lists were read, then a chapter stopped the download.
        self.change("UPDATE lists SET read_at=?, complete=1, observed_series_count=1",datetime.now(timezone.utc).isoformat())
        self.change("UPDATE state SET status='held', code='task_failed', stage='series'")
        def saved_lists():
            db=download_control._connect(self.queue)
            try:return sorted(tuple(row) for row in db.execute('SELECT * FROM lists'))
            finally:db.close()
        lists=saved_lists()
        self.assertEqual(len(lists),3)
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'held')
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.assertEqual(saved_lists(),lists)
        self.assertEqual(self.state()['stage'],'series')

    def test_go_still_meets_a_hold_that_arrived_after_pause(self):
        task,_=self.running_batch()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0):self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.end_batch(task,{'status':'error','error':'Sign in again.','error_code':'login_required'})
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(self.outstanding(),task)
        self.tick()
        value=self.state()
        self.assertEqual((value['status'],value['code']),('held','login_required'))

    def test_closing_interrupts_whatever_the_downloader_is_on_and_leaves_the_saved_download_running(self):
        library=self.workspace.library
        account_ui.interrupt(self.workspace)  # Nothing is running and nothing is configured.
        _,run=self.running_batch()
        before=self.state()
        with patch.object(account_ui.time,'sleep') as nap:account_ui.interrupt(self.workspace)
        nap.assert_not_called()
        self.assertTrue(RunCancellation(library,run).requested())
        self.assertEqual(self.state(),before)
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')

    CANCELLED={'archived_episode_ids':[],'downloaded_pages':3,'downloaded_bytes':30,'pending_episode_count':1,
               'inventory':{'scan_remaining_series_count':0}}

    def ticks(self):
        for _ in range(3):self.tick()

    def waiting_for_go(self):
        """What the page shows, and whether anything is queued for a downloader to run."""
        self.ticks()
        return (self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],
                [path.name for path in (self.queue/'requests').iterdir()])

    def test_a_start_of_the_app_sets_a_download_that_was_running_to_wait_for_go_and_go_continues_it(self):
        self.assertFalse(account_ui.reopen(self.workspace))  # A library that has never downloaded.
        task,run=self.running_batch()
        account_ui.interrupt(self.workspace)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.assertTrue(account_ui.reopen(self.workspace))
        # Stopped by the start itself: the cancelled receipt is not settled until the next tick.
        self.assertEqual(self.state()['status'],'stopped')
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.assertFalse(account_ui.reopen(self.workspace))  # It waits already.
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.ticks()
        value=self.state()
        self.assertEqual((value['status'],value['code']),('running',None))
        self.assertNotIn(self.outstanding(),(None,task))
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'running')

    def test_a_start_of_the_app_sets_a_download_waiting_to_try_again_to_wait_for_go(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.change("UPDATE state SET status='waiting', code='connection', wait_until=?",
                    (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
        self.assertTrue(account_ui.reopen(self.workspace))
        value=self.state()
        self.assertEqual((value['status'],value['code'],value['wait_until']),('stopped',None,None))
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.tick()
        self.assertEqual(len(list((self.queue/'requests').glob('*.json'))),1)

    def test_the_page_still_shows_the_download_when_its_record_is_damaged(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        (self.queue/download_control.STATE_FILE).write_bytes(b'PRIVATE damaged record')
        shown=self.registry.account_status(LOCAL_OWNER,self.workspace.id)
        self.assertEqual((shown['status'],shown['code']),('held','controller_failed'))
        self.assertNotIn('PRIVATE',json.dumps(shown))

    def test_pause_and_a_start_of_the_app_leave_a_library_that_has_never_downloaded_alone(self):
        for name in ('requests','responses'):(self.queue/name).rmdir()  # No downloader has run here yet.
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        self.assertFalse(account_ui.reopen(self.workspace))
        for name in ('requests',download_control.STATE_FILE):
            self.assertFalse((self.queue/name).exists(),name)

    def test_a_start_of_the_app_sets_an_interrupted_check_of_the_lists_to_wait_for_go_which_queues_it_again(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.tick()  # Reserved, then published.
        task=self.outstanding()
        self.end_batch(task,{'status':'ok','result':{'status':'cancelled',**self.CANCELLED}})
        self.assertTrue(account_ui.reopen(self.workspace))
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.assertEqual(self.state()['status'],'running')
        self.tick()
        again=self.outstanding()
        self.assertNotIn(again,(None,task))
        request=json.loads((self.queue/'requests'/(again+'.json')).read_text())
        self.assertEqual((request['operation'],request['args']['categories']),('sync',['subscribed']))

    def test_a_start_of_the_app_ends_unrun_the_task_a_power_cut_left_queued_so_no_downloader_works_through_it(self):
        task,_=self.running_batch()  # No receipt, and the downloader's last word is that it was busy with it.
        request=self.queue/'requests'/(task+'.json');before=request.read_bytes()
        with patch.object(account_ui.time,'sleep') as nap:self.assertTrue(account_ui.reopen(self.workspace))
        nap.assert_not_called()
        receipt=self.queue/'responses'/(task+'.json');value=json.loads(receipt.read_bytes())
        # It was running, so what it used went unreported.
        self.assertEqual((value['task_id'],value['status'],value['result']['status'],value['result']['cancelled_before_start'],
                          value['result']['usage_unreported'],value['result']['downloaded_pages'],
                          value['result']['pending_episode_count']),(task,'ok','cancelled',False,True,0,1))
        self.assertEqual((receipt.stat().st_mode&0o777,request.read_bytes()),(PRIVATE_FILE,before))
        # The downloader that starts next removes a request that has its receipt, without running it.
        request.unlink();private_json(self.queue/'status.json',{'state':'ready'})
        self.assertEqual(self.waiting_for_go(),('stopped',[]))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.ticks()
        value=self.state()
        self.assertEqual((value['status'],value['code']),('running',None))
        self.assertNotIn(self.outstanding(),(None,task))

    def test_a_start_of_the_app_that_finds_a_downloader_still_running_stops_what_it_is_on_like_pause(self):
        task,run=self.running_batch()
        self.assertTrue(account_ui.reopen(self.workspace,idle=False))
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())
        self.assertFalse((self.queue/'responses'/(task+'.json')).exists())  # Its receipt is the downloader's to write.
        self.assertEqual(self.registry.account_status(LOCAL_OWNER,self.workspace.id)['status'],'stopped')

    def test_a_start_of_the_app_leaves_a_paused_download_as_it_is_and_still_ends_its_unrun_task(self):
        task,run=self.running_batch()
        self.registry.pause_account(LOCAL_OWNER,self.workspace.id)
        saved=self.state()
        self.assertFalse(account_ui.reopen(self.workspace,idle=False))
        self.assertFalse((self.queue/'responses'/(task+'.json')).exists())
        self.assertFalse(account_ui.reopen(self.workspace))  # Paused by its owner; the downloader died with the task.
        self.assertEqual(json.loads((self.queue/'responses'/(task+'.json')).read_bytes())['result']['status'],'cancelled')
        self.assertEqual(self.state(),saved)
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED}})
        self.assertFalse(account_ui.reopen(self.workspace))  # A receipt that is there is never written over.
        self.assertEqual(json.loads((self.queue/'responses'/(task+'.json')).read_bytes())['result']['downloaded_pages'],3)

    def test_go_after_pause_resumes_a_download_whose_next_step_was_never_sent(self):
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        private_json(self.queue/'status.json',{'state':'busy','task_id':'job-'+'b'*24})
        self.tick()  # Reserved; the downloader is busy, so it waits to be sent.
        reserved=self.outstanding()
        self.assertIsNotNone(reserved);self.assertFalse(list((self.queue/'requests').glob('*.json')))
        self.assertEqual(self.registry.pause_account(LOCAL_OWNER,self.workspace.id)['status'],'stopped')
        self.tick()
        self.assertIsNone(self.outstanding())  # A step never sent is forgotten, not sent after Pause.
        private_json(self.queue/'status.json',{'state':'ready'})
        self.assertEqual(self.registry.start_account(LOCAL_OWNER,self.workspace.id)['status'],'running')
        self.tick()
        self.assertNotIn(self.outstanding(),(None,reserved))
        self.assertEqual(len(list((self.queue/'requests').glob('*.json'))),1)

    def test_closing_asks_again_while_a_batch_that_just_started_cannot_be_stopped_yet(self):
        _,run=self.running_batch()
        (self.workspace.library/'.archive-state/progress.json').unlink()
        with patch.object(account_ui,'PAUSE_WAIT_SECONDS',0),patch.object(account_ui.time,'sleep') as nap:
            account_ui.interrupt(self.workspace)
        nap.assert_not_called()
        self.assertFalse(RunCancellation(self.workspace.library,run).requested())
        naps=[]
        def nap(seconds):
            naps.append(seconds);job_progress(self.workspace.library,run,phase='archive')
        with patch.object(account_ui.time,'sleep',nap):account_ui.interrupt(self.workspace)
        self.assertEqual(naps,[0.25])
        self.assertTrue(RunCancellation(self.workspace.library,run).requested())

    def test_a_receipt_that_cannot_be_counted_is_set_aside_and_the_step_tried_again_once(self):
        task,run=self.running_batch()
        self.end_batch(task,{'status':'ok','result':{'run_id':run,'status':'cancelled',**self.CANCELLED,'downloaded_pages':10**9}})
        receipt=(self.queue/'responses'/(task+'.json')).read_bytes()
        self.assertTrue(account_ui.reopen(self.workspace))
        self.registry.start_account(LOCAL_OWNER,self.workspace.id)
        self.tick()
        value=self.state()
        self.assertEqual((value['status'],value['code'],value['failed_retries']),('waiting','idle',1))
        self.assertEqual((self.queue/'responses'/(task+'.json')).read_bytes(),receipt)
        later=datetime.now(timezone.utc)+timedelta(seconds=5)
        with patch('comic_archive.download_control._now',return_value=later):self.tick()
        self.assertNotIn(self.outstanding(),(None,task))
