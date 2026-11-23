"""Audit the exact installed desktop dependency set before packaging; run on the CI fleet."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

RECEIPT_ENV = 'COMIC_ARCHIVER_DESKTOP_AUDIT_RECEIPT'
RECEIPT_SHA256_ENV = 'COMIC_ARCHIVER_DESKTOP_AUDIT_RECEIPT_SHA256'


def approved_receipt():
    path = os.environ.get(RECEIPT_ENV)
    digest = os.environ.get(RECEIPT_SHA256_ENV)
    if path is None and digest is None:
        return None
    if not path or not digest or re.fullmatch(r'[a-f0-9]{64}', digest) is None:
        raise ValueError('Supply both a desktop audit receipt and its independently approved SHA-256.')
    return Path(path), digest


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
    # Require a clean report for every exact name and version we handed to the scanner.
    expected = sorted((canonical(name), version) for name, version in
                      (line.split('==', 1) for line in wanted.splitlines()))
    if (not isinstance(found, list)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                   or not isinstance(item.get('version'), str) or item.get('vulns') != []
                   or 'skip_reason' in item for item in found)
            or sorted((canonical(item['name']), item['version']) for item in found) != expected):
        raise RuntimeError('The bundled dependency audit did not pass.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--platform', choices=('linux', 'macos', 'windows'))
    args = parser.parse_args()
    if args.platform and (receipt := approved_receipt()) is not None:
        if __package__:
            from .desktop_advisories import verify_bundle
        else:
            from desktop_advisories import verify_bundle
        report = verify_bundle(Path(__file__).resolve().parents[1], args.platform,
                               args.requirements.read_text('utf-8'), *receipt)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        return
    audit(args.requirements, args.output)


if __name__ == '__main__':
    main()
