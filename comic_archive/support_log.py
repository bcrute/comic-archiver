"""The app's own record of what it did, kept so that one file, sent once, explains a download that fell short.

The window writes the app log; each site's downloader writes its own beside that site's library, every line stamped
with the time. Save log for support gathers both, with each site's episodes not saved and its download's last
steps, into one text file. Series and episodes go in by number only, never by title, and the home folder and the
library folder are written as ~ and <library>. Nothing here uses Qt: the phone app saves the same file.
"""
import faulthandler
import logging
import json
import os
import platform
import re
import shutil
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .osdep import NOFOLLOW, NONBLOCK, WINDOWS, drive, linked, open_log

DOWNLOAD_LOG = 'comic_archive.download'  # The download's decisions, in whichever process makes them.
WORKER_LOG_BYTES = 1024 * 1024  # The downloader's log starts afresh past this when it starts,
WORKER_LOGS_KEPT = 2  # keeping this many before it.
CRASH_NAME = 'crash.log'
CRASH_BYTES = 64 * 1024
# Support attachments quote a recent excerpt from each local log.
APP_LOG_BYTES = 24 * 1024
DOWNLOADER_BYTES = 24 * 1024
EXPORTED_CRASH_BYTES = 8 * 1024
UNSAVED = 40
NOVEL_FAILURES = 15
STEPS = 40
LIMIT = 128 * 1024
SHORTENED = '(shortened: the start of this part is left out)'
CODE = re.compile(r'[a-z][a-z0-9_]{0,47}')
NUMBER = re.compile(r'[0-9A-Za-z_.-]{1,64}')  # A site's own id for a series or an episode.
MOMENT = re.compile(r'[0-9T:.+Z-]{1,40}')
# Routine browser and per-page messages omitted from support excerpts.
NOISE = re.compile(r'handshake failed; returned -1|QVulkanInstance: Failed to initialize Vulkan'
                   r'|Fontconfig error: Cannot load default config file')
PAGE_READY = re.compile(r'^(?:[0-9-]{10} [0-9:]{8} )?Episode [0-9]+: page [0-9]+/[0-9]+ ready$')
TASK_CHATTER = re.compile(r'^(?:[0-9-]{10} [0-9:]{8} )?'
                          r'(?:Starting task job-[a-f0-9]{24}|Task job-[a-f0-9]{24} completed\.'
                          r'|INFO download: step job-[a-f0-9]{24} \([a-z]+\) sent)$')
TASK_COUNT = r'(?:(?:pages|bytes|episodes|series_checked) [0-9]+|progressed)'
TASK_COUNTS = rf'(?:: {TASK_COUNT}(?:, {TASK_COUNT})*)?'
SUCCESSFUL_TASK = re.compile(r'^[0-9-]{10} [0-9:]{8} INFO download: step '
                             rf'(job-[a-f0-9]{{24}}) \([a-z]+\) ended ok finished{TASK_COUNTS}$')
APP_TIME = r'[0-9-]{10} [0-9:]{8},[0-9]{3}'
SLOW_READ = re.compile(rf'^(?P<time>{APP_TIME}) INFO Slow library read site=(?P<site>Tapas|Webtoon|Tappytoon) '
                       r'operation=(?P<operation>[a-z_]+) seconds=(?P<seconds>[0-9]+\.[0-9]{3})$')
SLEEP_CHATTER = re.compile(rf'^{APP_TIME} INFO the computer is (?:kept awake for the download|free to sleep again)$')
CHOICE_CYCLE = re.compile(
    rf'^(?P<started>{APP_TIME}) INFO download: running at (?P<stage>[a-z]+) -> stopped at (?P=stage)'
    r' \(changing download choices\)\n'
    rf'(?:(?P<outcome>{APP_TIME} INFO download: step job-[a-f0-9]{{24}} \([a-z]+\) ended ok '
    rf'(?P<result>cancelled|finished){TASK_COUNTS})\n)?'
    rf'(?P<ended>{APP_TIME}) INFO download: stopped at (?P=stage) -> running at (?P=stage)'
    r' \(download choices changed\)(?:\n|$)', re.MULTILINE)
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
    """The end of the files, read oldest first, as text: at most limit UTF-8 bytes, from the start of a line.

    Nothing is read through a link, in the files or in the folders named: the saved file is sent to someone,
    and a link could make it hold the sign-in instead of a log.
    """
    if any(linked(folder) for folder in folders):
        return ''
    data = b''.join(_read(path, limit + 1) for path in paths)
    return _end(data.decode('utf-8', errors='replace').encode('utf-8'), limit).decode('utf-8')


def _end(data, limit):
    """The last complete lines that fit in limit bytes of UTF-8 data."""
    if limit <= 0:
        return b''
    if len(data) <= limit:
        return data
    start = len(data) - limit
    if data[start - 1] != 10:
        newline = data.find(b'\n', start)
        start = len(data) if newline < 0 else newline + 1
    return data[start:]


def _log_excerpt(text, limit):
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    marker = SHORTENED + '\n'
    return marker[:limit] + _end(data, limit - len(marker)).decode('utf-8')


def _quiet(text, recorded_steps=()):
    """Drop routine worker chatter and successful results covered by the structured task history."""
    lines = text.splitlines()
    kept = [line for line in lines if not NOISE.search(line)]
    dropped = len(lines) - len(kept)
    without_pages = [line for line in kept if not PAGE_READY.fullmatch(line)]
    progress = len(kept) - len(without_pages)
    useful = [line for line in without_pages if not TASK_CHATTER.fullmatch(line)]
    tasks = len(without_pages) - len(useful)
    recorded = {step['task_id'] for step in recorded_steps if step['finished_at'] and step['outcome'] == 'ok'}
    unique = [line for line in useful
              if not ((finished := SUCCESSFUL_TASK.fullmatch(line)) and finished[1] in recorded)]
    duplicates = len(useful) - len(unique)
    useful = unique
    if dropped:
        useful.append(f'({dropped} routine messages from the browser engine left out)')
    if progress:
        useful.append(f'({progress} routine page progress lines left out)')
    if tasks:
        useful.append(f'({tasks} routine task start, dispatch and completion lines left out)')
    if duplicates:
        useful.append(f'({duplicates} successful task endings already listed in task history left out)')
    return '\n'.join(useful)


def _quiet_app(text):
    """Keep the slowest repeat read and the outcomes of complete checkbox pause/resume cycles."""
    slow_reads = {}
    kept = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip('\r\n')
        if SLEEP_CHATTER.fullmatch(content):
            continue
        slow = SLOW_READ.fullmatch(content)
        if slow:
            key = slow['site'], slow['operation']
            count, peak, peak_line = slow_reads.get(key, (0, -1, ''))
            seconds = float(slow['seconds'])
            slow_reads[key] = (count + 1, seconds, line) if seconds > peak else (count + 1, peak, peak_line)
            continue
        kept.append(line)
    compact = _quiet_choice_cycles(''.join(kept))
    if not slow_reads:
        return compact
    readings = ''.join(line if count == 1 else f'{line.rstrip()} (slowest of {count} occurrences)\n'
                       for count, _, line in slow_reads.values())
    return compact.rstrip('\n') + ('\n' if compact else '') + readings


def _quiet_choice_cycles(text):
    output = []
    burst = []
    end = 0

    def flush():
        first, last = burst[0], burst[-1]
        cancelled = sum(cycle['result'] == 'cancelled' for cycle in burst)
        output.append(f"{first['started']} INFO Saved download choices and resumed {len(burst)} time(s) "
                      f"at {first['stage']} through {last['ended']}; {cancelled} running task(s) cancelled for changes.\n")
        output.extend(cycle['outcome'] + '\n' for cycle in burst if cycle['outcome'] and (
            cycle['result'] == 'finished' or re.search(r'\bepisodes [1-9]', cycle['outcome'])))
        burst.clear()

    for cycle in CHOICE_CYCLE.finditer(text):
        between = text[end:cycle.start()]
        if burst and (between or burst[-1]['stage'] != cycle['stage']
                      or not _nearby(burst[-1]['ended'], cycle['started'])):
            flush()
        if between:
            output.append(between)
        burst.append(cycle)
        end = cycle.end()
    if burst:
        flush()
    output.append(text[end:])
    return ''.join(output)


def _nearby(earlier, later):
    try:
        return timedelta() <= datetime.fromisoformat(later) - datetime.fromisoformat(earlier) <= timedelta(minutes=1)
    except ValueError:
        return False


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


# Extract only the fixed fields printed by older versions of the worker.
NOVEL_FAILURE_LINE = re.compile(
    r'(?:[0-9-]{10} [0-9:]{8} )?Episode (?P<id>[0-9]{1,20}) of series (?P<series_id>[0-9]{1,20}): '
    r'not saved, novel_manifest_incomplete(?: reason=(?P<reason>[a-z_]{1,48}))?'
    r'(?: tag=(?P<tag>[a-z_]{1,48}))? \(UnsupportedContent(?P<location>[^\r\n]{0,512})\)\.')
NOVEL_LOCATION = re.compile(r'(?: at | < )(novels\.py:[0-9]{1,6})(?= < |$)')


def _novel_failures(library, logs, folders):
    if not (library / '.archive-state/inventory.sqlite3').is_file():
        return []
    from .inventory import Inventory
    from .novels import NOVEL_REASONS, DIAGNOSTIC_TAGS
    with Inventory(library, read_only=True) as inventory:
        rows = inventory.novel_failures(NOVEL_FAILURES + 1)
    shown = rows[:NOVEL_FAILURES]
    missing = {(row['series_id'], row['id']): row for row in shown if row['recorded_at'] is None}
    if missing and not any(linked(folder) for folder in folders):
        for path in reversed(logs):
            for line in reversed(_read(path, WORKER_LOG_BYTES).decode('utf-8', errors='replace').splitlines()):
                match = NOVEL_FAILURE_LINE.fullmatch(line)
                if match:
                    row = missing.pop((match['series_id'], match['id']), None)
                    if row is not None:
                        row.update(reason=match['reason'], tag=match['tag'])
                        location = NOVEL_LOCATION.search(match['location'])
                        if location:
                            row['location'] = location[1]
                if not missing:
                    break
            if not missing:
                break

    lines = []
    for row in shown:
        reason = row['reason'] if row['reason'] in NOVEL_REASONS else 'unrecorded'
        tag = f" tag={row['tag']}" if row['tag'] in DIAGNOSTIC_TAGS else ''
        parser = f" parser={row['location']}" if row.get('location') else ''
        lines.append(f"series {_plain(row['series_id'], NUMBER)} episode {_plain(row['id'], NUMBER)}: "
                     f"reason={reason}{tag}{parser}")
    if len(rows) > NOVEL_FAILURES:
        lines.append(f'(only {NOVEL_FAILURES} failures are listed)')
    return lines


def _counts(row, keys):
    return ', '.join(f'{key} {row[key]}' for key in keys if type(row.get(key)) is int)


def _steps(record):
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
    if len(steps) == STEPS:
        lines.append(f'(showing up to {STEPS} recent steps)')
    for step in steps:
        ended = (f"{_word(step['outcome'])} at {_plain(step['finished_at'], MOMENT)}" if step['finished_at']
                 else 'out' if step['published_at'] else 'not sent yet')
        done = _counts(step, ('episodes', 'pages', 'bytes', 'series_checked'))
        elapsed = ''
        if step['finished_at'] and step['published_at']:
            try:
                seconds = (datetime.fromisoformat(step['finished_at']) -
                           datetime.fromisoformat(step['published_at'])).total_seconds()
                if seconds >= 0:
                    elapsed = f'; elapsed {seconds:.1f}s'
            except (ValueError, TypeError):
                pass
        lines.append(f"{_plain(step['reserved_at'], MOMENT)} {_plain(step['task_id'], NUMBER)} {_word(step['kind'])}: "
                     f"{ended}{elapsed}{'; ' + done if done else ''}{'; progressed' if step['progressed'] else ''}")
    return lines


def _part(read, *args):
    try:
        return read(*args)
    except Exception as error:
        return [f'Could not be read: {describe(error)}']


def _account_lists(session, record):
    from . import download_control, sites
    if record is None:
        return []
    state, _ = record
    rows = {row['category']: row for row in state['account_lists'] if row['category'] in sites.LISTS}
    if not rows:
        return []
    lines = [f"Pass started: {_plain(state['round_started_at'], MOMENT)}"]
    for category in download_control.lists(session):
        row = rows.get(category, {})
        read_at = _plain(row.get('read_at'), MOMENT)
        if read_at == '-':
            lines.append(f'{category}: not checked in this pass')
            continue
        result = 'complete' if row.get('complete') == 1 else 'partial'
        count = row.get('observed_series_count')
        count_text = f', {count} series observed' if type(count) is int and count >= 0 else ''
        lines.append(f'{category}: {result}{count_text}, checked {read_at}')
    return lines


def _image_failures(library):
    """Recover image failures from recent batch receipts, even after the worker log rotates."""
    from .inventory import Inventory
    from .transfer_metrics import NETWORK_FAILURE_CODES
    if not (library / '.archive-state/inventory.sqlite3').is_file():
        return []
    lines = []
    with Inventory(library, read_only=True) as inventory:
        rows = inventory.db.execute("SELECT finished_at,summary FROM runs WHERE finished_at IS NOT NULL "
                                    "ORDER BY rowid DESC LIMIT ?", (STEPS,))
        for finished, raw in rows:
            try:
                batch = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(batch, dict) or not batch.get('deferred_asset_episode_ids'):
                continue
            metrics = batch.get('asset_metrics') or {}
            counts = metrics.get('error_counts', {}) if isinstance(metrics, dict) else {}
            reasons = [f'{key}={counts[key]}' for key in sorted(NETWORK_FAILURE_CODES)
                       if isinstance(counts, dict) and type(counts.get(key)) is int and counts[key] > 0]
            lines.append(f"{_plain(finished, MOMENT)} batch image errors: {', '.join(reasons) or 'unrecorded'}")
            failures = batch.get('deferred_asset_failures')
            if not isinstance(failures, list):
                continue
            for failure in failures[:5]:
                if not isinstance(failure, dict) or failure.get('reason') not in ('read_timeout', 'read_error'):
                    continue
                episode = _plain(failure.get('episode'), NUMBER)
                page = failure.get('page')
                lines.append(f"  episode {episode}, page {page if type(page) is int else 'unknown'}: {failure['reason']}")
                attempts = failure.get('attempts')
                if not isinstance(attempts, list):
                    continue
                for attempt in attempts[:3]:
                    if not isinstance(attempt, dict) or attempt.get('failure') not in (*NETWORK_FAILURE_CODES, 'http_error'):
                        continue
                    status = attempt.get('http_status')
                    elapsed, size = attempt.get('elapsed_ms'), attempt.get('received_bytes')
                    if type(elapsed) is int and type(size) is int:
                        reply = f'HTTP {status}' if type(status) is int else 'no response headers'
                        lines.append(f"    {attempt['failure']}, {reply}, {size} decoded bytes, {elapsed} ms")
    return lines


def _history_failure(library):
    from .inventory import Inventory
    from .purchase_history import support_diagnostic
    if not (library / '.archive-state' / 'inventory.sqlite3').is_file():
        return []
    with Inventory(library, read_only=True) as inventory:
        failure = support_diagnostic(inventory)
    if not failure:
        return []
    reason = failure.pop('reason')
    return [f"Last incomplete purchase-history read: {reason}",
            ', '.join(f'{key}={value}' for key, value in failure.items())]


def site_records(name, session, library):
    """One site's part of a saved log: its episodes not saved, its download's last steps and its downloader's log."""
    from . import download_control

    session, library = Path(session), Path(library)
    worker = session / 'worker' / 'background.log'
    logs = [worker.with_name(f'{worker.name}.{n}') for n in range(WORKER_LOGS_KEPT, 0, -1)] + [worker]
    folders = (session, worker.parent)
    failures = _part(_novel_failures, library, logs, folders)
    record = None
    try:
        record = download_control.history(session, library, STEPS)
    except Exception as error:
        steps = [f'Could not be read: {describe(error)}']
    else:
        steps = _part(_steps, record)
    excerpt = _quiet(_tail(logs, 8 * DOWNLOADER_BYTES, folders), record[1] if record else ())
    sections = [f'=== {name}: episodes not saved', *_part(_unsaved, library),
                '', f'=== {name}: the download’s last steps, oldest first', *steps,
                '', f'=== {name}: the downloader’s log', _log_excerpt(excerpt, DOWNLOADER_BYTES)]
    if failures:
        sections.extend(['', f'=== {name}: EPUB failure details', *failures])
    images = _part(_image_failures, library)
    if images:
        sections.extend(['', f'=== {name}: recent image failures, newest batch first', *images])
    discovery = _part(_account_lists, session, record)
    if name.casefold() == 'tapas':
        discovery.extend(_part(_history_failure, library))
    # Keep these structured results at the end, which survives the export's shared byte budget.
    if discovery:
        sections.extend(['', f'=== {name}: account discovery', *discovery])
    return '\n'.join(sections)


def app_log(folder, name='desktop.log'):
    """Recent app activity, summarized slow reads and the last crash, if any."""
    folder = Path(folder)
    text = _quiet_app(_tail([folder / f'{name}.2', folder / f'{name}.1', folder / name], 8 * APP_LOG_BYTES))
    text = _end(text.encode('utf-8'), APP_LOG_BYTES).decode('utf-8')
    crash = _tail([folder / CRASH_NAME], EXPORTED_CRASH_BYTES)
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


def _any_case():
    """Whether this computer's folders are the same folder in any case: so on Windows, and as a Mac's disk comes."""
    return WINDOWS or sys.platform == 'darwin'


def scrub(text, libraries, home=None):
    """text with each library folder written as <library> and the home folder as ~, however they are spelled.

    The home folder's own name goes too where only the end of its path is written, as under another mount point.
    """
    home = Path(Path.home() if home is None else home)
    flags = re.IGNORECASE if _any_case() else 0
    names = [(spelling, '<library>') for folder in libraries for spelling in _spellings(folder)]
    names += [(spelling, '~') for spelling in _spellings(home)]
    for spelling, word in sorted(names, key=lambda name: -len(name[0])):
        text = re.sub(re.escape(spelling), lambda _, word=word: word, text, flags=flags)
    if home.parent.name and len(home.name) > 1:
        text = re.sub(rf'[/\\]{re.escape(home.parent.name)}[/\\]{re.escape(home.name)}(?![\w.-])', '~', text, flags=flags)
    return text


def _fit(parts, limit):
    """Fit the scrubbed sections in a shared UTF-8 byte budget."""
    fitted, room = [part.encode('utf-8') for part in parts], max(0, limit)
    order = sorted(range(len(fitted)), key=lambda index: len(fitted[index]))
    marker = ('\n' + SHORTENED + '\n').encode('utf-8')
    for place, index in enumerate(order):
        share = room // (len(order) - place)
        if len(fitted[index]) > share:
            head, _, body = fitted[index].partition(b'\n')
            prefix = head + marker
            fitted[index] = prefix[:share] + _end(body, share - len(prefix))
            # The header may itself have exceeded the budget.
            fitted[index] = fitted[index].decode('utf-8', errors='ignore').encode('utf-8')
        room -= len(fitted[index])
    return [part.decode('utf-8') for part in fitted]


def bundle(details, sites, app_text, libraries, now=None, device='computer', home=None):
    """The whole file: the details for support, each site's records, then the app's log."""
    stamp = (now or datetime.now().astimezone()).isoformat(timespec='seconds')
    parts = [f'Saved {stamp}; the times below are this {device}’s own.', details, *sites,
             f'=== The app’s log\n{app_text}']
    parts = [scrub(part, libraries, home) for part in parts]
    return '\n\n'.join(_fit(parts, LIMIT - 2 * (len(parts) - 1)))


def save(folder, text, now=None):
    """Write text into folder as a new file named for what it is and when; its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"Comic Archiver log {(now or datetime.now()):%Y-%m-%d %H.%M.%S}"
    for number in range(1, 100):
        path = folder / (f'{name}.txt' if number == 1 else f'{name} ({number}).txt')
        try:
            with open(path, 'x', encoding='utf-8', newline='\n') as file:
                file.write(text)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(name)
