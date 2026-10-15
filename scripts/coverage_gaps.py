#!/usr/bin/env python3
"""List the lines and branches no test runs, from a coverage JSON report.

The gate writes `.validation/coverage.json` on the fleet, where only the end
of a job's output can be read back. `--pack` prints the whole list as one
compressed line for that; `--unpack` turns such a line back into the list.

    python scripts/coverage_gaps.py [REPORT] [--files PART ...] [--pack]
    python scripts/coverage_gaps.py --unpack < line.txt
"""
import argparse
import base64
import json
from pathlib import Path
import sys
import zlib


def ranges(numbers):
    """[3, 4, 5, 9] as '3-5 9'."""
    parts, start, last = [], None, None
    for number in sorted(numbers):
        if start is None:
            start = last = number
        elif number == last + 1:
            last = number
        else:
            parts.append(str(start) if start == last else f'{start}-{last}')
            start = last = number
    if start is not None:
        parts.append(str(start) if start == last else f'{start}-{last}')
    return ' '.join(parts)


def gaps(report, only=()):
    """{file: [statements, missed, branches, missed branches, missed line ranges, missed branches as 'from>to']}."""
    result = {}
    for name, value in sorted(report['files'].items()):
        if only and not any(part in name for part in only):
            continue
        summary = value['summary']
        # A negative target is the way out of the function.
        jumps = ' '.join(f'{source}>{target if target > 0 else "exit"}' for source, target in value.get('missing_branches', []))
        result[name] = [summary['num_statements'], summary['missing_lines'], summary.get('num_branches', 0),
                        summary.get('missing_branches', 0), ranges(value['missing_lines']), jumps]
    return result


def show(result):
    for name, (statements, missed, branches, missed_branches, lines, jumps) in result.items():
        print(f'{name}: {missed} of {statements} lines and {missed_branches} of {branches} branches never run')
        if lines:
            print(f'  lines: {lines}')
        if jumps:
            print(f'  branches: {jumps}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('report', nargs='?', default='.validation/coverage.json')
    parser.add_argument('--files', nargs='*', default=(), help='only files whose path contains one of these')
    parser.add_argument('--pack', action='store_true', help='print one compressed line')
    parser.add_argument('--unpack', action='store_true', help='read such a line from standard input')
    args = parser.parse_args()
    if args.unpack:
        text = sys.stdin.read().split('GAPS ')[-1].strip()
        show(json.loads(zlib.decompress(base64.b85decode(text))))
        return 0
    result = gaps(json.loads(Path(args.report).read_text()), args.files)
    if args.pack:
        print('GAPS ' + base64.b85encode(zlib.compress(json.dumps(result, separators=(',', ':')).encode(), 9)).decode())
    else:
        show(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
