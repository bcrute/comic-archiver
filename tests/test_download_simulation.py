"""A download over weeks of simulated time, with the site, the network, the worker and the person misbehaving.

Seeded random walks through the apps' door (account_ui), the real worker jobs and a real library: outages,
refusals, expired sign-ins, failed and garbled receipts, a worker dying around its receipt, Pause, closing and
reopening the app, Go. After every step the rules every controller must keep are checked; at the end the faults
stop and Go must carry the download to the end of the account.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from random import Random
import tempfile
import unittest
from unittest.mock import patch

from comic_archive import account_ui
from comic_archive.inventory import Inventory
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.worker import controller_ticks, private_json
from download_harness import HOLDS, Account, Worker, state_files, status_problems, tick

SEEDS = range(6)
STEPS = 300
FAULTS = ['network', 'network', 'policy', 'refused', 'refused_reader', 'refused_reader', 'login', 'failed']
STATE_LIMIT = 2 * 1024 * 1024
STALL_TICKS = 6
GO_LIMIT = 4  # Go presses the end of a walk may need once the faults have stopped


class Walk:
    def __init__(self, test, seed):
        self.test, self.seed = test, seed
        self.random = Random(seed)
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.session = self.project / '.comic-session'
        self.library = self.project / 'Library'
        (self.session / 'worker/requests').mkdir(parents=True)
        (self.session / 'worker/responses').mkdir()
        private_json(self.session / 'state.json', {'cookies': [], 'origins': []})
        with Inventory(self.library):
            pass
        self.registry = LocalWorkspaceRegistry(self.project)
        self.workspace = self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id)
        self.clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.account = Account(self.random)
        self.account.now = lambda: self.clock
        self.worker = Worker(self.session, self.account)
        self.published = {}
        self.stopped_with = None
        self.stalled = 0
        self.drift = 0.0
        self.problems = {}
        self.step = 0

    def wall(self):
        """The clock as time.time() reads it: the walk's, creeping on so a wait for a stop runs out."""
        self.drift += 0.3
        return self.clock.timestamp() + self.drift

    def problem(self, kind, detail):
        self.problems.setdefault(kind, f'seed {self.seed} step {self.step}: {detail}')

    def status(self):
        try:
            value = account_ui.status(self.workspace)
        except Exception as error:  # noqa: BLE001 - the contract is that status never raises
            self.problem('status raised', type(error).__name__)
            return {}
        for issue in status_problems(value):
            self.problem('status shape', issue)
        return value

    def go(self):
        try:
            self.registry.start_account(LOCAL_OWNER, self.registry.workspace_id)
        except Exception as error:  # noqa: BLE001 - Go must get past the controller's own state
            self.problem('go raised', f'{type(error).__name__}: {error}')
        self.stopped_with = None

    def pause(self):
        account_ui.pause(self.workspace)
        self.stopped_with = {p.stem for p in self.worker.waiting()}

    def close_and_reopen(self, running):
        """The app closes (the downloader interrupted) and opens again, before or after its worker restarts."""
        account_ui.interrupt(self.workspace)
        if self.worker.claimed:
            self.worker.complete()
        try:
            account_ui.reopen(self.workspace, idle=not running)
        except Exception as error:  # noqa: BLE001
            self.problem('reopen raised', f'{type(error).__name__}: {error}')
        self.stopped_with = {p.stem for p in self.worker.waiting()}

    def tick(self):
        try:
            tick(self.session)
        except Exception as error:  # noqa: BLE001 - the worker survives this, but it is still wrong
            self.problem('tick raised', type(error).__name__)

    def run_worker(self, faults=True):
        if self.worker.claimed is None and self.random.random() < 0.9:
            self.worker.claim()
        if self.worker.claimed is None:
            return
        if faults and self.random.random() < 0.03:
            self.pause()  # Pause while the step runs: the worker is busy with it.
        r = self.random.random() if faults else 1
        self.worker.complete(leave_request=r < 0.02, vanish=0.02 <= r < 0.03, garble=0.03 <= r < 0.04)

    def check(self, state):
        waiting = self.worker.waiting()
        if len(waiting) > 1:
            self.problem('two requests', [p.stem for p in waiting])
        for path in waiting:
            try:
                content = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if self.published.setdefault(path.stem, content) != content:
                self.problem('identity reused', path.stem)
        if self.stopped_with is not None and state.get('status') == 'stopped':
            new = {p.stem for p in waiting} - self.stopped_with
            if new:
                self.problem('published while paused', sorted(new))
        if state.get('status') == 'held' and state.get('code') not in HOLDS:
            self.problem(f"hold {state.get('code')}", 'a hold the person cannot act on')
        idle = (state.get('status') == 'running' and 'retry' not in state and not waiting
                and self.worker.claimed is None)
        self.stalled = self.stalled + 1 if idle else 0
        if self.stalled >= STALL_TICKS:
            self.problem('stalled', f'running for {self.stalled} steps with nothing out and no wait: {state}')
        files = state_files(self.session)
        size = sum(p.stat().st_size for p in files)
        if size > STATE_LIMIT:
            self.problem('state grew', size)
        if any(b'PRIVATE' in p.read_bytes() for p in files):
            self.problem('private text saved', [p.name for p in files if b'PRIVATE' in p.read_bytes()])
        if state.get('status') == 'finished':
            with Inventory(self.library, read_only=True) as inventory:
                if inventory.pending():
                    self.problem('finished with episodes waiting', len(inventory.pending()))

    def person(self, state):
        status, code = state.get('status'), state.get('code')
        if status == 'held' and code == 'login_required' and self.account.fault and self.account.fault[0] == 'login':
            if self.random.random() < 0.3:
                self.account.fault = None  # Signed in again.
            return
        if status in ('held', 'stopped') and self.random.random() < 0.4:
            self.go()
        elif status == 'finished' and self.random.random() < 0.05:
            self.go()
        elif status == 'running' and self.random.random() < 0.02:
            self.pause()
        elif self.random.random() < 0.015:
            self.close_and_reopen(running=self.random.random() < 0.5)
        elif status != 'running' and self.random.random() < 0.05:
            account_ui.choose_speed(self.workspace, self.random.choice(sorted(account_ui.SPEEDS)))

    def run(self):
        account_ui.choose_speed(self.workspace, 'medium')
        self.go()
        for self.step in range(STEPS):
            self.clock += timedelta(seconds=self.random.choice([5, 30, 120, 600, 600, 3600]))
            if self.account.fault is None and self.random.random() < 0.08:
                kind = self.random.choice(FAULTS)
                self.account.fault = (kind, self.clock + timedelta(seconds=self.random.choice([60, 900, 3600, 14400])))
            self.person(self.status())
            self.tick()
            self.run_worker()
            self.tick()
            self.check(self.status())
        self.account.fault = None
        presses = 0
        for self.step in range(STEPS, STEPS + 600):
            self.clock += timedelta(seconds=120)
            state = self.status()
            if state.get('status') == 'finished':
                break
            if state.get('status') in ('held', 'stopped') or not state.get('configured'):
                presses += 1
                self.go()
            self.tick()
            self.run_worker(faults=False)
            self.tick()
            self.check(self.status())
        else:
            self.problem('never finished', self.status())
        if presses > GO_LIMIT:
            self.problem('needed Go too often', presses)
        with Inventory(self.library, read_only=True) as inventory:
            rows = {row['id']: row for row in inventory.db.execute('SELECT id,status,error_code,attempts FROM episodes')}
        missing = sorted(key for key in self.account.accessible()
                         if key not in rows or rows[key]['status'] != 'archived'
                         and not (rows[key]['status'] == 'failed' and rows[key]['attempts'] >= 3))
        if missing:
            self.problem('episodes never saved', missing[:5])
        return self.problems


@unittest.skipUnless(any(name == 'download' for name, _ in controller_ticks()),
                     'The stacked controllers fail these rules; the 0.3.0 controller is held to them (docs: road-to-0.3.0, A3).')
class DownloadSimulationTests(unittest.TestCase):
    def setUp(self):
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)

    def test_weeks_of_faults_keep_every_rule_and_go_still_finishes_the_account(self):
        problems = {}
        for seed in SEEDS:
            walk = Walk(self, seed)
            os.chdir(walk.project)
            now = lambda: walk.clock  # noqa: E731
            targets = [patch('comic_archive.batches._now', side_effect=now),
                       patch('time.time', side_effect=walk.wall),
                       patch('time.sleep'), patch('builtins.print')]
            try:
                import comic_archive.download_control  # noqa: F401 - the 0.3.0 controller, once it exists
                targets.append(patch('comic_archive.download_control._now', side_effect=now))
            except ImportError:
                pass
            for target in targets:
                target.start()
            try:
                for kind, example in walk.run().items():
                    problems.setdefault(kind, example)
            finally:
                for target in reversed(targets):
                    target.stop()
        self.assertEqual(problems, {}, '\n' + '\n'.join(f'{k}: {v}' for k, v in sorted(problems.items())))


if __name__ == '__main__':
    unittest.main()
