"""Verify the owner's fleet attestation and the staged release files before publication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib

CHECKS = {'validation', 'mutation', 'qt_engine', 'linux_build', 'windows_build', 'bundle_audits'}
SUMS = 'SHA256SUMS.txt'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def owned_by(actor, owner):
    return (isinstance(actor, dict) and isinstance(actor.get('login'), str)
            and actor['login'].casefold() == owner.casefold())


def check_sums(path, artifacts):
    """The optional checksum list users are told to trust must name exactly the attested downloads."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError('The checksum list is missing or invalid.')
    lines = [re.fullmatch(r'([a-f0-9]{64}) [ *]([^\s/\\]+)', line) for line in path.read_text('utf-8').splitlines()]
    sums = {line[2]: line[1] for line in lines if line}
    if None in lines or len(sums) != len(lines) or sums != {name: record['sha256'] for name, record in artifacts.items()}:
        raise ValueError('The checksum list must match the fleet evidence exactly.')


def verify(folder, commit, release, owner, version):
    evidence = folder / 'release-evidence.json'
    if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 1024**2:
        raise ValueError('Missing or invalid release evidence.')
    value = json.loads(evidence.read_text('utf-8'))
    if (not re.fullmatch(r'[a-f0-9]{40}', commit) or value.get('schema_version') != 1
            or value.get('commit') != commit or value.get('version') != version):
        raise ValueError('The evidence does not describe this tagged version and commit.')
    if (release.get('draft') is not True or release.get('target_commitish') != commit
            or release.get('tag_name') != 'v' + version
            or not owned_by(release.get('author'), owner)):
        raise ValueError('The owner must stage a draft release for this exact commit and version.')
    assets = release.get('assets', [])
    if not isinstance(assets, list):
        raise ValueError('The release must include uploaded asset metadata.')
    manifests = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == evidence.name]
    if (len(manifests) != 1 or manifests[0].get('state') != 'uploaded'
            or not owned_by(manifests[0].get('uploader'), owner)
            or manifests[0].get('size') != evidence.stat().st_size
            or manifests[0].get('digest') != 'sha256:' + digest(evidence)):
        raise ValueError('The exact evidence must be uploaded by the owner and match GitHub\'s recorded digest.')
    checks = value.get('checks', {})
    if set(checks) != CHECKS or any(checks[name] is not True for name in CHECKS):
        raise ValueError('Every required fleet check must pass.')
    tests = value.get('tests', {})
    if (type(tests.get('tests_run')) is not int or tests['tests_run'] < 1
            or tests.get('failures') != 0 or tests.get('errors') != 0 or tests.get('skipped') != []):
        raise ValueError('The required suite must pass without skips.')
    expected = {f'ComicArchiver-{version}-linux-x86_64.tar.gz', f'ComicArchiver-{version}-windows-x86_64.zip',
                f'ComicArchiver-{version}-windows-x86_64-setup.exe'}
    artifacts = value.get('artifacts', {})
    if set(artifacts) != expected:
        raise ValueError('The evidence must name all three release downloads.')
    for name, record in artifacts.items():
        path = folder / name
        if (not isinstance(record, dict) or path.is_symlink() or not path.is_file()
                or type(record.get('size')) is not int or record['size'] < 1
                or path.stat().st_size != record['size'] or digest(path) != record.get('sha256')):
            raise ValueError('A staged download differs from the fleet evidence: ' + name)
    # Publishing releases every draft asset, so nothing may ride along unchecked.
    names = [asset.get('name') if isinstance(asset, dict) else None for asset in assets]
    required = expected | {evidence.name}
    if len(set(names)) != len(names) or not required <= set(names) <= required | {SUMS}:
        raise ValueError('The draft must hold only the three downloads, the evidence and an optional checksum list.')
    for asset in assets:
        path = folder / asset['name']
        if (asset.get('state') != 'uploaded' or path.is_symlink() or not path.is_file()
                or asset.get('size') != path.stat().st_size):
            raise ValueError('A draft asset differs from its downloaded file: ' + asset['name'])
    if SUMS in names:
        check_sums(folder / SUMS, artifacts)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--owner', required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    version = tomllib.loads(project.read_text('utf-8'))['project']['version']
    verify(args.folder, args.commit, json.loads(args.release.read_text('utf-8')), args.owner, version)
    print('Fleet evidence and every release asset match the tagged commit.')


if __name__ == '__main__':
    main()
