"""What the apps see of a download, through the one door they use (account_ui via the registry).

The worker, its queue, the jobs and the library are real; only the network is the synthetic Tapas of the
integration tests. Nothing here reads a controller's own files, so these tests hold whichever controller
the worker ticks.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import account_ui
from comic_archive.archive import verify
from comic_archive.errors import AuthenticationError
from comic_archive.inventory import Inventory
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError
from comic_archive.run_receipts import failure_result
from comic_archive.worker import controller_ticks, execute, private_json
from test_account_export_integration import OfflineAdapter, library_archives

KEYS = {'supported', 'configured', 'coverage_verified', 'speed', 'status', 'phase', 'code', 'lists',
        'elapsed_seconds', 'progress_age_seconds', 'between_steps_seconds', 'retry', 'go_after_seconds', 'audit'}
STATUSES = {'running', 'held', 'stopped', 'finished'}
PHASES = ['existing_backlog', 'lists', 'series', 'archive', 'verify', 'finished']
RETRY_REASONS = {'connection', 'source', 'images', 'idle'}


class DownloadContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        previous = Path.cwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous)
        self.session = self.project / '.comic-session'
        self.library = self.project / 'Library'
        self.queue = self.session / 'worker'
        (self.queue / 'requests').mkdir(parents=True)
        (self.queue / 'responses').mkdir()
        private_json(self.queue / 'status.json', {'state': 'ready'})
        private_json(self.session / 'state.json', {'cookies': [], 'origins': []})
        with Inventory(self.library):
            pass
        self.registry = LocalWorkspaceRegistry(self.project)
        self.workspace = self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id)
        self.adapter = OfflineAdapter()
        self.adapter.prefetch_episode = lambda url: None
        self.executed = []
        self.seen = []
        # The synthetic network is not the fast importer's; the medium speed runs every step through it.
        self.registry.choose_speed(LOCAL_OWNER, self.registry.workspace_id, 'medium')

    def status(self):
        value = self.registry.account_status(LOCAL_OWNER, self.registry.workspace_id)
        self.check_shape(value)
        self.seen.append(value)
        return value

    def check_shape(self, value):
        self.assertLessEqual(set(value), KEYS, value)
        self.assertIs(value['supported'], True)
        self.assertIs(value['coverage_verified'], False)
        self.assertIn(value['speed'], account_ui.SPEEDS)
        if not value['configured']:
            self.assertNotIn(value.get('status'), {'running', 'held'})
            return
        self.assertIn(value['status'], STATUSES, value)
        self.assertIn(value.get('phase'), PHASES, value)
        if value['status'] == 'held':
            self.assertIsInstance(value.get('code'), str, value)
        elif value.get('code') is not None:
            self.fail(f'A code without a hold: {value}')
        if 'lists' in value:
            lists = value['lists']
            self.assertLessEqual(0, lists['checked'])
            self.assertLessEqual(lists['checked'], lists['total'])
        if 'retry' in value:
            self.assertEqual(value['status'], 'running')
            self.assertIn(value['retry']['reason'], RETRY_REASONS)
            self.assertIs(type(value['retry']['seconds']), int)
        for key in ('elapsed_seconds', 'progress_age_seconds', 'between_steps_seconds', 'go_after_seconds'):
            if key in value:
                self.assertIs(type(value[key]), int, key)
                self.assertGreaterEqual(value[key], 0, key)
        if 'audit' in value:
            self.assertTrue(all(type(n) is int and n >= 0 for n in value['audit'].values()), value['audit'])

    def tick(self):
        for _, tick in controller_ticks():
            tick(self.session)

    def run_one(self):
        """The worker runs the request that is waiting, as it would between two ticks; False when none waits."""
        for path in sorted((self.queue / 'requests').glob('*.json')):
            receipt = self.queue / 'responses' / path.name
            if receipt.exists():
                path.unlink()
                continue
            request = json.loads(path.read_text())
            try:
                with patch('builtins.print'):
                    response = {'task_id': path.stem, 'status': 'ok', 'result': execute(self.adapter, request)}
            except ArchiveError as error:
                response = {'task_id': path.stem, 'status': 'error', 'error': str(error),
                            'error_code': 'login_required' if isinstance(error, AuthenticationError) else 'job_failed'}
                result = failure_result(error)
                if result is not None:
                    response['result'] = result
            private_json(receipt, response)
            path.unlink()
            self.executed.append(request['operation'])
            return True
        return False

    def drive(self, until=lambda state: state['status'] in ('held', 'finished', 'stopped'), steps=200):
        for _ in range(steps):
            self.tick()
            state = self.status()
            if until(state):
                return state
            self.run_one()
        self.fail(f'The download did not get there: {self.seen[-1]}')

    def go(self):
        value = self.registry.start_account(LOCAL_OWNER, self.registry.workspace_id)
        self.check_shape(value)
        return value

    def images(self):
        return len([p for host, p in self.adapter.transport.calls if host == 'us-a.tapas.io'])

    def assert_saved_whole_account(self, state):
        self.assertEqual(state['status'], 'finished', state)
        self.assertEqual(state['audit']['verified_inventory_episode_count'], 3)
        self.assertEqual(state['audit']['missing_recorded_accessible_episode_count'], 0)
        self.assertEqual({p.suffix for p in library_archives(self.library)}, {'.cbz', '.epub'})
        self.assertFalse(any(path in ('/episode/83', '/episode/92') for _, path in self.adapter.transport.calls))
        self.assertEqual(verify(self.library, decode_images=True), [])

    def test_a_library_that_never_downloaded_says_only_that_and_pause_or_reopen_change_nothing(self):
        state = self.status()
        self.assertFalse(state['configured'])
        self.assertEqual(state['speed'], 'medium')
        asked = []
        account_ui.pause(self.workspace, on_stop_requested=lambda: asked.append(True))
        self.assertEqual(asked, [True])
        self.assertFalse(account_ui.reopen(self.workspace, idle=True))
        self.tick()
        self.assertFalse(list((self.queue / 'requests').iterdir()))
        self.assertFalse(self.status()['configured'])

    def test_go_saves_the_whole_account_reading_every_list_then_checking_then_auditing(self):
        self.assertEqual(self.go()['status'], 'running')
        state = self.drive()
        self.assert_saved_whole_account(state)
        phases = [PHASES.index(s['phase']) for s in self.seen if s.get('phase')]
        self.assertEqual(phases, sorted(phases), 'The round went back a stage')
        totals = {s['lists']['total'] for s in self.seen if 'lists' in s}
        self.assertEqual(len(totals), 1)
        self.assertTrue(any(s['lists']['checked'] == s['lists']['total'] for s in self.seen if 'lists' in s))
        self.assertEqual(self.executed[-1], 'audit_account')
        self.assertEqual(self.images(), 4)

    def test_a_second_go_while_running_starts_nothing_more(self):
        self.go()
        self.tick()
        self.tick()
        published = sorted(p.name for p in (self.queue / 'requests').glob('*.json'))
        self.assertEqual(len(published), 1)
        self.assertEqual(self.go()['status'], 'running')
        self.tick()
        self.assertEqual(sorted(p.name for p in (self.queue / 'requests').glob('*.json')), published)

    def test_pause_after_every_step_then_go_still_saves_everything_once(self):
        self.go()
        for _ in range(60):
            self.tick()
            if not self.run_one():
                if self.status()['status'] == 'finished':
                    break
                continue
            asked = []
            account_ui.pause(self.workspace, on_stop_requested=lambda: asked.append(self.status()['status']))
            self.assertEqual(asked, ['stopped'])
            for _ in range(3):
                self.tick()
                self.assertFalse(list((self.queue / 'requests').glob('*.json')), 'Published while paused')
            self.assertIn(self.status()['status'], ('stopped', 'finished'))
            if self.status()['status'] == 'finished':
                break
            self.assertEqual(self.go()['status'], 'running')
        self.assert_saved_whole_account(self.status())
        self.assertEqual(self.images(), 4)

    def test_a_start_of_the_app_in_the_middle_waits_for_go_and_go_finishes_without_repeating_work(self):
        self.go()
        self.drive(until=lambda state: state.get('phase') == 'series')
        for _ in range(6):
            if list((self.queue / 'requests').glob('*.json')):
                break
            self.tick()
        self.assertEqual(len(list((self.queue / 'requests').glob('*.json'))), 1)
        self.assertTrue(account_ui.reopen(self.workspace, idle=True))
        self.assertEqual(self.status()['status'], 'stopped')
        self.run_one()  # The task the app ended unrun is discarded, not run.
        self.tick()
        self.assertFalse(list((self.queue / 'requests').glob('*.json')))
        self.assertEqual(self.go()['status'], 'running')
        self.assert_saved_whole_account(self.drive())
        self.assertEqual(self.images(), 4)

    def test_an_expired_sign_in_holds_and_go_after_signing_in_again_finishes(self):
        self.adapter.transport.account = None
        self.go()
        state = self.drive()
        self.assertEqual((state['status'], state['code']), ('held', 'login_required'))
        calls = list(self.adapter.transport.calls)
        for _ in range(3):
            self.tick()
            self.run_one()
        self.assertEqual(self.adapter.transport.calls, calls, 'A held download reached the site')
        self.adapter.transport.account = 11
        self.assertEqual(self.go()['status'], 'running')
        self.assert_saved_whole_account(self.drive())

    def test_a_damaged_saved_file_holds_at_the_audit_and_go_after_repair_finishes(self):
        self.go()
        self.drive(until=lambda state: state.get('phase') == 'verify')
        path = library_archives(self.library)[0]
        original = path.read_bytes()
        path.write_bytes(b'corrupt archive')
        state = self.drive()
        self.assertEqual((state['status'], state['code']), ('held', 'archive_incomplete'))
        path.write_bytes(original)
        self.go()
        self.assert_saved_whole_account(self.drive())


if __name__ == '__main__':
    unittest.main()
