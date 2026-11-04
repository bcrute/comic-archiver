"""The 0.3.0 download controller on its own: its record, each receipt it settles, Pause, a start of the app, Go.

The queue folders and receipts are real files; the worker is these tests writing receipts as it would, and the
clock is the controller's own, moved by hand. What the apps see is held separately (test_download_contract).
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import account_ui, download_control as dc
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.worker import private_json
from comic_archive.workspace import Workspace
from download_harness import status_problems

TAPAS_LISTS = ['subscribed', 'updated', 'recent', 'free_episodes']


def counted(**values):
    """A download or check receipt's result that can be counted."""
    return {'status': 'finished', 'archived_episode_ids': [], 'downloaded_pages': 0, 'downloaded_bytes': 0,
            'pending_episode_count': 0, **values}


class Controller(unittest.TestCase):
    """A project with a ready downloader and an empty library, and the helpers to look at both."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.workspace = Workspace('default', self.project)
        self.session, self.library = self.workspace.session, self.workspace.library
        self.queue = self.session / 'worker'
        (self.queue / 'requests').mkdir(parents=True)
        (self.queue / 'responses').mkdir()
        private_json(self.queue / 'status.json', {'state': 'ready', 'cookie': 'PRIVATE'})
        with Inventory(self.library):
            pass
        self.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        clock = patch('comic_archive.download_control._now', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    # The record and the queue, as these tests see them.

    def later(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def db(self):
        return dc._connect(self.queue)

    def state(self):
        db = self.db()
        try:
            return dc._state(db)
        finally:
            db.close()

    def change(self, sql, *args):
        db = self.db()
        try:
            db.execute(sql, args)
        finally:
            db.close()

    def tasks(self):
        db = self.db()
        try:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY reserved_at, rowid')]
        finally:
            db.close()

    def lists(self):
        db = self.db()
        try:
            return {row['category']: dict(row) for row in db.execute('SELECT * FROM lists')}
        finally:
            db.close()

    def go(self):
        self.later()
        dc.start(self.session, self.library)

    def tick(self):
        dc.worker_tick(self.session, self.library)

    def requests(self):
        return sorted(p.stem for p in (self.queue / 'requests').glob('*.json'))

    def published(self):
        """The one request the controller has out, after a tick: (task id, request)."""
        self.tick()
        names = self.requests()
        self.assertEqual(len(names), 1, names)
        return names[0], json.loads((self.queue / 'requests' / (names[0] + '.json')).read_text())

    def answer(self, response, task=None):
        """The worker ran the request and wrote its receipt."""
        task = task or self.requests()[0]
        private_json(self.queue / 'responses' / (task + '.json'), {'task_id': task, **response})
        (self.queue / 'requests' / (task + '.json')).unlink(missing_ok=True)
        self.later()
        return task

    def ok(self, result):
        return self.answer({'status': 'ok', 'result': result})

    def error(self, code, result=None, message='PRIVATE failure'):
        response = {'status': 'error', 'error': message}
        if code is not None:
            response['error_code'] = code
        if result is not None:
            response['result'] = result
        return self.answer(response)

    def status(self):
        value = account_ui.status(self.workspace)
        self.assertEqual(status_problems(value), [], value)
        return value

    def pending_episode(self, key='81'):
        with Inventory(self.library) as inventory:
            inventory.observe('42', 'Fixture', [{'id': key, 'title': 'One', 'order': 1, 'access_state': 'tapas_free'}],
                              'fixture')

    def downloading(self):
        """Go with an episode waiting: the first step is a download."""
        self.pending_episode()
        self.go()
        task, request = self.published()
        self.assertEqual(request['operation'], 'archive_discovered')
        return task, request

    def read_lists(self, lists=TAPAS_LISTS):
        for category in lists:
            _, request = self.published()
            self.assertEqual(request['args']['categories'], [category])
            self.ok(counted(refresh={'reading_lists': [{'category': category, 'complete': True,
                                                        'observed_series_count': 2}]}))

    def held(self, folder):
        """Another writer (a thread of its own, as locks are shared within one) holds the folder until released."""
        import threading
        from comic_archive.locking import LibraryLock
        taken, release = threading.Event(), threading.Event()

        def hold():
            with LibraryLock(folder):
                taken.set()
                release.wait(10)
        thread = threading.Thread(target=hold)
        thread.start()
        taken.wait(10)

        def done():
            release.set()
            thread.join()
        return done


class ControllerTests(Controller):
    # The record itself.

    def test_go_makes_a_private_record_with_the_sites_lists_and_reads_the_first_one(self):
        self.assertEqual(dc.status(self.session, self.library), {'configured': False})
        self.assertFalse((self.queue / dc.STATE_FILE).exists())
        self.tick()  # No record, nothing is made.
        self.assertFalse((self.queue / dc.STATE_FILE).exists())
        self.go()
        self.assertEqual((self.queue / dc.STATE_FILE).stat().st_mode & 0o777, PRIVATE_FILE)
        self.assertEqual(list(self.lists()), TAPAS_LISTS)
        value = self.state()
        self.assertEqual((value['status'], value['stage'], value['code']), ('running', 'lists', None))
        task, request = self.published()
        self.assertEqual(request, {'operation': 'sync', 'args': dc.LIST_READ | {'categories': ['subscribed']}})
        self.assertEqual(self.tasks()[0]['kind'], 'list')
        self.assertEqual(dc.outstanding(self.session, self.library), task)

    def test_the_record_refuses_rows_that_break_its_rules(self):
        self.go()
        db = self.db()
        try:
            for sql in ("UPDATE state SET status='held', code=NULL",
                        "UPDATE state SET status='held', code='PRIVATE'",
                        "UPDATE state SET status='waiting', code='idle', wait_until=NULL",
                        "UPDATE state SET status='waiting', code='login_required', wait_until='2026-11-02T00:00:00+00:00'",
                        "UPDATE state SET status='sleeping'",
                        "UPDATE state SET stage='elsewhere'",
                        "UPDATE state SET idle_waits=-1",
                        "UPDATE state SET source_delay=5",
                        "INSERT INTO state (id, status, stage, round_started_at, stretch_started_at, updated_at) "
                        "VALUES (2, 'running', 'lists', 'x', 'x', 'x')",
                        "INSERT INTO tasks (task_id, kind, operation, options, reserved_at) "
                        "VALUES ('PRIVATE', 'list', 'sync', '{}', 'x')",
                        "INSERT INTO tasks (task_id, kind, operation, options, reserved_at) "
                        "VALUES ('job-" + 'a' * 24 + "', 'list', 'delete_everything', '{}', 'x')",
                        "UPDATE lists SET complete=2"):
                with self.subTest(sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            # Only one step is ever out.
            for letter in 'ab':
                if letter == 'b':
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("INSERT INTO tasks (task_id, kind, operation, options, reserved_at) "
                                   "VALUES (?, 'list', 'sync', '{}', 'x')", ('job-' + letter * 24,))
                else:
                    db.execute("INSERT INTO tasks (task_id, kind, operation, options, reserved_at) "
                               "VALUES (?, 'list', 'sync', '{}', 'x')", ('job-' + letter * 24,))
        finally:
            db.close()

    def test_a_record_from_another_version_or_damaged_holds_and_go_sets_it_aside_and_starts_again(self):
        self.go()
        self.published()
        task = "INSERT INTO tasks (task_id, kind, operation, options, reserved_at) VALUES (?, 'list', 'sync', ?, 'x')"
        for damage, sql, *args in (('version', 'PRAGMA user_version = 7'), ('bytes', None),
                                   ('value', "UPDATE state SET round_started_at='PRIVATE'"),
                                   ('episode', "UPDATE state SET source_episode='PRIVATE'"),
                                   ('audit', 'UPDATE state SET audit=?', '["PRIVATE"]'),
                                   ('task', task, 'job-' + 'PRIVATE' * 3 + 'PRI', '{}'),
                                   ('options', task, 'job-' + 'a' * 24, '["PRIVATE"]')):
            with self.subTest(damage):
                path = self.queue / dc.STATE_FILE
                if sql is None:
                    for extra in ('-wal', '-shm'):
                        Path(str(path) + extra).unlink(missing_ok=True)
                    path.write_bytes(b'PRIVATE not a database')
                else:
                    self.change(sql, *args)
                before = path.read_bytes()
                shown = self.status()
                self.assertEqual((shown['status'], shown['code']), ('held', 'controller_failed'))
                self.assertNotIn('PRIVATE', json.dumps(shown))
                self.tick()
                self.assertEqual(path.read_bytes(), before)
                self.assertIsNone(dc.source_recovery(self.session, self.library))
                self.assertIsNone(dc.outstanding(self.session, self.library))
                dc.request_stop(self.session, self.library)  # Pause on a damaged record changes nothing.
                self.assertFalse(dc.reopen(self.session, self.library))
                self.assertEqual(path.read_bytes(), before)
                self.go()
                self.assertEqual(self.state()['status'], 'running')
                self.assertEqual(self.status()['status'], 'running')
                for name in self.requests():  # What the damaged record had out is answered first.
                    self.answer({'status': 'ok', 'result': {'status': 'cancelled'}}, name)
        aside = [p for p in self.queue.glob(dc.STATE_FILE + '.damaged-*') if not p.name.endswith(('-wal', '-shm'))]
        self.assertEqual(len(aside), 3)  # The newest three of seven.
        with patch('comic_archive.download_control.sqlite3.connect', side_effect=sqlite3.OperationalError('PRIVATE')):
            self.assertEqual(self.status()['code'], 'controller_failed')

    def test_a_record_with_no_download_in_it_is_not_configured_and_does_nothing(self):
        dc._connect(self.queue, create=True).close()
        self.tick()
        self.assertEqual(self.requests(), [])
        self.assertEqual(dc.status(self.session, self.library), {'configured': False})
        self.go()
        self.assertEqual(self.published()[1]['args']['categories'], ['subscribed'])

    def test_a_library_with_nothing_saved_yet_starts_by_reading_the_first_list(self):
        for suffix in ('', '-wal', '-shm'):
            (self.library / ('.archive-state/inventory.sqlite3' + suffix)).unlink(missing_ok=True)
        self.go()
        self.assertEqual(self.published()[1]['args']['categories'], ['subscribed'])

    def test_a_step_left_queued_by_a_record_set_aside_runs_before_anything_new_is_sent(self):
        self.go()
        task, _ = self.published()
        (self.queue / dc.STATE_FILE).write_bytes(b'damaged')
        self.go()
        self.tick()
        self.assertEqual(self.requests(), [task])  # Nothing sent while a request waits.
        self.answer({'status': 'ok', 'result': counted()}, task)
        task2, _ = self.published()
        self.assertNotEqual(task2, task)

    def test_a_symlinked_record_or_queue_is_refused_and_nothing_is_written(self):
        self.go()
        path = self.queue / dc.STATE_FILE
        real = self.project / 'elsewhere.sqlite3'
        os.replace(path, real)
        path.symlink_to(real)
        with self.assertRaises(ArchiveError):
            dc.start(self.session, self.library)
        self.tick()
        self.assertFalse(self.requests())
        path.unlink()
        os.replace(real, path)
        (self.queue / 'requests').rmdir()
        (self.project / 'requests').mkdir()
        (self.queue / 'requests').symlink_to(self.project / 'requests')
        with self.assertRaises(ArchiveError):
            dc.start(self.session, self.library)
        self.tick()
        self.assertFalse(list((self.project / 'requests').iterdir()))

    def test_a_download_outside_its_project_folder_is_refused(self):
        with self.assertRaisesRegex(ArchiveError, 'Library and .comic-session'):
            dc.start(self.session, self.project / 'Other')

    # A round: lists, series, saving, the audit.

    def test_a_round_reads_each_list_then_checks_series_then_saves_then_audits_then_finishes(self):
        with Inventory(self.library) as inventory:
            inventory.observe('42', 'Fixture', [], 'subscribed')
            inventory.scan_candidates()  # The first list read starts the round of series checks.
        self.go()
        self.read_lists()
        _, request = self.published()
        self.assertEqual(self.status()['lists'], {'checked': 4, 'total': 4})
        self.assertEqual(request['args'], dc.SERIES_CHECK | {'metadata_speed': 'fast'})
        self.assertEqual(self.state()['stage'], 'series')
        self.assertEqual(self.status()['phase'], 'series')
        with Inventory(self.library) as inventory:
            inventory.record_series_scan('42')
            inventory.finish_scan_cycle()
        self.pending_episode()
        self.ok(counted(refresh={'series_checks_completed': 1}))
        _, request = self.published()
        self.assertEqual(request['operation'], 'archive_discovered')
        self.assertEqual(self.state()['stage'], 'archive')
        with Inventory(self.library) as inventory:
            inventory.mark('81', 'archived')
        self.ok(counted(archived_episode_ids=['81'], downloaded_pages=1, downloaded_bytes=10))
        _, request = self.published()
        self.assertEqual(request, {'operation': 'audit_account', 'args': {}})
        self.assertEqual(self.status()['phase'], 'verify')
        self.ok({'verified_inventory_episode_count': 1, 'missing_recorded_accessible_episode_count': 0,
                 'inventory_index_problem_count': 0, 'unindexed_archive_count': 0, 'archive_integrity_passed': True,
                 'episode_id': 'PRIVATE'})
        self.tick()
        shown = self.status()
        self.assertEqual((shown['status'], shown['phase']), ('finished', 'finished'))
        self.assertEqual(shown['audit']['verified_inventory_episode_count'], 1)
        self.assertFalse(self.requests())
        # Go after a finished round starts the next: every list is read again.
        self.go()
        self.assertTrue(all(row['read_at'] is None for row in self.lists().values()))
        self.assertEqual(self.published()[1]['args']['categories'], ['subscribed'])

    def test_episodes_already_waiting_are_saved_before_the_lists_are_read(self):
        self.pending_episode()
        self.go()
        self.assertEqual(self.published()[1]['operation'], 'archive_discovered')
        self.assertEqual(self.status()['phase'], 'existing_backlog')

    def test_a_list_read_that_could_not_be_confirmed_still_counts_so_the_round_moves_on(self):
        self.go()
        self.published()
        self.ok(counted(refresh={'reading_lists': 'PRIVATE'}))
        self.tick()
        row = self.lists()['subscribed']
        self.assertEqual((row['read_at'] is not None, row['complete'], row['observed_series_count']), (True, 0, 0))
        self.assertEqual(self.published()[1]['args']['categories'], ['updated'])

    def test_an_audit_that_finds_something_missing_holds_and_go_audits_again(self):
        self.go()
        self.read_lists()
        self.published()
        self.ok({'verified_inventory_episode_count': 1, 'missing_recorded_accessible_episode_count': 2,
                 'inventory_index_problem_count': 0, 'unindexed_archive_count': 0, 'archive_integrity_passed': True})
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'archive_incomplete'))
        self.assertEqual(self.status()['audit']['missing_recorded_accessible_episode_count'], 2)
        self.go()
        self.assertNotIn('audit', self.status())
        self.assertEqual(self.published()[1]['operation'], 'audit_account')

    def test_an_audit_receipt_that_cannot_be_read_is_tried_once_more_then_holds(self):
        self.go()
        self.read_lists()
        self.published()
        self.ok({'status': 'paused'})
        self.tick()
        self.assertEqual(self.state()['failed_retries'], 1)
        self.later(dc.FAILED_WAIT)
        self.published()
        self.ok({'status': 'paused'})
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'task_result_invalid'))

    # Settling receipts: progress, waits and holds.

    def test_progress_moves_on_and_forgives_every_earlier_retry(self):
        self.downloading()
        self.change('UPDATE state SET idle_waits=2, network_waits=3, source_probes=1, failed_retries=1')
        self.ok(counted(status='limited', archived_episode_ids=['81'], downloaded_pages=2, downloaded_bytes=20,
                        pending_episode_count=1))
        task, _ = self.published()
        value = self.state()
        self.assertEqual((value['status'], value['idle_waits'], value['network_waits'], value['source_probes'],
                          value['failed_retries']), ('running', 0, 0, 0, 0))
        done = self.tasks()[0]
        self.assertEqual((done['outcome'], done['pages'], done['bytes'], done['episodes'], done['progressed']),
                         ('ok', 2, 20, 1, 1))

    def test_steps_that_save_nothing_wait_longer_each_time_then_hold(self):
        self.downloading()
        for seconds in dc.IDLE_WAITS:
            self.ok(counted(status='limited', pending_episode_count=1))
            self.tick()
            shown = self.status()
            self.assertEqual(shown['retry'], {'reason': 'idle', 'seconds': seconds})
            self.tick()
            self.assertFalse(self.requests())  # Nothing is sent while it waits.
            self.later(seconds)
            self.published()
        self.ok(counted(status='limited', pending_episode_count=1))
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'no_progress'))
        self.go()
        self.assertEqual(self.state()['idle_waits'], 0)
        self.published()

    def test_images_that_did_not_arrive_are_waited_for_then_tried_again(self):
        self.downloading()
        self.ok(counted(status='limited', pending_episode_count=1, asset_retry_after_seconds=90))
        self.tick()
        self.assertEqual(self.status()['retry'], {'reason': 'images', 'seconds': 90})
        self.later(90)
        self.published()

    def test_a_lost_connection_waits_longer_each_time_and_never_holds(self):
        self.downloading()
        for seconds in (*dc.CONNECTION_WAITS, 900, 900):
            self.ok(counted(status='paused', pause_reason='network_unavailable'))
            self.tick()
            self.assertEqual(self.status()['retry'], {'reason': 'connection', 'seconds': seconds})
            self.later(seconds)
            self.published()
        # The downloader's own longer wait wins.
        self.ok(counted(status='paused', pause_reason='network_unavailable', retry_after_seconds=1200))
        self.tick()
        self.assertEqual(self.status()['retry']['seconds'], 1200)

    def test_a_connection_this_device_may_not_use_is_tried_again_every_five_minutes(self):
        self.downloading()
        for _ in range(6):
            self.ok(counted(status='paused', pause_reason='network_policy'))
            self.tick()
            self.assertEqual((self.state()['code'], self.status()['retry']),
                             ('network_policy', {'reason': 'connection', 'seconds': 300}))
            self.later(dc.POLICY_WAIT)
            self.published()

    def test_cloud_troubles_that_pass_are_waited_out_and_the_rest_hold_with_their_own_code(self):
        for code in sorted(dc.CLOUD_WAITS):
            with self.subTest(code):
                self.setUp()
                self.downloading()
                self.error(code)
                self.tick()
                self.assertEqual(self.status()['retry']['reason'], 'connection')
        for code in ('cloud_space', 'b2_key_permissions', 'cloud_unknown'):
            with self.subTest(code):
                self.setUp()
                self.downloading()
                self.error(code)
                self.tick()
                self.assertEqual((self.state()['status'], self.state()['code']), ('held', code))

    def test_an_expired_sign_in_holds_until_go(self):
        task, request = self.downloading()
        self.error('login_required')
        self.tick()
        self.assertEqual((self.status()['status'], self.status()['code']), ('held', 'login_required'))
        for _ in range(3):
            self.later(3600)
            self.tick()
        self.assertFalse(self.requests())
        self.go()
        self.assertEqual(self.published()[1], request)

    def test_a_failed_step_is_tried_once_more_under_a_new_identity_then_holds(self):
        first, request = self.downloading()
        self.error('job_failed')
        self.tick()
        self.assertEqual(self.status()['retry'], {'reason': 'idle', 'seconds': dc.FAILED_WAIT})
        self.later(dc.FAILED_WAIT)
        second, again = self.published()
        self.assertNotEqual(second, first)
        self.assertEqual(again, request)
        self.error(None)
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'task_failed'))
        self.assertNotIn(b'PRIVATE', (self.queue / dc.STATE_FILE).read_bytes())

    def test_a_step_paused_for_a_reason_it_does_not_know_counts_as_failed(self):
        self.downloading()
        self.ok(counted(status='paused', pause_reason='PRIVATE'))
        self.tick()
        value = self.state()
        self.assertEqual((value['status'], value['code'], value['failed_retries']), ('waiting', 'idle', 1))
        self.assertEqual(self.status()['retry']['seconds'], dc.FAILED_WAIT)

    def test_a_step_reserved_before_a_hold_is_forgotten_never_sent(self):
        self.go()
        private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': 'job-' + 'b' * 24})
        self.tick()
        self.assertEqual(len(self.tasks()), 1)
        self.change("UPDATE state SET status='held', code='task_failed'")
        private_json(self.queue / 'status.json', {'state': 'ready'})
        self.tick()
        self.assertEqual((self.tasks(), self.requests()), ([], []))

    def test_a_receipt_that_cannot_be_counted_is_left_as_it_is_tried_once_more_then_holds(self):
        for bad in ({'status': 'ok', 'result': ['PRIVATE']},
                    {'status': 'ok', 'result': counted(downloaded_pages=dc.DOWNLOAD['max_pages'] + 1)},
                    {'status': 'ok', 'result': counted(archived_episode_ids=['PRIVATE'])},
                    {'status': 'ok', 'result': counted(downloaded_bytes=True)},
                    {'status': 'ok', 'result': counted(status='PRIVATE')},
                    {'status': 'maybe'}):
            with self.subTest(bad):
                self.setUp()
                self.downloading()
                task = self.answer(bad)
                path = self.queue / 'responses' / (task + '.json')
                before = path.read_bytes()
                self.tick()
                self.assertEqual((self.state()['failed_retries'], path.read_bytes()), (1, before))
                self.later(dc.FAILED_WAIT)
                self.published()
                self.answer(bad)
                self.tick()
                self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'task_result_invalid'))

    def test_a_step_whose_request_and_receipt_vanished_is_sent_again_once_then_holds(self):
        task, request = self.downloading()
        (self.queue / 'requests' / (task + '.json')).unlink()
        self.tick()
        self.assertEqual(self.requests(), [task])  # The same step, under the same identity.
        (self.queue / 'requests' / (task + '.json')).unlink()
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'task_unavailable'))
        self.assertEqual(self.tasks()[0]['outcome'], 'vanished')

    def test_a_refused_episode_is_tried_alone_with_doubling_waits_then_holds_and_go_tries_it_first(self):
        refused = {'status': 'paused', 'pause_reason': 'source_refused', 'source_http_status': 403,
                   'source_response_kind': 'html', 'source_retry_episode_id': '81'}
        self.downloading()
        delays = []
        for _ in range(dc.SOURCE_PROBES + 1):
            self.error('source_refused', counted(**refused))
            self.tick()
            if self.state()['status'] == 'held':
                break
            shown = self.status()
            delays.append(shown['retry']['seconds'])
            self.assertEqual(shown['retry']['reason'], 'source')
            self.assertEqual(dc.source_recovery(self.session, self.library)['episode_id'], '81')
            self.later(shown['retry']['seconds'])
            _, request = self.published()
            self.assertEqual((request['args']['retry_episode_id'], request['args']['limit']), ('81', 1))
        self.assertEqual(delays, [30, 60, 120, 240])
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'source_refused'))
        self.go()
        value = self.state()
        self.assertEqual((value['source_probes'], value['source_delay'], value['source_episode']), (0, 480, '81'))
        self.assertEqual(self.published()[1]['args']['retry_episode_id'], '81')
        # The probe got through: the download goes on and the refused episode is no longer singled out.
        self.ok(counted(archived_episode_ids=['81'], downloaded_pages=1, downloaded_bytes=1,
                        reader_probe_episode_id='81', reader_probe_succeeded=True))
        _, request = self.published()
        self.assertNotIn('retry_episode_id', request['args'])
        self.assertIsNone(self.state()['source_episode'])

    def test_a_probe_that_reaches_another_episode_holds_as_changed_access(self):
        self.downloading()
        self.error('source_refused', counted(status='paused', pause_reason='source_refused', source_http_status=403,
                                             source_response_kind='html', source_retry_episode_id='81'))
        self.tick()
        self.later(30)
        self.published()
        self.ok(counted(reader_probe_episode_id='82', reader_probe_succeeded=True))
        self.tick()
        self.assertEqual((self.state()['status'], self.state()['code']), ('held', 'source_access_changed'))

    def test_a_refusal_with_nothing_to_probe_waits_five_fifteen_sixty_minutes_then_holds_past_the_sites_wait(self):
        self.downloading()
        for seconds in dc.REFUSAL_WAITS:
            self.error('source_refused', counted(status='paused', pause_reason='source_refused', source_http_status=429))
            self.tick()
            self.assertEqual(self.status()['retry'], {'reason': 'source', 'seconds': seconds})
            self.later(seconds)
            self.published()
        self.error('source_refused', counted(status='paused', pause_reason='source_refused', source_http_status=429,
                                             retry_after_seconds=7200))
        self.tick()
        shown = self.status()
        self.assertEqual((shown['status'], shown['code'], shown['go_after_seconds']), ('held', 'source_refused', 7200))
        # Go cannot cut the site's wait short; it starts when the wait ends.
        self.go()
        shown = self.status()
        self.assertEqual((shown['status'], shown['retry']['reason']), ('running', 'source'))
        self.tick()
        self.assertFalse(self.requests())
        self.later(7200)
        self.published()

    def test_a_refusal_waits_at_least_as_long_as_the_site_asked(self):
        self.downloading()
        self.error('source_refused', counted(status='paused', pause_reason='source_refused', source_http_status=429,
                                             retry_after_seconds=1000))
        self.tick()
        self.assertEqual(self.status()['retry']['seconds'], 1000)

    def test_a_cancelled_step_stops_the_download_unless_go_came_after_it(self):
        self.downloading()
        self.ok(counted(status='cancelled', pending_episode_count=1))
        self.tick()
        self.assertEqual(self.status()['status'], 'stopped')
        self.go()
        task, _ = self.published()
        dc.request_stop(self.session, self.library)
        self.go()  # Go before the stopped step's receipt is settled.
        self.ok(counted(status='cancelled', pending_episode_count=1))
        again, _ = self.published()
        self.assertNotEqual(again, task)
        self.assertEqual(self.state()['status'], 'running')

    # Pause, a start of the app, Go.

    def test_pause_stops_and_forgets_a_step_never_sent_but_leaves_one_out_to_answer(self):
        self.go()
        private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': 'job-' + 'b' * 24})
        self.tick()
        self.assertEqual(len(self.tasks()), 1)
        self.assertFalse(self.requests())
        dc.request_stop(self.session, self.library)
        self.assertEqual(self.tasks(), [])
        private_json(self.queue / 'status.json', {'state': 'ready'})
        self.tick()
        self.assertFalse(self.requests())
        self.go()
        task, _ = self.published()
        dc.request_stop(self.session, self.library)
        self.assertEqual((self.state()['status'], dc.outstanding(self.session, self.library)), ('stopped', task))
        for _ in range(3):
            self.later(3600)
            self.tick()
        self.assertEqual(self.requests(), [task])  # Nothing new while paused.
        self.ok(counted(refresh={'reading_lists': [{'category': 'subscribed', 'complete': True,
                                                    'observed_series_count': 1}]}))
        self.tick()
        self.assertEqual((self.state()['status'], self.requests()), ('stopped', []))
        self.assertIsNotNone(self.lists()['subscribed']['read_at'])  # What it did still counts.

    def test_pause_on_a_wait_stops_it(self):
        self.downloading()
        self.ok(counted(status='paused', pause_reason='network_unavailable'))
        self.tick()
        dc.request_stop(self.session, self.library)
        value = self.state()
        self.assertEqual((value['status'], value['code'], value['wait_until']), ('stopped', None, None))
        self.later(3600)
        self.tick()
        self.assertFalse(self.requests())

    def test_go_from_each_status(self):
        self.downloading()
        self.change("UPDATE state SET idle_waits=2, network_waits=2, failed_retries=1, source_delay=240, "
                    "source_probes=3, source_episode='81'")
        for status, code in (('stopped', None), ('held', 'no_progress'), ('held', 'login_required')):
            with self.subTest(status=status, code=code):
                self.change('UPDATE state SET status=?, code=?, wait_until=NULL', status, code)
                self.go()
                value = self.state()
                self.assertEqual((value['status'], value['code'], value['idle_waits'], value['network_waits'],
                                  value['failed_retries'], value['source_probes'], value['source_delay'],
                                  value['source_episode']), ('running', None, 0, 0, 0, 0, 240, '81'))
                self.assertEqual(value['stretch_started_at'], self.now.isoformat())
        # Go on a download that is going or waiting changes nothing.
        self.change("UPDATE state SET status='waiting', code='connection', wait_until=?",
                    (self.now + timedelta(minutes=5)).isoformat())
        before = self.state()
        self.go()
        self.assertEqual(self.state(), before)
        # Go after a finished download starts a new round: every list is read again.
        self.change("UPDATE state SET status='finished', code=NULL, stage='finished', wait_until=NULL")
        self.change('UPDATE lists SET read_at=?, complete=1, observed_series_count=2', self.now.isoformat())
        self.go()
        value = self.state()
        self.assertEqual((value['status'], value['stage'], value['round_started_at']),
                         ('running', 'lists', self.now.isoformat()))
        self.assertEqual([row['read_at'] for row in self.lists().values()], [None] * len(TAPAS_LISTS))

    def test_a_start_of_the_app_sets_a_going_download_to_wait_for_go_and_ends_its_unsent_or_unrun_step(self):
        self.assertFalse(dc.reopen(self.session, self.library))  # Never downloaded.
        task, _ = self.downloading()
        self.assertTrue(dc.reopen(self.session, self.library))
        self.assertEqual(self.state()['status'], 'stopped')
        receipt = json.loads((self.queue / 'responses' / (task + '.json')).read_text())
        self.assertEqual((receipt['status'], receipt['result']['status']), ('ok', 'cancelled'))
        self.assertFalse(dc.reopen(self.session, self.library))
        # A step only reserved is forgotten.
        self.go()
        (self.queue / 'requests' / (task + '.json')).unlink(missing_ok=True)
        private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': 'job-' + 'b' * 24})
        self.tick()
        self.tick()
        reserved = dc.outstanding(self.session, self.library)
        self.assertIsNotNone(reserved)
        self.assertTrue(dc.reopen(self.session, self.library))
        self.assertIsNone(dc.outstanding(self.session, self.library))
        self.assertFalse((self.queue / 'responses' / (reserved + '.json')).exists())

    def test_a_start_of_the_app_with_its_downloader_still_running_leaves_the_step_to_it(self):
        task, _ = self.downloading()
        self.assertTrue(dc.reopen(self.session, self.library, idle=False))
        self.assertFalse((self.queue / 'responses' / (task + '.json')).exists())
        self.assertEqual(self.requests(), [task])

    # The record stays small and private.

    def test_history_is_trimmed_to_a_month_and_a_fixed_number_of_steps(self):
        self.downloading()
        db = self.db()
        try:
            old = (self.now - timedelta(days=dc.HISTORY_DAYS + 1)).isoformat()
            db.executemany("INSERT INTO tasks (task_id, kind, operation, options, reserved_at, published_at, finished_at) "
                           "VALUES (?, 'download', 'archive_discovered', '{}', ?, ?, ?)",
                           [(f'job-{n:024x}', old, old, old) for n in range(5)])
        finally:
            db.close()
        with patch.object(dc, 'HISTORY_ROWS', 3):
            self.ok(counted(downloaded_pages=1, archived_episode_ids=['81'], downloaded_bytes=1))
            self.later(2 * 86400)
            self.tick()
        rows = self.tasks()
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(all(row['finished_at'] is None or row['finished_at'] > old for row in rows))

    def test_a_tick_that_fails_unexpectedly_holds_the_download_and_never_raises(self):
        self.downloading()
        with patch.object(dc, '_step', side_effect=RuntimeError('PRIVATE')):
            self.tick()
        self.assertEqual((self.status()['status'], self.status()['code']), ('held', 'controller_failed'))
        self.assertNotIn(b'PRIVATE', (self.queue / dc.STATE_FILE).read_bytes())

    def test_a_busy_record_or_library_is_left_for_the_next_tick(self):
        self.go()
        release = self.held(self.queue / '.download-state')
        self.tick()
        release()
        self.assertEqual(self.tasks(), [])
        release = self.held(self.library)
        self.tick()
        self.tick()
        release()
        self.assertEqual(len(self.tasks()), 1)  # Reserved, but not sent while another writer has the library.
        self.assertFalse(self.requests())
        self.published()

    def test_nothing_is_sent_while_any_request_waits_or_the_downloader_is_not_ready(self):
        self.go()
        private_json(self.queue / 'requests' / ('job-' + 'c' * 24 + '.json'), {'operation': 'cloud_upload', 'args': {}})
        self.tick()
        self.assertEqual(self.requests(), ['job-' + 'c' * 24])
        (self.queue / 'requests' / ('job-' + 'c' * 24 + '.json')).unlink()
        for worker in ({'state': 'busy'}, {'state': 'starting'}, 'PRIVATE'):
            private_json(self.queue / 'status.json', worker)
            self.tick()
            self.assertFalse(self.requests())
        (self.queue / 'status.json').write_text('{broken')
        self.tick()
        self.assertFalse(self.requests())
        private_json(self.queue / 'status.json', {'state': 'ready'})
        self.published()

    def test_the_status_keeps_the_apps_contract_in_every_state(self):
        self.assertEqual(self.status()['configured'], False)
        self.downloading()
        self.status()
        for status, code, until in (('waiting', 'idle', 60), ('waiting', 'network_policy', 60), ('held', 'no_progress', None),
                                    ('stopped', None, None), ('finished', None, None)):
            with self.subTest(status):
                self.change('UPDATE state SET status=?, code=?, wait_until=?', status, code,
                            None if until is None else (self.now + timedelta(seconds=until)).isoformat())
                shown = self.status()
                self.assertEqual(shown['status'], 'running' if status == 'waiting' else status)

    def test_the_fast_speed_reads_series_quickly_and_the_others_at_the_ordinary_pace(self):
        for speed, pace in (('fast', 'fast'), ('medium', 'normal'), ('slow', 'normal')):
            account_ui.choose_speed(self.workspace, speed)
            options, metadata = dc._pace(self.session)
            self.assertEqual(metadata, pace)
            self.assertEqual(options.get('speed', 'normal'), pace)
            self.assertEqual({k: options[k] for k in dc.DOWNLOAD}, dc.DOWNLOAD)


class MoveTests(Controller):
    """The first open after the update from 0.2: the old controllers' download becomes this one's record."""

    def forget(self):
        """The new record is gone again, as before the update."""
        for suffix in ('', '-wal', '-shm'):
            (self.queue / (dc.STATE_FILE + suffix)).unlink(missing_ok=True)

    def export(self, **values):
        """The account download's old record, as 0.2 wrote it."""
        value = {'schema_version': 1, 'status': 'stopped', 'phase': 'lists', 'code': None, 'category_index': 0,
                 'started_at': '2026-10-30T08:00:00+00:00', 'list_checks': [], 'audit': None, 'pending': None,
                 'window_budgets': {'archive_only': True, 'refresh_lists': False}, **values}
        private_json(self.queue / 'account-export.json', value)

    def backlog(self, **values):
        private_json(self.queue / 'backlog.json', {'schema_version': 1, 'status': 'stopped', 'code': None,
                                                    'pending': None, 'source_recovery_delay': 30, **values})

    def check(self, category, complete=True, count=3):
        return {'category': category, 'complete': complete, 'boundary_observed': complete,
                'observed_series_count': count}

    def old_task(self, operation='sync', options=None, published=True):
        task = 'job-' + os.urandom(12).hex()
        options = {'categories': ['updated'], **dc.LIST_READ} if options is None else options
        if published:
            private_json(self.queue / 'requests' / (task + '.json'), {'operation': operation, 'args': options})
        return {'task_id': task, 'operation': operation, 'options': options, 'before_remaining': 0}

    def kept(self):
        """The old files, as moved aside: {name: path}."""
        places = list((self.queue / 'legacy').iterdir())
        self.assertEqual(len(places), 1, places)
        return {path.name: path for path in places[0].iterdir()}

    def test_an_old_finished_download_moves_in_finished_with_its_counts_and_the_old_files_are_kept_aside(self):
        audit = {key: 0 for key in dc.AUDIT_KEYS} | {'verified_inventory_episode_count': 140,
                                                     'coverage_blockers': ['PRIVATE'], 'archive_integrity_passed': True}
        self.export(status='finished', phase='finished', audit=audit, category_index=4,
                    list_checks=[self.check(c) for c in TAPAS_LISTS])
        private_json(self.queue / 'completion.json', {'schema_version': 1, 'status': 'known_backlog_drained'})
        self.backlog()
        (self.queue / 'backlog-history').mkdir()
        private_json(self.queue / 'backlog-history' / 'window.json', {'note': 'PRIVATE'})
        shown = self.status()
        self.assertEqual((shown['status'], shown['phase']), ('finished', 'finished'))
        self.assertEqual(shown['audit'], {key: audit[key] for key in dc.AUDIT_KEYS})
        self.assertEqual(sorted(self.kept()), ['account-export.json', 'backlog-history', 'backlog.json', 'completion.json'])
        for name in dc.OLD_FILES:
            self.assertFalse((self.queue / name).exists())
        for folder in (self.queue / 'legacy', *(self.queue / 'legacy').iterdir()):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.queue / dc.STATE_FILE).stat().st_mode & 0o777, PRIVATE_FILE)
        self.assertNotIn(b'PRIVATE', (self.queue / dc.STATE_FILE).read_bytes())
        self.assertEqual({c: row['complete'] for c, row in self.lists().items()}, dict.fromkeys(TAPAS_LISTS, 1))
        self.assertFalse(dc.reopen(self.session, self.library))
        # Go starts a new round, as after any finished download.
        self.go()
        _, request = self.published()
        self.assertEqual(request['args']['categories'], ['subscribed'])

    def test_a_download_that_needed_a_sign_in_still_does_and_anything_else_waits_for_go(self):
        for export, shown in (({'status': 'held', 'code': 'login_required'}, ('held', 'login_required')),
                              ({'status': 'held', 'code': 'window_changed'}, ('stopped', None)),
                              ({'status': 'held', 'code': 'scan_no_progress', 'phase': 'series'}, ('stopped', None)),
                              ({'status': 'running'}, ('stopped', None)),
                              (None, ('stopped', None))):
            with self.subTest(export):
                self.forget()
                if export is None:  # Only a plan of windows, from before the account download.
                    private_json(self.queue / 'completion.json', {'schema_version': 1, 'status': 'running'})
                else:
                    self.export(**export)
                value = self.status()
                self.assertEqual((value['status'], value.get('code')), shown)
                self.assertTrue(value['configured'])
                self.assertIsNone(self.state()['wait_until'])

    def test_where_the_round_had_got_to_is_kept(self):
        for phase, checks, stage, read in (
                ('existing_backlog', [], 'lists', []),
                ('lists', [self.check('subscribed'), self.check('updated', complete=False, count=0)], 'lists', ['subscribed']),
                ('series', [self.check(c) for c in TAPAS_LISTS], 'series', TAPAS_LISTS),
                ('archive', [], 'archive', TAPAS_LISTS),
                ('verify', [], 'verify', TAPAS_LISTS),
                ('finished', [], 'verify', TAPAS_LISTS),  # Finished but not marked so: audited again.
                ('a later phase', [self.check('subscribed')], 'lists', [])):
            with self.subTest(phase):
                self.forget()
                self.export(phase=phase, list_checks=checks, category_index=len(checks))
                self.status()
                self.assertEqual(self.state()['stage'], stage)
                rows = self.lists()
                self.assertEqual(list(rows), TAPAS_LISTS)
                self.assertEqual([c for c, row in rows.items() if row['read_at']], read)
                if phase == 'lists':
                    self.assertEqual((rows['subscribed']['complete'], rows['subscribed']['observed_series_count']), (1, 3))
                if phase == 'archive':  # Read before, with nothing known of how.
                    self.assertEqual((rows['recent']['complete'], rows['recent']['observed_series_count']), (None, None))
        # From the list it stopped at, Go reads the lists not yet read.
        self.forget()
        self.export(phase='lists', list_checks=[self.check('subscribed')], category_index=1)
        self.go()
        self.assertEqual(self.published()[1]['args']['categories'], ['updated'])

    def test_a_step_already_sent_is_carried_with_its_identity_and_one_only_reserved_is_dropped(self):
        sent = self.old_task()
        self.export(phase='lists', list_checks=[self.check('subscribed')], category_index=1, pending=sent)
        self.backlog(pending=dict(self.old_task('archive_discovered', {'limit': 100}, published=False), phase='reserved'))
        self.status()
        task, = self.tasks()
        self.assertEqual((task['task_id'], task['kind'], task['operation']), (sent['task_id'], 'list', 'sync'))
        self.assertEqual(json.loads(task['options']), sent['options'])
        self.assertIsNotNone(task['published_at'])
        self.assertEqual(dc.outstanding(self.session, self.library), sent['task_id'])
        # Go, and its receipt is settled as this controller's own: the list it read counts.
        self.go()
        self.tick()
        self.assertEqual(self.requests(), [sent['task_id']])
        self.ok(counted(refresh={'reading_lists': [self.check('updated')]}))
        _, request = self.published()
        self.assertEqual(request['args']['categories'], ['recent'])
        self.assertIsNotNone(self.lists()['updated']['read_at'])
        # A step only reserved, with nothing else out: nothing is carried, and Go reserves its own.
        self.forget()
        self.export(pending=self.old_task(published=False))
        self.status()
        self.assertEqual(self.tasks(), [])

    def test_each_kind_of_step_sent_is_known_again(self):
        for operation, options, kind in (('sync', {'categories': ['updated'], **dc.LIST_READ}, 'list'),
                                         ('sync', {'categories': ['elsewhere'], **dc.LIST_READ}, 'series'),
                                         ('sync', dict(dc.SERIES_CHECK), 'series'),
                                         ('archive_discovered', {'limit': 100}, 'download'),
                                         ('archive_discovered', {'limit': 1, 'retry_episode_id': '81'}, 'probe'),
                                         ('archive_discovered', {'limit': 1, 'retry_episode_id': 'x'}, 'download'),
                                         ('audit_account', {}, 'audit')):
            with self.subTest(kind=kind, options=options):
                self.forget()
                for path in (self.queue / 'requests').glob('*.json'):
                    path.unlink()
                self.backlog(pending=dict(self.old_task(operation, options), phase='queued'))
                self.status()
                self.assertEqual([task['kind'] for task in self.tasks()], [kind])
        for pending in ({'task_id': 'job-1', 'operation': 'sync', 'options': {}}, ['PRIVATE'],
                        dict(self.old_task(), operation='delete_everything'), dict(self.old_task(), options=[])):
            with self.subTest(pending=pending):
                self.forget()
                self.backlog(pending=pending)
                self.status()
                self.assertEqual(self.tasks(), [])

    def test_when_both_old_controllers_had_a_step_out_the_accounts_is_kept_and_the_other_ended_unrun(self):
        account, plan = self.old_task(), self.old_task('archive_discovered', {'limit': 100})
        self.export(pending=account)
        self.backlog(pending=dict(plan, phase='queued'))
        self.status()
        self.assertEqual([task['task_id'] for task in self.tasks()], [account['task_id']])
        receipt = json.loads((self.queue / 'responses' / (plan['task_id'] + '.json')).read_text())
        self.assertEqual(receipt['result']['status'], 'cancelled')
        # One the downloader is on is left to end by itself.
        self.forget()
        account, plan = self.old_task(), self.old_task('archive_discovered', {'limit': 100})
        self.export(pending=account)
        self.backlog(pending=dict(plan, phase='queued'))
        private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': plan['task_id']})
        self.status()
        self.assertFalse((self.queue / 'responses' / (plan['task_id'] + '.json')).exists())
        # The same step named by both is carried once.
        self.forget()
        account = self.old_task()
        self.export(pending=account)
        self.backlog(pending=dict(account, phase='queued'))
        self.status()
        self.assertEqual([task['task_id'] for task in self.tasks()], [account['task_id']])
        # A queue that cannot be answered does not stop the move; the other step is then an orphan, run first.
        self.forget()
        (self.queue / 'status.json').unlink()
        account, plan = self.old_task(), self.old_task('archive_discovered', {'limit': 100})
        self.export(pending=account)
        self.backlog(pending=dict(plan, phase='queued'))
        with patch('comic_archive.task_cancellation.end_unstarted', side_effect=ArchiveError('PRIVATE')):
            self.status()
        self.assertEqual([task['task_id'] for task in self.tasks()], [account['task_id']])
        self.assertIn(plan['task_id'], self.requests())

    def test_a_start_of_the_app_ends_a_carried_step_no_downloader_got_to(self):
        sent = self.old_task('archive_discovered', {'limit': 100})
        self.export(status='running', phase='archive', pending=sent)
        self.assertFalse(account_ui.reopen(self.workspace))  # Already waiting for Go once moved.
        receipt = json.loads((self.queue / 'responses' / (sent['task_id'] + '.json')).read_text())
        self.assertEqual(receipt['result']['status'], 'cancelled')
        self.go()
        self.tick()
        self.assertEqual(self.tasks()[0]['outcome'], 'ok')
        self.assertEqual(self.state()['status'], 'running')

    def test_the_readers_learned_delay_and_the_refused_episode_are_carried(self):
        self.export(phase='archive')
        self.backlog(status='paused', code='source_refused', source_recovery_delay=240, source_recovery_episode='81')
        self.status()
        value = self.state()
        self.assertEqual((value['source_delay'], value['source_episode']), (240, '81'))
        self.go()
        self.assertEqual(self.published()[1]['args']['retry_episode_id'], '81')
        for delay, episode in ((5, '0'), (10**6, 'PRIVATE'), ('240', 81)):
            with self.subTest(delay=delay):
                self.forget()
                self.backlog(source_recovery_delay=delay, source_recovery_episode=episode)
                self.status()
                value = self.state()
                self.assertEqual((value['source_delay'], value['source_episode']), (dc.MIN_SOURCE_DELAY, None))

    def test_old_files_that_cannot_be_read_are_moved_aside_unread(self):
        (self.queue / 'account-export.json').write_text('{"status": "finished", PRIVATE')
        (self.queue / 'backlog.json').write_text('["PRIVATE"]')
        (self.queue / 'completion.json').write_bytes(b'{' * (dc.OLD_LIMIT + 1))
        self.pending_episode()
        shown = self.status()
        self.assertEqual((shown['status'], shown['phase']), ('stopped', 'existing_backlog'))
        self.assertEqual(sorted(self.kept()), sorted(dc.OLD_FILES))
        self.assertEqual(self.tasks(), [])
        self.export(status='finished', started_at='not a time')  # Found after the move: no longer read.
        self.assertEqual(self.status()['status'], 'stopped')
        # A round's start that cannot be read, or is still to come, is the time of the move.
        for started in ('2026-10-30T08:00:00', '2027-01-01T00:00:00+00:00', 5):
            with self.subTest(started=started):
                self.forget()
                self.export(started_at=started)
                self.status()
                self.assertEqual(dc._instant(self.state()['round_started_at']), self.now)
        self.forget()
        self.export()
        self.status()
        self.assertEqual(self.state()['round_started_at'], '2026-10-30T08:00:00+00:00')

    def test_a_move_another_process_is_making_is_left_to_it(self):
        self.export(status='finished')
        for lock in ('.download-state', *dc.OLD_LOCKS):
            with self.subTest(lock):
                release = self.held(self.queue / lock)
                try:
                    self.assertEqual(dc.status(self.session, self.library), {'configured': False})
                    self.assertTrue((self.queue / 'account-export.json').exists())
                finally:
                    release()
        self.assertEqual(self.status()['status'], 'finished')

    def test_a_move_that_cannot_be_written_holds_and_leaves_the_old_files(self):
        self.export(status='finished')
        with patch('comic_archive.download_control.sqlite3.connect', side_effect=sqlite3.OperationalError('PRIVATE')):
            self.assertEqual(self.status()['code'], 'controller_failed')
            with self.assertRaises(ArchiveError):
                self.go()
        self.assertTrue((self.queue / 'account-export.json').exists())
        self.assertFalse((self.queue / (dc.STATE_FILE + '.moving')).exists())
        self.assertFalse((self.queue / dc.STATE_FILE).exists())
        self.assertEqual(self.status()['status'], 'finished')

    def test_a_symlinked_place_for_the_old_files_is_refused(self):
        self.export(status='finished')
        elsewhere = self.project / 'elsewhere'
        elsewhere.mkdir()
        (self.queue / 'legacy').symlink_to(elsewhere)
        self.assertEqual(self.status()['code'], 'controller_failed')
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue((self.queue / 'account-export.json').exists())



class LogTests(Controller):
    """What the download writes in the log: each step and each change, enough to follow it in a log sent for support,
    by codes and counts only."""

    def logged(self, action, level='INFO'):
        with self.assertLogs(dc.LOG, level) as logs:
            action()
        return [record.getMessage() for record in logs.records]

    def test_go_each_step_each_wait_pause_and_a_start_of_the_app_are_logged_by_code(self):
        self.pending_episode()
        self.assertEqual(self.logged(self.go), ['download: begun (Go)'])
        task = self.logged(self.tick)
        self.assertEqual(task, [f'download: step {self.requests()[0]} (download) sent'])
        # A lost connection: how the step ended, then the wait, until when, and the tries it has had.
        task = self.ok(counted(status='paused', pause_reason='network_unavailable'))
        lines = self.logged(self.tick)
        self.assertEqual(lines[0], f'download: step {task} (download) ended ok paused network_unavailable: '
                                   'pages 0, bytes 0, episodes 0')
        self.assertRegex(lines[1], r'^download: running at lists -> waiting/connection at lists, '
                                   r'until 2026-11-02T00:0[0-9]:[0-9]{2}\+00:00, tries: network_waits 1$')
        self.later(3600)
        self.logged(self.tick)
        # A failed step: its code, never the worker's words.
        task = self.error('job_failed')
        lines = self.logged(self.tick)
        self.assertEqual(lines[0], f'download: step {task} (download) ended error job_failed')
        # The connection's try still counts: only progress forgives it.
        self.assertRegex(lines[1], r'-> waiting/idle at lists, until .*, tries: network_waits 1, failed_retries 1$')
        self.assertNotIn('PRIVATE', '\n'.join(lines))
        # The person's own steps are named even when nothing else would be logged.
        self.assertEqual(self.logged(lambda: dc.request_stop(self.session, self.library)),
                         ['download: waiting/idle at lists -> stopped at lists, tries: network_waits 1, failed_retries 1 '
                          '(Pause)'])
        self.assertEqual(self.logged(self.go), ['download: stopped at lists -> running at lists (Go)'])
        self.assertEqual(self.logged(self.go), ['download: Go while already running'])
        self.assertEqual(self.logged(lambda: dc.reopen(self.session, self.library))[0],
                         'download: running at lists -> stopped at lists (the app started)')

    def test_a_check_logs_what_it_counted_and_the_hold_it_leads_to(self):
        self.go()
        self.read_lists()
        task, _ = self.published()
        self.ok({'verified_inventory_episode_count': 1, 'missing_recorded_accessible_episode_count': 2,
                 'inventory_index_problem_count': 0, 'unindexed_archive_count': 0, 'archive_integrity_passed': True})
        self.assertEqual(self.logged(self.tick), [
            f'download: step {task} (audit) ended ok',
            'download: the check counted inventory_index_problem_count 0, missing_recorded_accessible_episode_count 2, '
            'unindexed_archive_count 0, verified_inventory_episode_count 1',
            'download: running at verify -> held/archive_incomplete at verify'])

    def test_a_controller_failure_is_logged_by_class_and_place_never_its_text(self):
        self.downloading()
        with patch.object(dc, '_step', side_effect=RuntimeError('PRIVATE')):
            line, = self.logged(self.tick, 'ERROR')
        self.assertRegex(line, r'^download: the controller failed: RuntimeError at [a-z_]+\.py:[0-9]+')
        self.assertNotIn('PRIVATE', line)

    def test_the_history_for_support_is_the_download_now_and_its_last_steps_oldest_first(self):
        self.assertIsNone(dc.history(self.session, self.library))
        first, _ = self.downloading()
        self.ok(counted(archived_episode_ids=['81'], downloaded_pages=3))
        self.tick()
        second = self.requests()[0]
        state, steps = dc.history(self.session, self.library)
        self.assertEqual((state['status'], state['stage']), ('running', 'lists'))
        self.assertEqual([step['task_id'] for step in steps], [first, second])
        self.assertEqual((steps[0]['outcome'], steps[0]['episodes'], steps[0]['pages'], steps[0]['progressed']),
                         ('ok', 1, 3, 1))
        self.assertIsNone(steps[1]['finished_at'])
        self.assertEqual([step['task_id'] for step in dc.history(self.session, self.library, 1)[1]], [second])
        (self.queue / dc.STATE_FILE).write_bytes(b'not a database')
        with self.assertRaises(dc.Damaged):
            dc.history(self.session, self.library)


if __name__ == '__main__':
    unittest.main()
