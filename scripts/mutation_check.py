#!/usr/bin/env python3
"""Break the product in one named way at a time and see whether the named test notices.

Coverage shows that a line runs under some test, not that a test would fail if
the line were wrong. Each entry of the list names one change to the product
(a guard removed, a limit ignored) and the test that exists to catch it. The
change is applied, that one test is run, and the file is put back.

Run it on a throwaway copy of the repository (the build fleet's), never on a
working tree someone is editing: files under comic_archive/ are rewritten
while it runs.

    python scripts/mutation_check.py [LIST ...]     default: tests/mutations.json

An entry: {"id", "source": "comic_archive/x.py", "old": text found exactly once
in that file, "new": its replacement, "test_file": "test_x.py", "test": name}.
Exit status 0 only when every change was caught. A change that stops the test
from running at all (an import error, a timeout) is CRASHED, not caught.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')


def run(item):
    """(exit status, tests run, first error line, whether the named test itself failed) of the one named test."""
    try:
        done = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', item['test_file'],
                               '-k', item['test']], capture_output=True, text=True, timeout=240, env=ENV)
    except subprocess.TimeoutExpired:
        return 1, 1, 'timed out after 240 seconds', False
    out = done.stderr + done.stdout
    count = re.search(r'^Ran (\d+) test', out, re.M)
    errors = [line for line in out.splitlines() if re.match(r'^[A-Za-z_.]*(Error|Exception|Exit|Interrupt)\b', line)]
    last = out.strip().splitlines()[-1] if out.strip() else ''
    # unittest heads each failure "FAIL: name (id)"; a module that failed to import shows up as a _FailedTest.
    # An exception unittest does not catch (KeyboardInterrupt) ends the run instead, its traceback through the test.
    failed = (any(item['test'] in test and '_FailedTest' not in where
                  for test, where in re.findall(r'^(?:FAIL|ERROR): (\S+) \(([^)]*)\)', out, re.M))
              or any(item['test'] in frame for frame in re.findall(
                  r'^  File "[^"]*' + re.escape(item['test_file']) + r'", line \d+, in (\w+)$', out, re.M)))
    return done.returncode, int(count.group(1)) if count else 0, (errors[0] if errors else last)[:170], failed


def check(item, unchanged):
    """One result line; it starts with CAUGHT only when the test passed before the change and failed with it."""
    key = (item['test_file'], item['test'])
    if key not in unchanged:
        unchanged[key] = run(item)
    code, count, why, _ = unchanged[key]
    name = f"{item['id']} [{item['test_file']} -k {item['test']}]"
    if count < 1:
        return f'NOTEST   {name}: no test matched ({why})'
    if code:
        return f'BROKEN   {name}: fails before anything is changed ({why})'
    path = Path(item['source'])
    if not item['source'].startswith('comic_archive/') or not path.is_file():
        return f'NOSOURCE {name}: {path}'
    text = path.read_text()
    if text.count(item['old']) != 1:
        return f"NOMATCH  {name}: 'old' occurs {text.count(item['old'])} times in {path}, must be exactly once"
    try:
        path.write_text(text.replace(item['old'], item['new']))
        try:
            compile(path.read_text(), str(path), 'exec')
        except SyntaxError as error:
            return f'INVALID  {name}: the changed file does not compile ({str(error)[:120]})'
        code, count, why, failed = run(item)
    finally:
        path.write_text(text)
    if code and count >= 1 and failed:
        return f'CAUGHT   {name}: {count} run, failed with: {why}'
    if code:
        return f'CRASHED  {name}: the change stopped the test from running, which proves nothing ({why})'
    return f'SURVIVED {name}: {count} run, all still pass with the product broken'


def main():
    lists = sys.argv[1:] or ['tests/mutations.json']
    unchanged, results = {}, []
    for name in lists:
        for item in json.loads(Path(name).read_text()):
            results.append(check(item, unchanged))
            print(results[-1], flush=True)
    caught = sum(line.startswith('CAUGHT') for line in results)
    print(f'SUMMARY {caught} of {len(results)} changes caught')
    return 0 if caught == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
