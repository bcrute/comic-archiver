"""Audit the exact installed desktop dependency set before packaging; run on the CI fleet."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def dependencies(text):
    """The application's own wheel is local; every third-party distribution must have an exact version."""
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', line):
            raise ValueError('The bundled dependency list contains an unpinned distribution.')
        name = line.split('==', 1)[0].lower().replace('_', '-').replace('.', '-')
        if name != 'comic-archive':
            result.append(line)
    if not result:
        raise ValueError('The bundled dependency list is empty.')
    return '\n'.join(sorted(result)) + '\n'


def audit(requirements, output):
    with tempfile.TemporaryDirectory(prefix='comic-audit-') as temporary:
        selected = Path(temporary) / 'requirements.txt'
        selected.write_text(dependencies(Path(requirements).read_text('utf-8')), encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'pip_audit', '--disable-pip', '--no-deps', '--progress-spinner', 'off',
                        '--format', 'json', '--output', str(output), '-r', str(selected)], check=True, timeout=300)
    report = json.loads(Path(output).read_text('utf-8'))
    if not report.get('dependencies') or any(item.get('vulns') for item in report['dependencies']):
        raise RuntimeError('The bundled dependency audit did not pass.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    audit(args.requirements, args.output)


if __name__ == '__main__':
    main()
