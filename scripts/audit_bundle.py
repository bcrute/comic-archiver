"""Audit the exact installed desktop dependency set before packaging; run on the CI fleet."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def dependencies(text):
    """The application's own wheel is local; every third-party distribution must have an exact version."""
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', line):
            raise ValueError('The bundled dependency list contains an unpinned distribution.')
        if canonical(line.split('==', 1)[0]) != 'comic-archive':
            result.append(line)
    if not result:
        raise ValueError('The bundled dependency list is empty.')
    return '\n'.join(sorted(result)) + '\n'


def audit(requirements, output):
    with tempfile.TemporaryDirectory(prefix='comic-audit-') as temporary:
        selected = Path(temporary) / 'requirements.txt'
        wanted = dependencies(Path(requirements).read_text('utf-8'))
        selected.write_text(wanted, encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'pip_audit', '--disable-pip', '--no-deps', '--progress-spinner', 'off',
                        '--format', 'json', '--output', str(output), '-r', str(selected)], check=True, timeout=300)
    report = json.loads(Path(output).read_text('utf-8'))
    found = report.get('dependencies')
    # A distribution the scanner skipped, or never reported, was not audited: that is not a clean result.
    if (not isinstance(found, list) or not found
            or any(not isinstance(item, dict) or item.get('vulns') or 'skip_reason' in item for item in found)
            or sorted(canonical(str(item.get('name'))) for item in found)
            != sorted(canonical(line.split('==', 1)[0]) for line in wanted.splitlines())):
        raise RuntimeError('The bundled dependency audit did not pass.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    audit(args.requirements, args.output)


if __name__ == '__main__':
    main()
