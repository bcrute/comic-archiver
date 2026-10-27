"""The concrete two-process Android beta journey, inside a fleet-owned emulator.

The first instrumentation intentionally cannot finish: the host observes a live
partial SAF copy, kills the target app, then requires a complete recovery test.
Ordinary test crashes, missing evidence, stale files and skipped tests never pass.
"""
import json
import pathlib
import re
import secrets
import subprocess
import time
from urllib.parse import unquote, urlsplit

TARGET = 'io.github.bcrute.comicarchiver'
TEST_CLASS = TARGET + '.integration.BetaJourneyTest'
CONTROL = 'content://' + TARGET + '.test.control'
TREE = 'content://' + TARGET + '.test.documents/tree/root'


def _inventory(provider):
    if not isinstance(provider, dict) or type(provider.get('blocked')) is not bool:
        return None
    rows = provider.get('files')
    if not isinstance(rows, list):
        return None
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            return None
        path = pathlib.PurePosixPath(row['id'])
        if path.parts[:1] != ('root',) or len(path.parts) < 2 or '..' in path.parts or row['id'] in identities:
            return None
        if type(row.get('bytes')) is not int or not 0 <= row['bytes'] <= 8 * 1024 * 1024:
            return None
        if not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
            return None
        identities.add(row['id'])
    return rows


def provider_status(text):
    """Read only the test provider's fixed Bundle fields; malformed output fails."""
    blocked = re.search(r'\bblocked=(true|false)\b', text)
    partial = re.search(r'\bpartial_bytes=(\d+)\b', text)
    files = re.search(r'\bfiles=(\[)', text)
    if not blocked or not partial or not files:
        raise ValueError('Incomplete fixture provider status')
    rows, _ = json.JSONDecoder().raw_decode(text[files.start(1):])
    if not isinstance(rows, list):
        raise ValueError('Invalid fixture file inventory')
    value = {'blocked': blocked.group(1) == 'true', 'partial_bytes': int(partial.group(1)), 'files': rows}
    if _inventory(value) is None:
        raise ValueError('Invalid fixture file records')
    return value


def valid_ready(value, provider, journal, token, pid, running):
    """Require independent live-process, real partial bytes and durable-copy evidence."""
    copies = _inventory(provider)
    if (not isinstance(value, dict) or running is not True or not isinstance(pid, str)
            or not re.fullmatch(r'[1-9][0-9]*', pid) or copies is None or not isinstance(journal, str)):
        return False
    if value.get('run_token') != token or value.get('phase') != 'copy_blocked' or any(
            value.get(key) is not True for key in ('native_pause_verified', 'native_recreation_verified',
                                                 'comic_and_novel_verified', 'native_fast_verified',
                                                 'native_discovery_verified')):
        return False
    originals = value.get('originals')
    if not isinstance(originals, list) or len(originals) != 2:
        return False
    for row in originals:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            return False
        path = pathlib.PurePosixPath(row['name'])
        if path.is_absolute() or '..' in path.parts or path.parts[:1] != ('Library',):
            return False
        if type(row.get('bytes')) is not int or not 0 < row['bytes'] <= 8 * 1024 * 1024:
            return False
        if not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
            return False
    if sorted(pathlib.PurePosixPath(row['name']).suffix for row in originals) != ['.cbz', '.epub']:
        return False
    comic = next(row for row in originals if row['name'].endswith('.cbz'))
    if type(value.get('partial_bytes')) is not int or not 0 < value['partial_bytes'] < comic['bytes']:
        return False
    if (type(value.get('expected_cbz_bytes')) is not int or value['expected_cbz_bytes'] != comic['bytes']
            or provider['blocked'] is not True or type(provider.get('partial_bytes')) is not int
            or provider['partial_bytes'] != value['partial_bytes']):
        return False
    document = value.get('document_id')
    if not isinstance(document, str) or not document.startswith('root/'):
        return False
    partials = [row for row in copies if row['id'] == document]
    if len(partials) != 1 or partials[0].get('bytes') != value['partial_bytes']:
        return False
    lines = journal.splitlines()
    if not lines or lines[0] != 'comicarchiver-export-journal v2':
        return False
    for line in lines[1:]:
        fields = line.split('|')
        if len(fields) != 12 or fields[2] != 'CBZ' or fields[7] != 'COPYING' or fields[11] != TREE:
            continue
        uri = urlsplit(fields[8])
        if (uri.scheme == 'content' and uri.netloc == TARGET + '.test.documents'
                and uri.path.startswith('/tree/root/document/')
                and unquote(uri.path.removeprefix('/tree/root/document/')) == document
                and not uri.query and not uri.fragment
                and fields[5] == str(comic['bytes']) and fields[6] == comic['sha256']):
            return True
    return False


def recovery_test_passed(text):
    blocks, current = [], {}
    for line in text.splitlines():
        if line.startswith('INSTRUMENTATION_STATUS: '):
            key, separator, value = line.removeprefix('INSTRUMENTATION_STATUS: ').partition('=')
            if separator:
                current[key] = value
        elif line.startswith('INSTRUMENTATION_STATUS_CODE: '):
            try:
                code = int(line.removeprefix('INSTRUMENTATION_STATUS_CODE: '))
            except ValueError:
                return False
            if code != 1:
                blocks.append((code, current))
            current = {}
    return (len(blocks) == 1 and blocks[0][0] == 0 and blocks[0][1].get('class') == TEST_CLASS
            and blocks[0][1].get('test') == 'recoverAfterHostStopWithoutGo'
            and blocks[0][1].get('numtests') == '1'
            and re.findall(r'^INSTRUMENTATION_CODE: (.*)$', text, re.M) == ['-1']
            and 'INSTRUMENTATION_FAILED' not in text and 'FAILURES!!!' not in text)


def valid_recovery(value, ready, provider, token):
    if not isinstance(value, dict) or value.get('phase') != 'recovered' or value.get('run_token') != token:
        return False
    if type(value.get('verified_exports')) is not int or value['verified_exports'] != 2 or any(value.get(key) is not True for key in
            ('held_without_go', 'originals_unchanged', 'same_partial_document_recovered', 'comic_and_novel_verified',
             'working_library_adopted', 'library_binding_preserved', 'library_originals_preserved', 'library_switch_held',
             'managed_archive_grants')):
        return False
    if not isinstance(value.get('adopted_library_id'), str) or not re.fullmatch(
            r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', value['adopted_library_id']):
        return False
    adopted = value.get('adopted_archives')
    if not isinstance(adopted, list) or len(adopted) != 2:
        return False
    for row in adopted:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or pathlib.PurePosixPath(row['name']).parts[:1] != ('Library',) or '..' in pathlib.PurePosixPath(row['name']).parts
                or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= 8 * 1024 * 1024
                or not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            return False
    if (sorted(pathlib.PurePosixPath(row['name']).suffix for row in adopted) != ['.cbz', '.epub']
            or sorted((row['bytes'], row['sha256']) for row in adopted) != sorted((row['bytes'], row['sha256']) for row in ready['originals'])):
        return False
    copies = _inventory(provider)
    if copies is None or provider['blocked'] is not False or len(copies) != 2:
        return False
    expected = sorted((row['bytes'], row['sha256']) for row in ready['originals'])
    observed = sorted((row.get('bytes', -1), row.get('sha256', '')) for row in copies)
    return expected == observed and sum(row.get('id') == ready['document_id'] for row in copies) == 1


def run(adb, serial, avd_name, test_apk, artifacts, runner):
    """Called only by the existing feasibility gate, before it disposes its AVD."""
    match = re.fullmatch(r'emulator-(\d+)', serial)
    if not match or not 5554 <= int(match[1]) <= 5682 or int(match[1]) % 2:
        raise ValueError('The beta journey requires an explicit emulator target')
    artifacts.mkdir(parents=True, exist_ok=True)
    prefix = [str(adb), '-s', serial]

    def command(*args, timeout=30):
        result = runner([*prefix, *map(str, args)], timeout=timeout, keep_bytes=32000)
        if result['returncode'] != 0:
            raise RuntimeError('An emulator journey command failed')
        return result['tail']

    if command('shell', 'getprop', 'ro.kernel.qemu').strip() != '1' or \
            command('emu', 'avd', 'name').splitlines()[:1] != [avd_name]:
        raise ValueError('The selected emulator does not belong to this fleet job')
    if 'Success' not in command('install', '-r', test_apk, timeout=300).splitlines():
        raise RuntimeError('Instrumentation APK installation failed')
    command('shell', 'am', 'force-stop', TARGET)
    token = secrets.token_hex(16)
    component = TARGET + '.test/' + TARGET + '.integration.BetaTestRunner'

    def instrument(method):
        return [*prefix, 'shell', 'am', 'instrument', '-w', '-r', '-e', 'class', TEST_CLASS + '#' + method,
                '-e', 'disposable_emulator', 'true', '-e', 'run_token', token, component]

    def screenshot(name):
        destination = artifacts / name
        with destination.open('wb') as output:
            result = subprocess.run([*prefix, 'exec-out', 'run-as', TARGET, 'cat',
                                     'files/integration-code/' + name], stdout=output,
                                    stderr=subprocess.PIPE, timeout=30, check=False)
        with destination.open('rb') as saved:
            header = saved.read(8)
        if result.returncode != 0 or header != b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('A native journey screenshot is missing or invalid')

    first = None
    summary = {'kind': 'native beta stand-in journey', 'phase1_intentionally_stopped': False,
               'phase2_passed': False, 'checks': {}}
    try:
        with (artifacts / 'prepare.log').open('wb') as output:
            first = subprocess.Popen(instrument('prepareTransferAndWaitForHostStop'), stdout=output,
                                     stderr=subprocess.STDOUT)
            ready = None
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if first.poll() is not None:
                    raise RuntimeError('The preparation test ended before a verified live copy')
                result = runner([*prefix, 'shell', 'run-as', TARGET, 'cat',
                                 'files/integration-code/ready.json'], timeout=15)
                if result['returncode'] == 0:
                    try:
                        ready = json.loads(result['tail'])
                    except ValueError:
                        pass
                    if ready is not None:
                        break
                time.sleep(0.5)
            if ready is None:
                raise RuntimeError('No live-copy readiness evidence arrived')
            status = provider_status(command('shell', 'content', 'call', '--uri', CONTROL, '--method', 'status'))
            journal = command('shell', 'run-as', TARGET, 'cat', 'files/export/journal-v1.txt')
            pid = command('shell', 'pidof', TARGET).strip()
            if not valid_ready(ready, status, journal, token, pid, first.poll() is None):
                raise RuntimeError('The live partial copy was not independently verified')
            (artifacts / 'ready.json').write_text(json.dumps(ready, indent=2) + '\n')
            (artifacts / 'partial-provider.json').write_text(json.dumps(status, indent=2) + '\n')
            (artifacts / 'partial-journal.txt').write_text(journal)
            screenshot('copy-blocked.png')
            command('shell', 'am', 'force-stop', TARGET)
            gone = runner([*prefix, 'shell', 'pidof', TARGET], timeout=15)
            if gone['returncode'] not in (0, 1) or gone['tail'].strip():
                raise RuntimeError('The target process was not stopped')
            first.wait(timeout=30)
            summary['phase1_intentionally_stopped'] = True
        second = runner(instrument('recoverAfterHostStopWithoutGo'), timeout=300, keep_bytes=32000)
        (artifacts / 'recover.log').write_text(second['tail'])
        if second['returncode'] != 0 or not recovery_test_passed(second['tail']):
            raise RuntimeError('The exact recovery instrumentation test did not pass')
        final = json.loads(command('shell', 'run-as', TARGET, 'cat', 'files/integration-code/result.json'))
        status = provider_status(command('shell', 'content', 'call', '--uri', CONTROL, '--method', 'status'))
        if not valid_recovery(final, ready, status, token):
            raise RuntimeError('The recovered bytes or held-start evidence did not match')
        (artifacts / 'result.json').write_text(json.dumps(final, indent=2) + '\n')
        (artifacts / 'final-provider.json').write_text(json.dumps(status, indent=2) + '\n')
        screenshot('recovered.png')
        summary.update(phase2_passed=True, checks={'native_signin_pause_recreation': True,
            'comic_and_novel_output': True, 'real_inflight_copy_stopped': True, 'same_document_recovered': True,
            'verified_cbz_epub_exports': True, 'held_restart_without_go': True, 'originals_unchanged': True,
            'working_library_adoption': True, 'preserved_account_binding': True, 'held_library_switch': True})
        return summary
    finally:
        # Assertion timeouts write fixture-only facts before Android tears down
        # the instrumentation process. Preserve them beside the primary log.
        diagnostic = runner([*prefix, 'shell', 'run-as', TARGET, 'cat',
                             'files/integration-code/timeout.json'], timeout=15, keep_bytes=32000)
        if diagnostic['returncode'] == 0:
            try:
                value = json.loads(diagnostic['tail'])
                if value.get('run_token') == token:
                    (artifacts / 'timeout.json').write_text(json.dumps(value, indent=2) + '\n')
                    screenshot('timeout.png')
            except (ValueError, OSError, RuntimeError):
                pass  # The primary failure remains authoritative.
        if first is not None and first.poll() is None:
            runner([*prefix, 'shell', 'am', 'force-stop', TARGET], timeout=30)
            first.terminate()
            try:
                first.wait(timeout=15)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=15)
        (artifacts / 'journey.json').write_text(json.dumps(summary, indent=2) + '\n')
