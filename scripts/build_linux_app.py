"""Build the self-contained Linux x86_64 Comic Archiver app and its tarball.

Run on a Linux CI host that has uv, never on the development PC:

    python3 scripts/build_linux_app.py --smoke

Output: dist/ComicArchiver-<version>-linux-x86_64.tar.gz and a .sha256 file.

Pinned downloads: CPython from python-build-standalone (SHA-256 below).
Python packages otherwise come from PyPI as in scripts/validate.py; the exact
result is recorded in requirements-frozen.txt. The app carries no separate
browser: the downloader uses the Qt web engine that comes with PySide6.
This script reads no Tapas
session and makes no Tapas requests.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
PACKAGING = PROJECT / 'packaging'
BUILD = PROJECT / 'build/linux'
DIST = PROJECT / 'dist'
APP = 'ComicArchiver'
PBS_TAG = '20261009'
PBS_NAME = 'cpython-3.14.8+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
PBS_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/' + PBS_TAG + '/'
           + PBS_NAME.replace('+', '%2B'))
PBS_SHA256 = 'ddc902fd26460f728758c67d80417c1031edf97589f1f162c4ba3aa158a35998'
EXTRAS = 'desktop,validation'


def version():
    return tomllib.loads((PROJECT / 'pyproject.toml').read_text('utf-8'))['project']['version']


def require_linux_x86_64(system=None, machine=None):
    system = system or platform.system()
    machine = machine or platform.machine()
    if system != 'Linux' or machine not in ('x86_64', 'AMD64'):
        raise RuntimeError(f'The Linux app can only be built on Linux x86_64 (this is {system} {machine})')


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, target, expected):
    """Download url to target only if its SHA-256 equals expected; never keep unverified bytes."""
    target = Path(target)
    expected = expected.lower()
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise RuntimeError('Download checksum is not a SHA-256 value')
    if target.is_file() and file_sha256(target) == expected:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.partial')
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as reply, temporary.open('wb') as out:
            while chunk := reply.read(1024 ** 2):
                out.write(chunk)
                digest.update(chunk)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError('Download checksum mismatch for ' + target.name)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print('Verified ' + target.name, flush=True)
    return target


def clean_env(**extra):
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env.update(PYTHONNOUSERSITE='1', UV_PYTHON_DOWNLOADS='never')
    env.update(extra)
    return env


def run(args, *, env=None, cwd=PROJECT, timeout=None, capture=False):
    print('Build stage: ' + ' '.join(str(a) for a in args), flush=True)
    try:
        return subprocess.run([str(a) for a in args], cwd=cwd, env=env or clean_env(), check=True, timeout=timeout,
                              capture_output=capture, text=True)
    except subprocess.CalledProcessError as error:
        if capture:
            print(str(error.stdout)[-3000:], str(error.stderr)[-3000:], sep='\n')
        raise


def app_dir():
    return BUILD / f'{APP}-{version()}'


def bundled_python(app):
    return app / 'python/bin/python3'


def bundle_python(app):
    """Unpack the checksum-verified CPython into app/python."""
    archive = fetch(PBS_URL, BUILD / 'downloads' / PBS_NAME, PBS_SHA256)
    work = Path(tempfile.mkdtemp(prefix='unpack-', dir=app.parent))
    try:
        with tarfile.open(archive) as package:
            package.extractall(work, filter='data')
        if not (work / 'python/bin/python3').exists():
            raise RuntimeError('Unexpected Python archive structure')
        (work / 'python').replace(app / 'python')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return bundled_python(app)


def copy_sources(destination):
    """Copy only what the wheel needs, so stale build/ output can never leak into it."""
    destination.mkdir(parents=True)
    for name in ('pyproject.toml', 'LICENSE', 'THIRD-PARTY-NOTICES.md'):
        shutil.copy2(PROJECT / name, destination / name)
    shutil.copytree(PROJECT / 'comic_archive', destination / 'comic_archive',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))


def build_wheel(work):
    source, out = work / 'src', work / 'wheel'
    for path in (source, out):
        shutil.rmtree(path, ignore_errors=True)
    copy_sources(source)
    run(['uv', 'build', '--wheel', '--out-dir', out, source])
    wheels = list(out.glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one newly built wheel')
    return wheels[0]


def uv_install(python, requirements):
    # No venv (they are not relocatable); the flag is harmless if the interpreter is not marked externally managed.
    run(['uv', 'pip', 'install', '--python', python, '--break-system-packages', '--compile-bytecode',
         '--constraint', PACKAGING / 'linux-requirements.txt', *requirements])


def drop_entry_scripts(app):
    """Remove console scripts that name the build path; the launcher is the entry point.

    uv writes either a '#!<python>' line or, for long or spaced paths, a '#!/bin/sh'
    line followed by an exec line holding the path; it is in the first two lines.
    """
    marker = str(app).encode()
    for path in sorted((app / 'python/bin').iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            head = b'\n'.join(stream.read(2048).split(b'\n')[:2])
        if marker in head:
            path.unlink()


def collect_licences(app):
    """Gather the licence texts the packages ship into app/licenses; the Qt LGPL text must be among them."""
    lib = app / 'python/lib'
    found = []
    for source in [*lib.glob('python*'), *lib.glob('python*/site-packages/PySide6'), *lib.glob('python*/site-packages/*.dist-info')]:
        if source.name.startswith('python'):
            prefixes, files = ('LICENSE',), source.glob('*')
        elif source.name == 'PySide6':
            prefixes, files = ('LICENSE', 'COPYING'), source.glob('*')
        else:
            prefixes, files = ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'), source.rglob('*')
        for path in files:
            if path.is_file() and not path.is_symlink() and path.name.upper().startswith(prefixes):
                target = app / 'licenses' / source.name / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                found.append(target)
    if not any('LESSER GENERAL PUBLIC LICENSE' in p.read_text('utf-8', 'replace').upper() for p in found):
        raise RuntimeError('No LGPL licence text for Qt was found in the installed packages; add it before shipping')
    return found


def frozen_requirements(text, release):
    lines = [l for l in text.splitlines() if l.strip() and not l.lower().startswith(('comic-archive', 'comic_archive'))]
    return '\n'.join([f'comic-archive=={release}', *sorted(lines)]) + '\n'


def install_packaging(app, release):
    (app / 'share/icons/hicolor/scalable/apps').mkdir(parents=True, exist_ok=True)
    shutil.copy2(PACKAGING / 'comic-archiver', app / 'comic-archiver')
    (app / 'comic-archiver').chmod(0o755)
    (app / 'share/applications').mkdir(parents=True, exist_ok=True)
    shutil.copy2(PACKAGING / 'comic-archiver.desktop', app / 'share/applications/comic-archiver.desktop')
    shutil.copy2(PACKAGING / 'README_FIRST.txt', app / 'README_FIRST.txt')
    shutil.copy2(PACKAGING / 'comic-archiver.svg', app / 'share/icons/hicolor/scalable/apps/comic-archiver.svg')
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.md'):
        shutil.copy2(PROJECT / name, app / name)
    (app / 'VERSION').write_text(release + '\n')


def archive_name(release):
    return f'{APP}-{release}-linux-x86_64.tar.gz'


def make_archive(app, destination, release):
    """Write destination/<name>.tar.gz of app (one top-level folder) and its sha256 file."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / archive_name(release)
    temporary = target.with_suffix(target.suffix + '.partial')

    def plain(info):
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        return info

    try:
        with temporary.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as packed:
                with tarfile.open(fileobj=packed, mode='w', format=tarfile.PAX_FORMAT) as package:
                    package.add(app, arcname=app.name, filter=plain)
            raw.flush()
            os.fsync(raw.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = target.with_name(target.name + '.sha256')
    checksum.write_text(f'{file_sha256(target)}  {target.name}\n')
    return target, checksum


def smoke(app, release):
    """Run the built launcher: version, off-screen self-test, then the built-in engine's own check."""
    launcher, python = app / 'comic-archiver', bundled_python(app)
    with tempfile.TemporaryDirectory(prefix='comic-smoke-') as temporary:
        home = Path(temporary)
        paths = {name: str(home / name) for name in ('state', 'data', 'config', 'cache')}
        env = clean_env(HOME=str(home), XDG_STATE_HOME=paths['state'], XDG_DATA_HOME=paths['data'],
                        XDG_CONFIG_HOME=paths['config'], XDG_CACHE_HOME=paths['cache'],
                        QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1',
                        # No GPU on CI hosts; unverified whether QtWebEngine needs this off-screen.
                        QTWEBENGINE_CHROMIUM_FLAGS=os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu'))
        # Run as on a computer with no display, so the checks cannot lean on one the build host happens to have.
        for name in ('DISPLAY', 'WAYLAND_DISPLAY'):
            env.pop(name, None)
        shown = run([launcher, '--version'], env=env, cwd=home, timeout=120, capture=True).stdout
        if release not in shown:
            raise RuntimeError('Packaged app reported an unexpected version')
        done = run([launcher, '--self-test'], env=env, cwd=home, timeout=300, capture=True)
        lines = done.stdout.strip().splitlines()
        try:
            ok = bool(lines) and json.loads(lines[-1]).get('ok') is True
        except ValueError:
            ok = False
        if not ok:
            print(done.stdout[-3000:], done.stderr[-3000:], sep='\n')
            raise RuntimeError('Packaged app self-test did not report ok true')
        print('Self-test report: ' + lines[-1], flush=True)
        # Starts the engine, opens a fresh session and page, and loads nothing from the internet.
        # The downloader has to hide itself and do without a graphics card unaided, as it does inside the app.
        for name in ('QT_QPA_PLATFORM', 'QTWEBENGINE_CHROMIUM_FLAGS'):
            env.pop(name, None)
        run([python, '-m', 'comic_archive', '--browser', 'qt', '--headless', 'doctor'], env=env, cwd=home, timeout=120)


def smoke_archive(app, archive, release):
    """Smoke the artifact people receive: unpacked in a path with a space, with the build tree gone."""
    shutil.rmtree(app)
    with tempfile.TemporaryDirectory(prefix='comic archiver smoke ') as temporary:
        with tarfile.open(archive) as package:
            package.extractall(temporary, filter='data')
        smoke(Path(temporary) / app.name, release)


def prepare(app):
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)


def build():
    if not shutil.which('uv'):
        raise RuntimeError('uv is required on the build host')
    release, app = version(), app_dir()
    prepare(app)
    work = BUILD / 'work'
    work.mkdir(exist_ok=True)
    python = bundle_python(app)
    wheel = build_wheel(work)
    uv_install(python, [f'{wheel}[{EXTRAS}]'])
    drop_entry_scripts(app)
    collect_licences(app)
    listing = run(['uv', 'pip', 'freeze', '--python', python], capture=True).stdout
    (app / 'requirements-frozen.txt').write_text(frozen_requirements(listing, release))
    run([sys.executable, PROJECT / 'scripts/audit_bundle.py', app / 'requirements-frozen.txt',
         '--output', app / 'dependency-audit.json'])
    install_packaging(app, release)
    return app, *make_archive(app, DIST, release)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--smoke', action='store_true', help='unpack the tarball elsewhere and run its launcher with --self-test off-screen')
    args = parser.parse_args(argv)
    try:
        require_linux_x86_64()
        app, archive, checksum = build()
        if args.smoke:
            smoke_archive(app, archive, version())
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        print('Linux app build failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'archive': str(archive), 'sha256': checksum.read_text().split()[0], 'smoke': args.smoke}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
