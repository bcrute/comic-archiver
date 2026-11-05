"""The app's own record of what it did, kept so that one file, sent once, explains a download that fell short.

The window writes the app log; each site's downloader writes its own beside that site's library, every line stamped
with the time. Save log for support gathers both, with each site's episodes not saved and its download's last
steps, into one text file. Series and episodes go in by number only, never by title, and the home folder and the
library folder are written as ~ and <library>. Nothing here uses Qt: the phone app saves the same file.
"""
import faulthandler
import logging
import os
import platform
import re
import shutil
import stat
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .osdep import NOFOLLOW, NONBLOCK, WINDOWS, drive, linked, open_log

DOWNLOAD_LOG = 'comic_archive.download'  # The download's decisions, in whichever process makes them.
WORKER_LOG_BYTES = 1024 * 1024  # The downloader's log starts afresh past this when it starts,
WORKER_LOGS_KEPT = 2  # keeping this many before it.
CRASH_NAME = 'crash.log'
CRASH_BYTES = 64 * 1024
# What one saved file holds of each part: together a few megabytes at most, under any mail service's limit.
APP_LOG_BYTES = 512 * 1024
DOWNLOADER_BYTES = 512 * 1024
UNSAVED = 5000
STEPS = 300
LIMIT = 4 * 1024 * 1024
CODE = re.compile(r'[a-z][a-z0-9_]{0,47}')
NUMBER = re.compile(r'[0-9A-Za-z_.-]{1,64}')  # A site's own id for a series or an episode.
MOMENT = re.compile(r'[0-9T:.+Z-]{1,40}')
# The browser engine's routine complaints, hundreds an hour, that say nothing about a download.
NOISE = re.compile(r'handshake failed; returned -1|QVulkanInstance: Failed to initialize Vulkan'
                   r'|Fontconfig error: Cannot load default config file')
LINE = re.compile(r'[^\n]*\n|[^\n]+')
_hooked = False
_crashes = None


def _frames(tb):
    return traceback.extract_tb(tb)


def describe(error):
    """Class and code location only: exception text can hold cookies, paths or URLs."""
    where = ' < '.join(f'{Path(frame.filename).name}:{frame.lineno}' for frame in reversed(_frames(error.__traceback__)[-4:]))
    return type(error).__name__ + (' at ' + where if where else '')


def unexpected(kind, tb):
    """An error nothing caught, as a log keeps it: its class and every place it passed through, never its text."""
    return '\n'.join([f'{kind.__name__} that nothing caught', *(
        f'  at {Path(frame.filename).name}:{frame.lineno} in {frame.name}' for frame in _frames(tb))])


def catch_unexpected(name):
    """Log every error nothing else caught, on any thread, to the log called name; Python then reports it as before."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    before, thread_before = sys.excepthook, threading.excepthook

    def hook(kind, error, tb):
        if not issubclass(kind, KeyboardInterrupt):
            logging.getLogger(name).critical(unexpected(kind, tb))
        before(kind, error, tb)

    def thread_hook(args):
        if not issubclass(args.exc_type, SystemExit):
            logging.getLogger(name).critical(unexpected(args.exc_type, args.exc_traceback))
        thread_before(args)
    sys.excepthook, threading.excepthook = hook, thread_hook


def catch_crashes(path):
    """Have Python write where each thread was if the app itself crashes, into a small file of its own."""
    global _crashes
    if _crashes is not None:
        return
    try:
        if path.is_symlink():
            return
        if path.is_file() and path.stat().st_size > CRASH_BYTES:
            path.unlink()
        _crashes = os.fdopen(open_log(path), 'a', encoding='utf-8')
        faulthandler.enable(_crashes, all_threads=True)
    except OSError:
        _crashes = None  # The app runs on; a crash is only not written down.


class Stamped:
    """A text stream that begins each line with the time it was written, as the app log does."""

    def __init__(self, stream, clock=time.localtime):
        self.stream, self.clock, self.fresh = stream, clock, True

    def write(self, text):
        out = []
        for line in LINE.findall(text):
            if self.fresh:
                out.append(time.strftime('%Y-%m-%d %H:%M:%S ', self.clock()))
            out.append(line)
            self.fresh = line.endswith('\n')
        self.stream.write(''.join(out))
        return len(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def downloader_output(version):
    """The downloader's own process: its output stamped, its download's decisions logged, a crash written down."""
    from .support_details import system
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        try:
            # Windows would write in its own code page, and stop at the first character outside it.
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        except (AttributeError, OSError, ValueError):
            pass
        setattr(sys, name, Stamped(stream))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log = logging.getLogger(DOWNLOAD_LOG)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        faulthandler.enable(sys.stderr, all_threads=True)
    except (AttributeError, OSError, ValueError):
        pass
    print(f'Downloader of Comic Archiver {version} starting on {system()}, Python {platform.python_version()}.',
          flush=True)


def trim(path, limit=WORKER_LOG_BYTES, kept=WORKER_LOGS_KEPT):
    """Start a log afresh once it has grown past limit, keeping the last few before it as path.1 and on."""
    path = Path(path)
    try:
        if path.stat().st_size <= limit:
            return
        for number in range(kept, 0, -1):
            newer = path if number == 1 else path.with_name(f'{path.name}.{number - 1}')
            if newer.exists():
                os.replace(newer, path.with_name(f'{path.name}.{number}'))
    except OSError:
        pass  # It grows on for now; the next start tries again.


def place(path):
    """Where a folder is, as support needs it: the kind of drive, its file system and the room left on it."""
    kind, system = drive(path)
    try:
        room = f'{shutil.disk_usage(path).free / 1e9:.1f} GB free'
    except OSError:
        room = 'room left unknown'
    return f'{kind} drive, {system}, {room}'


def _read(path, size):
    """The last size bytes of a log, or none for one that is reached through a link or is not a plain file."""
    if linked(path):
        return b''
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK | getattr(os, 'O_BINARY', 0)), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return b''
            stream.seek(max(0, info.st_size - size))
            return stream.read(size)
    except OSError:
        return b''


def _tail(paths, limit, folders=()):
    """The end of the files, read oldest first, as text: at most limit characters, from the start of a line.

    Nothing is read through a link, in the files or in the folders named: the saved file is sent to someone,
    and a link could make it hold the sign-in instead of a log.
    """
    if any(linked(folder) for folder in folders):
        return ''
    data = b''.join(_read(path, 4 * limit) for path in paths)  # Enough bytes for limit characters.
    return _end(data.decode('utf-8', errors='replace'), limit)


def _end(text, limit):
    """The last limit characters of text, from the start of a line."""
    if len(text) <= limit:
        return text
    text = text[-limit:]
    return text[text.find('\n') + 1:]


def _quiet(text):
    """text without the browser engine's routine complaints, saying how many were left out."""
    lines = text.splitlines()
    kept = [line for line in lines if not NOISE.search(line)]
    dropped = len(lines) - len(kept)
    return '\n'.join(kept + ([f'({dropped} routine messages from the browser engine left out)'] if dropped else []))


def _word(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else '-'


def _plain(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else '-'


def _unsaved(library):
    if not (library / '.archive-state/inventory.sqlite3').is_file():
        return ['None recorded yet']
    from .inventory import Inventory
    with Inventory(library, read_only=True) as inventory:
        rows = inventory.unsaved(UNSAVED + 1)
    lines = [f"series {_plain(row['series_id'], NUMBER)} episode {_plain(row['id'], NUMBER)}: {_word(row['status'])} "
             f"{_word(row['error_code'])}, tries {row['attempts']}, access {_word(row['access_state'])}, "
             f"checked {_plain(row['observed_at'], MOMENT)}" for row in rows[:UNSAVED]]
    return (lines or ['None']) + ([f'(only the first {UNSAVED} are listed)'] if len(rows) > UNSAVED else [])


def _counts(row, keys):
    return ', '.join(f'{key} {row[key]}' for key in keys if type(row.get(key)) is int)


def _steps(session, library):
    from . import download_control
    record = download_control.history(session, library, STEPS)
    if record is None:
        return ['No download yet']
    state, steps = record
    now = f"Now: {_word(state['status'])}"
    if state['code']:
        now += f"/{_word(state['code'])}"
    now += f" at {_word(state['stage'])} since {_plain(state['updated_at'], MOMENT)}"
    if state['wait_until']:
        now += f", waiting until {_plain(state['wait_until'], MOMENT)}"
    tries = _counts(state, ('idle_waits', 'network_waits', 'source_probes', 'failed_retries'))
    lines = [now, f"Tries: {tries}, episodes waiting {state['episodes_waiting']}"]
    if isinstance(state['audit'], dict):
        lines.append('Last check: ' + _counts(state['audit'], sorted(state['audit'])))
    for step in steps:
        ended = (f"{_word(step['outcome'])} at {_plain(step['finished_at'], MOMENT)}" if step['finished_at']
                 else 'out' if step['published_at'] else 'not sent yet')
        done = _counts(step, ('episodes', 'pages', 'bytes', 'series_checked'))
        lines.append(f"{_plain(step['reserved_at'], MOMENT)} {_plain(step['task_id'], NUMBER)} {_word(step['kind'])}: "
                     f"{ended}{'; ' + done if done else ''}{'; progressed' if step['progressed'] else ''}")
    return lines


def _part(read, *args):
    try:
        return read(*args)
    except Exception as error:
        return [f'Could not be read: {describe(error)}']


def site_records(name, session, library):
    """One site's part of a saved log: its episodes not saved, its download's last steps and its downloader's log."""
    session, library = Path(session), Path(library)
    worker = session / 'worker' / 'background.log'
    logs = [worker.with_name(f'{worker.name}.{n}') for n in range(WORKER_LOGS_KEPT, 0, -1)] + [worker]
    return '\n'.join([f'=== {name}: episodes not saved', *_part(_unsaved, library),
                      '', f'=== {name}: the download’s last steps, oldest first', *_part(_steps, session, library),
                      '', f'=== {name}: the downloader’s log',
                      _end(_quiet(_tail(logs, 4 * DOWNLOADER_BYTES, (session, worker.parent))), DOWNLOADER_BYTES)])


def app_log(folder):
    """The window's log, oldest first, and what Python wrote when the app last crashed, if it has."""
    folder = Path(folder)
    text = _tail([folder / 'desktop.log.2', folder / 'desktop.log.1', folder / 'desktop.log'], APP_LOG_BYTES)
    crash = _tail([folder / CRASH_NAME], CRASH_BYTES)
    return text + (f'\n=== Where the app was when it last stopped suddenly\n{crash}' if crash.strip() else '')


def _spellings(path):
    """The ways a folder may be written in a log: as given, resolved, and with either slash."""
    forms = {str(path)}
    try:
        forms.add(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        pass
    forms |= {form.replace('\\', '/') for form in forms} | {form.replace('/', '\\') for form in forms}
    # A folder so short (a drive, the root) would match inside every other path.
    return {form.rstrip('/\\') for form in forms if len(form.rstrip('/\\')) > 3}


def scrub(text, libraries, home=None):
    """text with each library folder written as <library> and the home folder as ~, however they are spelled."""
    names = [(spelling, '<library>') for folder in libraries for spelling in _spellings(folder)]
    names += [(spelling, '~') for spelling in _spellings(Path.home() if home is None else home)]
    for spelling, word in sorted(names, key=lambda name: -len(name[0])):
        text = re.sub(re.escape(spelling), lambda _, word=word: word, text, flags=re.IGNORECASE if WINDOWS else 0)
    return text


def bundle(details, sites, app_text, libraries, now=None):
    """The whole file: the details for support, each site's records, then the app's log."""
    stamp = (now or datetime.now().astimezone()).isoformat(timespec='seconds')
    text = '\n\n'.join([f'Saved {stamp}; the times below are this computer’s own.', details, *sites,
                        f'=== The app’s log\n{app_text}'])
    text = scrub(text, libraries)
    return text if len(text) <= LIMIT else text[:LIMIT] + '\n(shortened)'


def save(folder, text, now=None):
    """Write text into folder as a new file named for what it is and when; its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"Comic Archiver log {(now or datetime.now()):%Y-%m-%d %H.%M.%S}"
    for number in range(1, 100):
        path = folder / (f'{name}.txt' if number == 1 else f'{name} ({number}).txt')
        try:
            with open(path, 'x', encoding='utf-8') as file:
                file.write(text)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(name)
