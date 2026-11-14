"""Recovery seams across committed task intent, queue files, Pause and Go.

The controller and queue client are real. The fixtures interrupt publication or
write the same bounded receipt a worker would, without contacting a source.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import account_ui, download_control as dc
from comic_archive.cancellation import RunCancellation
from comic_archive.inventory import Inventory
from comic_archive.jobs import job_progress
from comic_archive.queue_client import submit_job, task_status
from comic_archive.task_cancellation import queued_stop_requested
from comic_archive.worker import private_json
from comic_archive.workspace import Workspace


class PublicationInterrupted(BaseException):
    """Simulated process loss that the worker's ordinary failure handler cannot swallow."""


class DownloadRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.workspace = Workspace('default', Path(work.name))
        self.session, self.library = self.workspace.session, self.workspace.library
        self.queue = self.session / 'worker'
        for folder in ('requests', 'responses'):
            (self.queue / folder).mkdir(parents=True)
        private_json(self.queue / 'status.json', {'state': 'ready'})
        with Inventory(self.library) as inventory:
            inventory.observe('42', 'Fixture', [{'id': '81', 'title': 'One', 'order': 1,
                                               'access_state': 'tapas_free'}], 'fixture')
        account_ui.choose_speed(self.workspace, 'medium')
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        clock = patch.object(dc, '_now', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tick(self):
        dc.worker_tick(self.session, self.library)

    def published(self):
        self.tick()
        requests = list((self.queue / 'requests').glob('*.json'))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        return request, json.loads(request.read_text())

    def task_row(self, task):
        db = sqlite3.connect(self.queue / dc.STATE_FILE)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            db.close()

    def interrupt_publication(self):
        account_ui.start(self.workspace)
        written = []
        def publish_then_interrupt(*args, **options):
            result = submit_job(*args, **options)
            written.append(result['task_id'])
            raise PublicationInterrupted()
        with patch.object(dc, 'submit_job', side_effect=publish_then_interrupt):
            with self.assertRaises(PublicationInterrupted):
                self.tick()
        self.assertEqual(len(written), 1)
        task = written[0]
        request = self.queue / 'requests' / (task + '.json')
        self.assertTrue(request.is_file())
        self.assertEqual(dc.outstanding(self.session, self.library), task)
        self.assertIsNone(self.task_row(task)['finished_at'])
        return request

    def assert_task_stopped(self, task):
        response = self.queue / 'responses' / (task + '.json')
        if response.exists():
            receipt = task_status(self.session, task)
            self.assertEqual((receipt['task_id'], receipt['status'], receipt['result']['status']),
                             (task, 'ok', 'cancelled'))
        else:
            self.assertEqual(dc.outstanding(self.session, self.library), task)
            self.assertTrue(queued_stop_requested(self.session, task))

    def test_idle_reopen_cancels_a_request_written_before_publication_was_committed(self):
        request = self.interrupt_publication()
        task, before = request.stem, request.read_bytes()
        self.now += timedelta(seconds=1)
        self.assertTrue(account_ui.reopen(self.workspace, idle=True))
        self.assertEqual(account_ui.status(self.workspace)['status'], 'stopped')
        self.assertEqual(request.read_bytes(), before)
        receipt = task_status(self.session, task)
        self.assertEqual((receipt['task_id'], receipt['status'], receipt['result']['status']),
                         (task, 'ok', 'cancelled'))
        self.assertTrue(receipt['result']['cancelled_before_start'])
        response = self.queue / 'responses' / request.name
        retained = response.read_bytes()
        self.tick()
        # The real worker discards a request once it has its terminal receipt.
        request.unlink()
        self.now += timedelta(seconds=1)
        account_ui.start(self.workspace)
        following, _ = self.published()
        self.assertNotEqual(following.stem, task)
        self.assertEqual(response.read_bytes(), retained)

    def test_pause_keeps_custody_of_a_request_written_before_publication_was_committed(self):
        request = self.interrupt_publication()
        task, before = request.stem, request.read_bytes()
        seen = []
        with patch.object(account_ui, 'PAUSE_WAIT_SECONDS', 0):
            account_ui.pause(self.workspace, on_stop_requested=lambda: seen.append(
                account_ui.status(self.workspace)['status']))
        self.assertEqual(seen, ['stopped'])
        self.assertEqual(request.read_bytes(), before)
        self.assert_task_stopped(task)
        self.tick()
        self.assertEqual([p.stem for p in (self.queue / 'requests').glob('*.json')], [task])

    def test_busy_task_with_an_unreadable_receipt_remains_owned_and_can_be_paused(self):
        account_ui.start(self.workspace)
        request, _ = self.published()
        task, before = request.stem, request.read_bytes()
        with Inventory(self.library) as inventory:
            run = inventory.start_run()
        job_progress(self.library, run, phase='archive')
        private_json(self.queue / 'status.json', {'state': 'busy', 'task_id': task, 'pid': os.getpid()})
        response = self.queue / 'responses' / request.name
        response.write_text('{broken')
        self.tick()
        self.assertEqual(dc.outstanding(self.session, self.library), task)
        self.assertIsNone(self.task_row(task)['finished_at'])
        self.assertEqual(request.read_bytes(), before)
        self.assertEqual(response.read_text(), '{broken')
        with patch.object(account_ui, 'PAUSE_WAIT_SECONDS', 0):
            account_ui.pause(self.workspace)
        self.assertEqual(account_ui.status(self.workspace)['status'], 'stopped')
        self.assertTrue(RunCancellation(self.library, run).requested()
                        or queued_stop_requested(self.session, task))
        self.assertEqual(dc.outstanding(self.session, self.library), task)
        self.assertEqual(response.read_text(), '{broken')

    def paused_receipt(self, request, *, source=False, retry_after=None):
        result = {'status': 'paused', 'run_id': 'a' * 24, 'downloaded_pages': 0, 'downloaded_bytes': 0,
                  'pending_episode_count': 1, 'archived_episode_ids': [],
                  'inventory': {'scan_remaining_series_count': 0},
                  'pause_reason': 'source_refused' if source else 'network_unavailable'}
        if retry_after is not None:
            result['retry_after_seconds'] = retry_after
        if source:
            result.update(source_http_status=403, source_response_kind='html', source_retry_episode_id='81')
        else:
            result['source_http_status'] = 503
        response = self.queue / 'responses' / request.name
        receipt = {'task_id': request.stem, 'status': 'error' if source else 'ok', 'result': result}
        if source:
            receipt['error_code'] = 'source_refused'
        private_json(response, receipt)
        request.unlink()
        self.tick()
        return response

    def resume(self, *, reopen=False):
        self.now += timedelta(seconds=1)
        if reopen:
            account_ui.reopen(self.workspace, idle=True)
        else:
            with patch.object(account_ui, 'PAUSE_WAIT_SECONDS', 0):
                account_ui.pause(self.workspace)
        self.assertEqual(account_ui.status(self.workspace)['status'], 'stopped')
        self.now += timedelta(seconds=1)
        account_ui.start(self.workspace)

    def assert_original_deadline(self, deadline, failed_request, response):
        retained = response.read_bytes()
        self.tick()
        self.assertEqual(list((self.queue / 'requests').glob('*.json')), [])
        self.now = deadline - timedelta(seconds=1)
        self.tick()
        self.assertEqual(list((self.queue / 'requests').glob('*.json')), [])
        self.now = deadline
        following, _ = self.published()
        self.assertNotEqual(following.stem, failed_request.stem)
        self.assertEqual(response.read_bytes(), retained)

    def test_source_retry_after_survives_pause_then_go(self):
        account_ui.start(self.workspace)
        request, _ = self.published()
        deadline = self.now + timedelta(seconds=300)
        response = self.paused_receipt(request, source=True, retry_after=300)
        self.resume()
        self.assert_original_deadline(deadline, request, response)

    def test_connection_retry_after_survives_pause_then_go(self):
        account_ui.start(self.workspace)
        request, _ = self.published()
        deadline = self.now + timedelta(seconds=300)
        response = self.paused_receipt(request, retry_after=300)
        self.resume()
        self.assert_original_deadline(deadline, request, response)

    def test_source_retry_after_survives_idle_reopen_then_go(self):
        account_ui.start(self.workspace)
        request, _ = self.published()
        deadline = self.now + timedelta(seconds=300)
        response = self.paused_receipt(request, source=True, retry_after=300)
        self.resume(reopen=True)
        self.assert_original_deadline(deadline, request, response)

    def test_held_server_deadline_survives_go_then_pause_go_and_reopen_go(self):
        account_ui.start(self.workspace)
        request, _ = self.published()
        for _ in range(dc.SOURCE_PROBES):
            self.paused_receipt(request, source=True)
            retry = account_ui.status(self.workspace)['retry']
            self.now += timedelta(seconds=retry['seconds'])
            request, _ = self.published()
        deadline = self.now + timedelta(seconds=300)
        response = self.paused_receipt(request, source=True, retry_after=300)
        self.assertEqual(account_ui.status(self.workspace)['status'], 'held')
        self.now += timedelta(seconds=1)
        account_ui.start(self.workspace)
        self.resume()
        self.resume(reopen=True)
        self.assert_original_deadline(deadline, request, response)
