#!/usr/bin/env python3
"""Try to break Go, Pause and closing the app.

The dashboard server and its routes, the queue, the controllers and the
downloader process are the real ones. Only Tapas is replaced: the downloader
is started with the offline source below instead of a browser, so nothing here
contacts Tapas. The window is not part of this. "Closing" is `Runtime.stop`,
which is what the window calls, and "opening" is a new `Runtime`.

Every scenario gets a fresh library, ends by letting the download finish, and
then checks that the saved library is complete and verifies.

The libraries are made in memory (/dev/shm) unless --dir says otherwise, so
that the time limits measure the app and not the disk: every state file is
synced to disk when written, and on a busy disk one such write can take many
seconds, which makes Pause and closing take as long.

    python scripts/stop_resume_stress.py [--seed N] [--repeat N] [--dir FOLDER] [--only NAME ...] [--list]
"""
import argparse
from contextlib import contextmanager
import faulthandler
import json
import logging
import os
from pathlib import Path
import random
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import zlib

# The offline account: comics only, the last episode of each series locked.
SERIES = tuple(range(4201, 4207))
EPISODES = 9
IMAGES = 12
IMAGE_SECONDS, OTHER_SECONDS, STEADY_SCAN_SECONDS, SLOW_SCAN_SECONDS = .08, .02, .4, 6
FOLDER = 'TAPAS_STRESS_DIR'
HOOK = ('import os, sys\n'
        'if os.environ.get({folder!r}):\n'
        '    sys.path.insert(0, {scripts!r})\n'
        '    import stop_resume_stress\n'
        '    sys.path.pop(0)\n'
        '    stop_resume_stress.install()\n')


def png(color):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes([0, color % 256, 0, 0]))) + chunk(b'IEND', b''))


def envelope(value):
    return json.dumps({'data': value, 'error_details': None}).encode()


def locked(key):
    return key % 100 == EPISODES


class OfflineTransport:
    """Answers what the downloader asks Tapas, from memory, and writes every request to a log the driver reads.

    Files in the control folder change its behaviour while it runs: `fast`
    (no delays), `steady-scan` (series requests take long enough to be caught
    in the middle), `slow-scan` (one series request takes seconds) and `stuck`
    (an image request does not come back, like a dead connection).
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.log = os.open(self.folder / 'requests.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def close(self):
        os.close(self.log)

    def note(self, kind, path):
        os.write(self.log, f'{time.time():.3f} {os.getpid()} {kind} {path}\n'.encode())

    def flag(self, name):
        return (self.folder / name).exists()

    def wait(self, kind, path):
        self.note(kind, path)
        if kind == 'image' and self.flag('stuck'):
            limit = time.monotonic() + 120
            while self.flag('stuck') and time.monotonic() < limit:
                time.sleep(.05)
        elif self.flag('fast'):
            return
        elif kind == 'scan' and self.flag('slow-scan'):
            time.sleep(SLOW_SCAN_SECONDS)
        elif kind == 'scan' and self.flag('steady-scan'):
            time.sleep(STEADY_SCAN_SECONDS)
        else:
            time.sleep(IMAGE_SECONDS if kind == 'image' else OTHER_SECONDS)

    def info(self, key):
        return {'id': key, 'scene': key % 100, 'title': f'Episode {key % 100}', 'book': False, 'free': not locked(key),
                'unlocked': False, 'must_pay': False, 'scheduled': False, 'closing_date': None}

    def get(self, url, **options):
        value = urlsplit(url)
        if value.hostname == 'us-a.tapas.io' and value.path.startswith('/pc/'):
            self.wait('image', value.path)
            return png(int(value.path.rsplit('-', 1)[1].split('.')[0]))
        parts = value.path.strip('/').split('/')
        if value.hostname == 'tapas.io' and value.path == '/who-am-i':
            self.wait('other', value.path)
            return envelope({'user': {'id': 11}})
        if value.hostname == 'tapas.io' and len(parts) >= 2 and parts[1].isdigit():
            key = int(parts[1])
            if parts[0] == 'series' and key in SERIES:
                self.wait('scan', value.path)
                if len(parts) == 2:
                    return envelope({'id': key, 'title': f'Comic {key}', 'url': f'comic-{key}', 'comic': True, 'book': False})
                if parts[2:] == ['episodes']:
                    keys = [key * 100 + n for n in range(1, EPISODES + 1)]
                    return envelope({'episodes': [self.info(k) for k in keys],
                                     'pagination': {'page': 1, 'total': len(keys), 'has_next': False}})
            if parts[0] == 'episode' and key // 100 in SERIES and 1 <= key % 100 <= EPISODES:
                if parts[2:] == ['info']:
                    self.wait('other', value.path)
                    return envelope(self.info(key))
                if len(parts) == 2 and not locked(key):
                    self.wait('reader', value.path)
                    images = ''.join(f'<img class="content__img" src="https://us-a.tapas.io/pc/{key}-{n}.png">'
                                     for n in range(1, IMAGES + 1))
                    return f'<script>var seriesId={key // 100};</script><div id="episode-{key}">{images}</div>'.encode()
        self.note('UNEXPECTED', value.path)
        raise AssertionError('Unexpected offline request')


def install():
    """Runs inside the downloader process, before it starts: replace its browser with the offline source."""
    from comic_archive import fast_import, worker
    from comic_archive.adapters.tapas_http import TapasHTTPAdapter
    folder = os.environ[FOLDER]
    if (Path(folder) / 'requests.log').is_file():
        # Lets the driver ask this process where it is when something is slow.
        install.stacks = open(Path(folder) / 'stacks.log', 'a')
        faulthandler.register(signal.SIGUSR1, file=install.stacks, all_threads=True)

    class OfflineAdapter(TapasHTTPAdapter):
        def __init__(self, session, preferred='firefox', headless=False, interval=1.0):
            super().__init__(transport=OfflineTransport(folder))
            self.interval = interval

        def inspect_library(self, category):
            self.check_session()
            return {'authenticated': True, 'membership_verified': True, 'scope': category, 'complete': True,
                    'boundary_observed': True,
                    'series': [{'source_series_id': str(key), 'title': f'Comic {key}'} for key in SERIES]}

        def episode_url(self, key):
            return 'https://tapas.io/episode/' + key

        def capture_automatic(self, url):
            return self.capture(url)

        def prefetch_episode(self, url):
            return None

    class OfflineFastSession:
        """What the fast speed downloads through: the same offline source."""

        def __init__(self, session, *args, **options):
            self.adapter = OfflineAdapter(session)
            self.adapter.download_profile = 'fast'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @contextmanager
        def batch(self):
            yield self.adapter

    worker.TapasAdapter = OfflineAdapter
    fast_import.FastWorkerSession = OfflineFastSession


class Failed(Exception):
    pass


class Bench:
    """One fresh library and the app on it, driven the way the window and the page drive it."""

    def __init__(self, name, home, rng):
        self.name, self.rng = name, rng
        self.home = Path(home)
        self.root, self.control = self.home / 'Comic Archiver', self.home / 'control'
        self.session, self.library = self.root / '.comic-session', self.root / 'Library'
        self.session.mkdir(parents=True); self.control.mkdir()
        (self.session / 'state.json').write_text(json.dumps({'cookies': [], 'origins': []}))
        (self.session / 'state.json').chmod(0o600)
        self.log = self.control / 'requests.log'
        self.log.touch()
        self.problems, self.times, self.runtime, self.notes = [], {'pause': [], 'close': []}, None, []
        self.busy_answers, self.slow = 0, []

    # -- what went wrong ---------------------------------------------------------------------------------------
    def check(self, good, message):
        if not good:
            self.problems.append(message)
            print(f'    PROBLEM {self.name}: {message}', flush=True)
        return good

    def need(self, good, message):
        if not self.check(good, message):
            raise Failed(message)

    # -- observing ---------------------------------------------------------------------------------------------
    def lines(self, kind=None):
        rows = [line.split(' ', 3) for line in self.log.read_text().splitlines()]
        return [row for row in rows if len(row) == 4 and (kind is None or row[2] == kind)]

    def account(self):
        from comic_archive.local_web import LOCAL_OWNER
        return self.registry.account_status(LOCAL_OWNER, self.registry.workspace_id)

    def worker(self):
        from comic_archive import background
        value = background.status(self.session)
        return value.get('state') if value.get('running') is True else 'gone'

    def processes(self):
        found = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    words = (entry / 'cmdline').read_bytes().split(b'\0')
                except OSError:
                    continue
                if b'browser-worker' in words and os.fsencode(str(self.session)) in words:
                    found.append(int(entry.name))
        return found

    def snapshot(self):
        """One line on what the downloader is doing, for the report on something slow."""
        def read(path):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}
        worker = read(self.session / 'worker/status.json')
        task = worker.get('task_id') if worker.get('state') == 'busy' else None
        request = read(self.session / f'worker/requests/{task}.json') if task else {}
        progress = read(self.library / '.archive-state/progress.json')
        run = progress.get('run_id')
        asked = bool(run) and (self.library / f'.archive-state/cancellations/{run}.json').exists()
        leave = read(self.session / 'worker/drain.json').get('worker_id')
        leave = 'no' if leave is None else 'yes' if leave == worker.get('worker_id') else 'another'
        return (f'downloader {self.worker()} task {str(task)[-6:]} {request.get("operation")} asked_to_leave={leave}; '
                f'run {str(run)[-6:]} {progress.get("status")}/{progress.get("phase")} asked_to_stop={asked}; '
                f'requests {len(self.lines())}')

    def stacks(self):
        """Where this process and the downloader are right now, innermost call first."""
        mine = []
        for ident, frame in sys._current_frames().items():
            if ident != threading.get_ident():
                calls = [f'{Path(call.filename).name}:{call.lineno} {call.name}' for call in traceback.extract_stack(frame)[-7:]]
                mine.append(' < '.join(reversed(calls)))
        theirs, log = [], self.control / 'stacks.log'
        try:
            pid = json.loads((self.session / 'worker/status.json').read_text()).get('pid')
            before = log.stat().st_size if log.exists() else 0
            if pid in self.processes():
                os.kill(pid, signal.SIGUSR1)
                time.sleep(.3)
                with log.open() as file:
                    file.seek(before)
                    for line in file.read().splitlines():
                        if line.startswith(('Thread', 'Current thread')):
                            theirs.append('')
                        elif line.startswith('  File') and theirs and theirs[-1].count(' < ') < 8:
                            name, _, rest = line.strip()[6:].partition('", line ')
                            theirs[-1] += (' < ' if theirs[-1] else '') + f'{Path(name).name}:{rest.replace(" in ", " ")}'
        except (OSError, ValueError) as error:
            theirs.append(type(error).__name__)
        return {'app': mine, 'downloader': theirs}

    @contextmanager
    def watch(self):
        """Note twice a second what the downloader is doing while something is waited for."""
        samples, done, start = [], threading.Event(), time.monotonic()
        def run():
            asked = False
            while len(samples) < 60:
                try:
                    samples.append(f'{time.monotonic() - start:.1f}s {self.snapshot()}')
                    if not asked and time.monotonic() - start > 2.5:
                        asked = True
                        samples.insert(0, self.stacks())
                except Exception as error:
                    samples.append(f'{time.monotonic() - start:.1f}s {type(error).__name__}')
                if done.wait(.5):
                    return
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield samples
        finally:
            done.set(); thread.join()

    def slowly(self, what, seconds, samples):
        stacks = [sample for sample in samples if isinstance(sample, dict)]
        lines = [sample for sample in samples if isinstance(sample, str)]
        # Only the moments where something changed, apart from the clock.
        changes = [line for n, line in enumerate(lines) if n == 0 or line.split(' ', 1)[1] != lines[n - 1].split(' ', 1)[1]]
        self.slow.append({'what': what, 'seconds': seconds, 'app_log': list(self.notes), 'changes': changes[:10],
                          'after_2.5s': stacks[0] if stacks else None})

    def until(self, test, seconds, what):
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            value = test()
            if value:
                return value
            state = self.account()
            self.need(state.get('status') != 'held', f'the download is held ({state.get("code")}) while waiting for: {what}')
            time.sleep(.05)
        self.need(False, f'timed out after {seconds}s waiting for: {what} (account {self.account().get("status")}/'
                         f'{self.account().get("phase")}, downloader {self.worker()})')

    def flowing(self, more=3, seconds=90):
        """Images are being downloaded right now."""
        start = len(self.lines('image'))
        self.until(lambda: len(self.lines('image')) >= start + more, seconds, 'images being downloaded')

    def quiet(self, seconds, after):
        """Nothing at all is asked of Tapas during this time."""
        before = len(self.lines())
        time.sleep(seconds)
        extra = self.lines()[before:]
        self.check(not extra, f'{len(extra)} request(s) were made {after}: {[row[2] + " " + row[3] for row in extra[:3]]}')

    def gone(self, after):
        from comic_archive import background
        self.check(background.status(self.session).get('running') is False, f'the downloader is still running {after}')
        limit = time.monotonic() + 5  # It has given up its lock and is on its way out.
        while self.processes() and time.monotonic() < limit:
            time.sleep(.05)
        self.check(not self.processes(), f'a downloader process is still alive 5s {after}')

    # -- acting ------------------------------------------------------------------------------------------------
    def open(self):
        from comic_archive import app_runtime
        os.environ[FOLDER] = str(self.control)
        self.runtime = app_runtime.Runtime(self.root)
        self.url = self.runtime.start()
        self.registry = self.runtime.server.get_app().registry
        outcome = self.runtime.start_worker()
        self.need(outcome == 'running', f'the downloader did not start: {outcome}')
        self.until(lambda: self.worker() in {'ready', 'busy'}, 90, 'the downloader to be ready')
        opener = build_opener(ProxyHandler({}))
        with opener.open(self.url, timeout=10) as response:
            cookie = response.headers['Set-Cookie'].split(';', 1)[0]
        with opener.open(Request(self.url + 'api/v1/context', headers={'Cookie': cookie}), timeout=10) as response:
            token = json.loads(response.read())['csrf_token']
        self.opener = opener
        self.headers = {'Cookie': cookie, 'Origin': self.url.rstrip('/'), 'X-CSRF-Token': token,
                        'Content-Type': 'application/json'}

    def post(self, action):
        """The page's Go ('account') or Pause ('account/pause') click.

        Returns the HTTP status, or None if the app was closing. 409 means
        "busy, try again": the page shows that as an error and the person
        clicks again, so it is counted and the click repeated.
        """
        url = f'{self.url}api/v1/workspaces/{self.registry.workspace_id}/{action}'
        start = time.monotonic()
        with self.watch() as samples:
            for _ in range(8):
                try:
                    with self.opener.open(Request(url, data=b'{}', headers=self.headers, method='POST'), timeout=60) as response:
                        response.read()
                        code = response.status
                except HTTPError as error:
                    code = error.code
                except (URLError, OSError):
                    return None
                if code != 409:
                    break
                self.busy_answers += 1
                time.sleep(.3)
        seconds = round(time.monotonic() - start, 2)
        if seconds > 2:
            self.slowly(f'{action} answered {code}', seconds, samples)
        self.check(code < 500 and code != 409, f'the dashboard answered {code} to {action} after {seconds}s')
        return code

    def go(self):
        self.until(lambda: self.worker() == 'ready', 30, 'the downloader to be free for Go')
        self.need(self.post('account') == 200, 'Go was refused although the downloader was ready')
        state = self.account()
        self.check(state.get('status') in {'running', 'finished'}, f'after Go the download is {state.get("status")}/{state.get("code")}')

    def pause(self, limit=20):
        """Pause, then time how long the downloader takes to stop working."""
        start = time.monotonic()
        self.need(self.post('account/pause') == 200, 'Pause was refused')
        self.check(self.account().get('status') == 'stopped', f'after Pause the download is {self.account().get("status")}')
        self.until(lambda: self.worker() != 'busy', limit, 'the downloader to stop after Pause')
        self.times['pause'].append(round(time.monotonic() - start, 2))
        self.check(self.worker() == 'ready', f'after Pause the downloader is {self.worker()}')

    def close(self, wait=20, limit=None, ended=False):
        """Close the app. `ended` says whether the downloader is expected to need ending instead of stopping by itself."""
        self.notes.clear()
        start = time.monotonic()
        with self.watch() as samples:
            outcome = self.runtime.stop(wait)
        seconds = round(time.monotonic() - start, 2)
        self.times['close'].append(seconds)
        if outcome != 'stopped' or (seconds > 4 and not ended):
            self.slowly(f'closing ended as {outcome}', seconds, samples)
        self.check(outcome == 'stopped', f'closing ended as {outcome!r} after {seconds}s')
        self.check(seconds <= (limit or wait + 15), f'closing took {seconds}s')
        was_ended = any('was ended' in note for note in self.notes)
        self.check(was_ended == ended, 'the downloader had to be ended' if was_ended else 'the downloader was expected to need ending')
        self.gone('after closing')
        self.quiet(1.5, 'after closing')

    def reopen_waiting(self):
        """Open again: a download that was running does nothing until Go, which continues it."""
        self.open()
        state = self.account()
        self.check(state.get('status') == 'stopped', f'after reopening the download is {state.get("status")}/{state.get("code")}')
        self.quiet(2, 'before Go after reopening')
        self.go()

    def finish(self):
        """Let the download run to its end and check what was saved."""
        from comic_archive.archive import episode_archives, verify
        (self.control / 'fast').touch()
        for name in ('stuck', 'slow-scan', 'steady-scan'):
            (self.control / name).unlink(missing_ok=True)
        if self.runtime is None or self.runtime._stopped:
            self.open()
        if self.account().get('status') == 'stopped':
            self.go()
        self.until(lambda: self.account().get('status') == 'finished', 240, 'the download to finish')
        self.close(ended=False)
        archives = [path for folder in sorted(self.library.iterdir()) if folder.is_dir() and not folder.name.startswith('.')
                    for path in episode_archives(folder)]
        expected = len(SERIES) * (EPISODES - 1)
        self.check(len(archives) == expected, f'{len(archives)} episodes were saved, expected {expected}')
        self.check(verify(self.library, decode_images=True) == [], 'the saved library does not verify')
        self.check(not self.lines('UNEXPECTED'), 'the offline source was asked something it does not know')
        readers = [row[3] for row in self.lines('reader')]
        self.check(not any(locked(int(path.rsplit('/', 1)[1])) for path in readers), 'a locked episode was opened')
        images = [row[3] for row in self.lines('image')]
        self.check(len(set(images)) == expected * IMAGES, f'{len(set(images))} different images were fetched, expected {expected * IMAGES}')
        return {'images_fetched_more_than_once': len(images) - len(set(images))}

    def receipt(self):
        """What the downloader answered to the task the account download is waiting for, when it is held."""
        try:
            pending = json.loads((self.session / 'worker/account-export.json').read_text()).get('pending') or {}
            answer = json.loads((self.session / f'worker/responses/{pending.get("task_id")}.json').read_text())
        except (OSError, ValueError, AttributeError):
            return None
        result = answer.get('result') if isinstance(answer.get('result'), dict) else {}
        return {'operation': pending.get('operation'), 'answer': answer.get('status'), 'error': answer.get('error'),
                'error_code': answer.get('error_code'), 'result_status': result.get('status'),
                'pause_reason': result.get('pause_reason')}

    def abandon(self):
        """After a failure: leave nothing running."""
        try:
            if self.runtime is not None:
                self.runtime.stop(5)
        except Exception:
            pass
        for pid in self.processes():
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


# -- scenarios -------------------------------------------------------------------------------------------------
def pause_and_go(b):
    """Pause at random moments, from the very first second of a new library, and Go again each time."""
    b.open(); b.go()
    for cycle in range(14):
        time.sleep(0 if cycle < 3 else b.rng.uniform(0, 1.5))
        b.pause()
        b.quiet(1, 'after Pause')
        b.go()
    b.flowing()


def pause_mid_download(b):
    """Pause while images are coming in: it stops within seconds and Go continues without a hold."""
    b.open(); b.go(); b.flowing()
    for _ in range(6):
        b.pause(limit=10)
        b.quiet(1.5, 'after Pause')
        b.go(); b.flowing()


def impatient_clicks(b):
    """Double clicks and clicks that come too early: two Pauses at once, two Gos at once, Go straight after Pause."""
    b.open(); b.go(); b.flowing()
    def twice(action):
        codes = []
        clicks = [threading.Thread(target=lambda: codes.append(b.post(action))) for _ in range(2)]
        for click in clicks: click.start()
        for click in clicks: click.join()
        b.check(200 in codes, f'neither of two clicks on {action} was accepted: {codes}')

    for _ in range(4):
        twice('account/pause')
        early = b.post('account')  # Go while the downloader may still be stopping: accepted or refused, never an error.
        b.check(early in {200, 400}, f'an early Go was answered {early}')
        b.until(lambda: b.worker() != 'busy', 20, 'the downloader to settle after the clicks')
        if b.account().get('status') != 'running':
            twice('account')
        b.check(b.account().get('status') == 'running', f'after the clicks the download is {b.account().get("status")}/{b.account().get("code")}')
        b.flowing()


def close_mid_download(b):
    """Close while images are coming in: everything stops at once, and the next opening waits for Go."""
    b.open(); b.go(); b.flowing()
    for _ in range(5):
        time.sleep(b.rng.uniform(0, 1))
        b.close(limit=12)
        b.reopen_waiting(); b.flowing()


def close_at_any_moment(b):
    """Close at random moments from the first second on, including while lists and series are being checked."""
    b.open(); b.go()
    for cycle in range(8):
        time.sleep(0 if cycle < 2 else b.rng.uniform(0, 2.5))
        b.close()
        b.reopen_waiting()
    b.flowing()


def close_while_paused(b):
    """Close a paused download, also while Pause is still taking effect: it stays paused until Go."""
    b.open(); b.go(); b.flowing()
    b.pause(); b.close()
    b.open()
    b.check(b.account().get('status') == 'stopped', f'a paused download reopened as {b.account().get("status")}')
    b.quiet(2, 'while paused after reopening')
    b.go(); b.flowing()
    click = threading.Thread(target=b.post, args=('account/pause',))
    click.start()
    time.sleep(b.rng.uniform(0, .2))
    b.close()
    click.join()
    b.open()
    state = b.account()
    b.check(state.get('status') in {'stopped', 'running'}, f'after Pause and closing together the download is {state.get("status")}/{state.get("code")}')
    if state.get('status') == 'stopped':
        b.quiet(1.5, 'while paused after reopening')
        b.go()
    b.flowing()


def stop_during_series_checks(b):
    """Pause, then close twice, in the middle of checking the series: each stops between two series and nothing is held."""
    def checking(more=1):
        start = len(b.lines('scan'))
        b.until(lambda: len(b.lines('scan')) >= start + more or b.lines('image'), 60, 'series being checked')
        b.until(lambda: b.worker() == 'busy' or b.lines('image'), 20, 'the downloader to be in the middle of a check')

    (b.control / 'steady-scan').touch()
    b.open(); b.go(); checking(2)
    b.pause(limit=5)
    b.quiet(1.5, 'after Pause')
    b.go()
    for _ in range(2):
        checking()
        b.close(limit=8)
        b.reopen_waiting()
    b.check(not b.lines('image'), 'the series checks were over before the last closing; nothing was tried')
    (b.control / 'steady-scan').unlink()
    b.flowing()


def close_during_slow_checks(b):
    """Close while a series check hangs on a slow answer: that step cannot be interrupted, so the downloader is ended."""
    (b.control / 'slow-scan').touch()
    b.open(); b.go()
    b.until(lambda: b.lines('scan'), 60, 'a series check to begin')
    time.sleep(.5)
    b.close(wait=3, ended=True)
    (b.control / 'slow-scan').unlink()
    b.check(not b.lines('image'), 'images were fetched before the series had been checked')
    b.reopen_waiting(); b.flowing()


def close_with_dead_connection(b):
    """Close while an image request never comes back: the downloader is ended, and the next opening waits for Go."""
    b.open(); b.go(); b.flowing()
    for _ in range(2):
        (b.control / 'stuck').touch()
        time.sleep(1)
        b.close(wait=3, ended=True)
        (b.control / 'stuck').unlink()
        b.reopen_waiting(); b.flowing()


def downloader_dies(b):
    """The downloader is killed outright in the middle of an episode, like a power cut."""
    from comic_archive import background
    b.open(); b.go(); b.flowing()
    for _ in range(2):
        time.sleep(b.rng.uniform(0, 1))
        pid = background.read(b.session / 'worker/status.json').get('pid')
        os.killpg(pid, signal.SIGKILL)
        b.until(lambda: b.worker() == 'gone', 10, 'the killed downloader to be gone')
        b.close(limit=5)
        b.reopen_waiting(); b.flowing()


def random_abuse(b):
    """A long random mix of Go, Pause, double clicks, closing and reopening."""
    b.open(); b.go()
    for step in range(40):
        if b.account().get('status') == 'finished':
            break
        action = b.rng.choice(['go', 'pause', 'pause', 'close', 'wait', 'both'])
        if action == 'wait':
            time.sleep(b.rng.uniform(0, 2))
        elif action == 'close':
            b.close()
            b.open()
        elif action == 'both':
            first = threading.Thread(target=b.post, args=('account/pause',))
            first.start(); b.post('account'); first.join()
        else:
            b.post('account' if action == 'go' else 'account/pause')
        state = b.account()
        b.need(state.get('status') in {'running', 'stopped', 'finished'},
               f'step {step} ({action}) left the download {state.get("status")}/{state.get("code")}')
        if state.get('status') == 'stopped':
            b.until(lambda: b.worker() != 'busy', 20, 'the downloader to stop after a Pause')
            b.quiet(.5, 'while paused')


SCENARIOS = [pause_and_go, pause_mid_download, impatient_clicks, close_mid_download, close_at_any_moment,
             close_while_paused, stop_during_series_checks, close_during_slow_checks, close_with_dead_connection, downloader_dies, random_abuse]


def prepare(top):
    """Make every downloader started from here use the offline source, and prove it before anything is started."""
    hook = Path(top) / 'hook'
    hook.mkdir()
    (hook / 'sitecustomize.py').write_text(HOOK.format(folder=FOLDER, scripts=str(Path(__file__).resolve().parent)))
    os.environ.update(PYTHONPATH=str(hook), HOME=str(Path(top) / 'home'), XDG_STATE_HOME=str(Path(top) / 'state'),
                      **{FOLDER: str(top)})
    probe = subprocess.run([sys.executable, '-c', 'import comic_archive.worker as w; print(w.TapasAdapter.__name__)'],
                           capture_output=True, text=True, timeout=120, cwd=top)
    if probe.stdout.strip() != 'OfflineAdapter':
        raise SystemExit('The downloader would not use the offline source; nothing was started.\n' + probe.stderr[-2000:])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--seed', type=int, default=20261012)
    parser.add_argument('--repeat', type=int, default=1, help='run everything this many times, each with the next seed')
    parser.add_argument('--dir', default=None, help='where to make the test libraries (default: in memory)')
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    if args.list:
        for scenario in SCENARIOS:
            print(f'{scenario.__name__}: {scenario.__doc__}')
        return 0
    if not Path('/proc/self/cmdline').is_file():
        raise SystemExit('This script needs Linux.')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from comic_archive import app_runtime
    results = []
    memory = Path('/dev/shm')
    where = args.dir or (str(memory) if memory.is_dir() and os.access(memory, os.W_OK) else None)
    print(f'Test libraries are made in {where or tempfile.gettempdir()}', flush=True)
    with tempfile.TemporaryDirectory(dir=where, prefix='tapas-stress-') as top:
        prepare(top)
        for seed, scenario in [(args.seed + n, scenario) for n in range(args.repeat) for scenario in SCENARIOS]:
            if args.only and scenario.__name__ not in args.only:
                continue
            bench = Bench(scenario.__name__, Path(top) / f'{scenario.__name__}-{seed}', random.Random(f'{seed}-{scenario.__name__}'))
            handler = logging.Handler()
            handler.emit = lambda record, bench=bench: bench.notes.append(record.getMessage())
            app_runtime.LOG.addHandler(handler); app_runtime.LOG.setLevel(logging.INFO)
            print(f'{scenario.__name__}: {scenario.__doc__}', flush=True)
            start, extra = time.monotonic(), {}
            try:
                scenario(bench)
                extra = bench.finish()
            except Failed:
                pass
            except Exception:
                bench.problems.append('the script itself failed: ' + traceback.format_exc()[-1500:])
            finally:
                bench.abandon()
                app_runtime.LOG.removeHandler(handler)
            if bench.problems:
                try:
                    extra['downloader_log_tail'] = (bench.session / 'worker/background.log').read_text()[-500:]
                    extra['account'] = {key: bench.account().get(key) for key in ('status', 'phase', 'code')}
                    extra['held_on'] = bench.receipt()
                except Exception:
                    pass
            results.append({'scenario': scenario.__name__, 'seed': seed, 'passed': not bench.problems, 'problems': bench.problems,
                            'slow': bench.slow[:2], 'busy_answers': bench.busy_answers,
                            'seconds': round(time.monotonic() - start, 1), 'pauses': len(bench.times['pause']),
                            'slowest_pause_seconds': max(bench.times['pause'], default=None),
                            'closes': len(bench.times['close']), 'slowest_close_seconds': max(bench.times['close'], default=None),
                            **extra})
            print(f'  {"PASS" if not bench.problems else "FAIL"} in {results[-1]["seconds"]}s', flush=True)
    for result in sorted(results, key=lambda result: (not result['passed'], bool(result['slow']))):
        print(json.dumps(result, indent=1 if result['slow'] or not result['passed'] else None))
    failed = [f'{result["scenario"]}-{result["seed"]}' for result in results if not result['passed']]
    print(f'SUMMARY {len(results) - len(failed)} of {len(results)} passed' + (f'; failed: {", ".join(failed)}' if failed else ''))
    return 0 if results and not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
