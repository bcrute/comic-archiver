"""Build the self-contained Linux x86_64 Comic Archiver app and its tarball.

Run on a Linux CI host that has uv, never on the development PC, with the project's test extra
(its pip-audit checks the finished app's packages):

    uv run --extra test python scripts/build_linux_app.py --smoke

Output: dist/ComicArchiver-<version>-linux-x86_64.tar.gz and a .sha256 file.

Pinned downloads: CPython from python-build-standalone (SHA-256 below).
Python packages come from PyPI pinned, with their hashes, by packaging/linux-requirements.txt;
the exact installed versions are audited and recorded in requirements-frozen.txt. The app carries no separate
browser: the downloader uses the Qt web engine that comes with PySide6. Qt's package holds every module Qt has, and
Python comes with Tk windows and the like; only what the app loads is kept in the folder.
This script reads no Tapas
session and makes no Tapas requests.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
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
# The one Python every build bundles and compiles its modules with; the other build scripts read it from here.
PYTHON_RELEASE = '3.14.8'
PBS_TAG = '20261009'
PBS_BASE = 'https://github.com/astral-sh/python-build-standalone/releases/download/' + PBS_TAG
# Every file in the tarball carries this time (1 January 2026 UTC), or SOURCE_DATE_EPOCH when that is set.
ARCHIVE_TIME = 1767225600


def pbs_name(platform_name):
    return f'cpython-{PYTHON_RELEASE}+{PBS_TAG}-{platform_name}-install_only_stripped.tar.gz'


PBS_NAME = pbs_name('x86_64-unknown-linux-gnu')
PBS_URL = PBS_BASE + '/' + PBS_NAME.replace('+', '%2B')
PBS_SHA256 = 'ddc902fd26460f728758c67d80417c1031edf97589f1f162c4ba3aa158a35998'
EXTRAS = 'desktop,validation'
# The license texts that come with no package in the app: Qt's (LGPL-3.0, which builds on GPL-3.0), Python's and
# those of the libraries built into this Python. They are kept in packaging/licenses; see SOURCES.md there.
LICENSES = PACKAGING / 'licenses'
LICENSE_TEXTS = ('GPL-3.0', 'LGPL-3.0', 'python-3.14', 'bzip2', 'expat', 'libedit', 'libffi', 'liblzma', 'libuuid',
                 'mpdecimal', 'ncurses', 'openssl', 'sqlite', 'zlib', 'zlib-ng', 'zstd')
# Qt comes as one package holding every module Qt has. These are the ones the app imports; what they load is
# worked out from the files themselves, and the rest of Qt's program files are left out of the app.
QT_MODULES = ('QtCore', 'QtGui', 'QtNetwork', 'QtQuick', 'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWidgets')
# Started by name while the app runs, which no file shows: the process that draws the pages.
QT_LOADED_BY_NAME = ('Qt/libexec/QtWebEngineProcess',)
# The plugins that put the window on the screen, on X11, on Wayland or on the bare screen, kept with what they load:
# which of them Qt asks for depends on the desktop of the computer the app runs on.
QT_SCREEN_PLUGINS = ('platforms', 'platformthemes', 'xcbglintegrations', 'egldeviceintegrations', 'wayland-decoration-client',
                     'wayland-graphics-integration-client', 'wayland-shell-integration')
# The other kinds of plugin a window with web pages asks Qt for, kept where they load nothing more. The other
# kinds belong to modules that are left out.
QT_PLUGINS = ('generic', 'iconengines', 'imageformats', 'networkinformation', 'platforminputcontexts', 'tls')
# In Qt's package for people who write programs with Qt, not for running one: the QML modules (the window is
# made of widgets), translations of Qt's own dialogs (the app shows none in another language), the pages'
# engine in its debugging form and the tools that inspect a page, type descriptions, headers and tools.
QT_UNUSED = ('Qt/qml', 'Qt/metatypes', 'Qt/translations/*.qm', 'Qt/resources/*.debug.*',
             'Qt/resources/qtwebengine_devtools_resources.pak', 'typesystems', 'include', 'glue', 'lib', 'doc', 'scripts',
             'QtAsyncio', '*.pyi')
# In the Python for Linux, and never loaded by the app: Tk windows (with Tcl and its libraries), the Berkeley DB
# module, Python's own editor and turtle drawings, and making new environments. pip stays, to replace Qt with.
PYTHON_UNUSED = ('bin/idle3*', 'lib/libtcl*', 'lib/tcl9*', 'lib/tk9*', 'lib/itcl*', 'lib/thread3*',
                 'lib/python3.*/tkinter', 'lib/python3.*/idlelib', 'lib/python3.*/turtledemo', 'lib/python3.*/turtle.py',
                 'lib/python3.*/__pycache__/turtle.*', 'lib/python3.*/ensurepip', 'lib/python3.*/venv',
                 'lib/python3.*/lib-dynload/_tkinter.*', 'lib/python3.*/lib-dynload/_dbm.*',
                 'lib/python3.*/site-packages/PIL/_imagingtk.*')


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
    # Bundled packages come from the public index alone; a host's mirror or extra index must not change them.
    env = {name: value for name, value in os.environ.items()
           if not (name.upper().startswith(('UV_', 'PIP_')) and ('INDEX' in name.upper() or name.upper().endswith('FIND_LINKS')))}
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


def require_audit():
    """The finished app's packages are audited by this Python: say so before an hour is spent building."""
    if importlib.util.find_spec('pip_audit') is None:
        raise RuntimeError('pip-audit is required in the Python that runs this script (the test extra)')


def require_version(shown, release):
    if release not in shown:
        raise RuntimeError('Packaged app reported an unexpected version')


def require_ok(done):
    """The self-test's own last line must say ok: its exit status alone is not the test."""
    lines = done.stdout.strip().splitlines()
    try:
        ok = bool(lines) and json.loads(lines[-1]).get('ok') is True
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        print(done.stdout[-3000:], done.stderr[-3000:], sep='\n')
        raise RuntimeError('Packaged app self-test did not report ok true')
    print('Self-test report: ' + lines[-1], flush=True)


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
    # The build backend is the one thing fetched to build the wheel: it is pinned, with its hashes.
    run(['uv', 'build', '--wheel', '--build-constraints', PACKAGING / 'build-constraints.txt', '--require-hashes',
         '--out-dir', out, source])
    wheels = list(out.glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one newly built wheel')
    return wheels[0]


def uv_install(python, requirements):
    # No venv (they are not relocatable); the flag is harmless if the interpreter is not marked externally managed.
    # Every package is checked against its pinned hash; the app's own wheel, built here, then needs none of its own.
    # Nothing is compiled here: a module compiled where it was installed names the build folder (compile_bytecode).
    command = ['uv', 'pip', 'install', '--python', python, '--break-system-packages', '--no-compile']
    run([*command, '--require-hashes', '--requirement', PACKAGING / 'linux-requirements.txt'])
    run([*command, '--no-deps', *requirements])
    # The wheel went in without its dependencies: the pinned packages must be all it asks for.
    run(['uv', 'pip', 'check', '--python', python])


def compile_bytecode(python, app, folder):
    """Compile every module under folder with python, so that no compiled file names the folder it was built in.

    A compiled module records the path of its source. Compiled in place, that is the build folder, the builder's
    user name included, and the app would differ with where it was built. The path is recorded from below app
    instead. The files are marked as never to be compared with their sources, so the times the archive gives
    the files do not matter, and any file the bundled Python compiled for itself earlier in the build is replaced.
    """
    found = run([python, '-I', '-c', 'import platform; print(platform.python_version())'], capture=True).stdout.strip()
    if found != PYTHON_RELEASE:
        # Compiled modules are only read by the Python version that wrote them.
        raise RuntimeError(f'The modules would be compiled by Python {found}, not {PYTHON_RELEASE}')
    command = [python, '-I', '-m', 'compileall', '-qq', '-f', '-j', '0', '--invalidation-mode', 'unchecked-hash',
               '-s', app, folder]
    print('Build stage: ' + ' '.join(str(a) for a in command), flush=True)
    # Its exit status is not the test: packages ship a few files that are not meant to compile.
    subprocess.run([str(a) for a in command], env=clean_env(), stdout=subprocess.DEVNULL, timeout=1800)
    marker = os.fsencode(app)
    for path in sorted(Path(folder).rglob('*.pyc')):
        if marker in path.read_bytes():
            raise RuntimeError('A compiled module names the build folder: ' + path.relative_to(app).as_posix())


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


def site_packages(app):
    found = list((app / 'python/lib').glob('python3.*/site-packages'))
    if len(found) != 1:
        raise RuntimeError('Expected exactly one site-packages folder in the bundled Python')
    return found[0]


def needed_names(path):
    """The names of the libraries Linux loads along with this program file, read from its dynamic section."""
    data = Path(path).read_bytes()
    try:
        if data[:4] != b'\x7fELF' or data[4:6] != b'\x02\x01':
            raise ValueError
        start, = struct.unpack_from('<Q', data, 0x20)
        size, count = struct.unpack_from('<HH', data, 0x36)
        loads, dynamic = [], None
        for n in range(count):
            kind, _, offset, address, _, stored, memory, _ = struct.unpack_from('<IIQQQQQQ', data, start + size * n)
            if kind == 1:
                loads.append((address, max(stored, memory), offset))
            elif kind == 2:
                dynamic = (offset, stored)
        if dynamic is None:
            return set()

        def place(address):
            for begin, length, offset in loads:
                if begin <= address < begin + length:
                    return offset + address - begin
            raise ValueError

        entries = []
        for n in range(dynamic[1] // 16):
            tag, value = struct.unpack_from('<qQ', data, dynamic[0] + 16 * n)
            if tag == 0:
                break
            entries.append((tag, value))
        strings = place(next(value for tag, value in entries if tag == 5))
        names = set()
        for tag, value in entries:
            if tag == 1:
                name = strings + value
                names.add(data[name:data.index(b'\0', name)].decode('ascii'))
        return names
    except (ValueError, StopIteration, struct.error, UnicodeDecodeError):
        raise RuntimeError(f'{Path(path).name} is not a Linux program file that can be read') from None


def qt_needed(needs):
    """The program files of Qt's package that the app can come to load.

    needs gives, for every program file in the package's folder (by its path inside it), the names it loads.
    Kept are the app's own Qt modules, the files named above, the plugins that show the window, whatever those
    load in turn, and the plugins of the other kinds the app asks for that load nothing but what is kept.
    """
    libraries = {name.rsplit('/', 1)[-1]: name for name in needs if '/' not in name or name.startswith('Qt/lib/')}
    wanted = [*(module + '.abi3.so' for module in QT_MODULES), *QT_LOADED_BY_NAME]
    absent = sorted(name for name in wanted if name not in needs)
    if absent:
        raise RuntimeError('The Qt package lacks ' + ', '.join(absent))
    plugins = {name: name.split('/')[2] for name in needs if name.startswith('Qt/plugins/') and name.count('/') == 3}
    wanted += [name for name, kind in plugins.items() if kind in QT_SCREEN_PLUGINS]
    kept = set()
    while wanted:
        name = wanted.pop()
        if name in kept:
            continue
        kept.add(name)
        wanted += [libraries[loaded] for loaded in needs[name] if loaded in libraries]
        # A Qt library's Python module is imported by the Python modules of the libraries built on it,
        # which no dynamic section shows: QtWebEngineWidgets imports QtPrintSupport.
        library = name.rsplit('/', 1)[-1]
        if library.startswith('libQt6') and library.endswith('.so.6'):
            wanted += [module for module in ['Qt' + library[6:-5] + '.abi3.so'] if module in needs]
    for name, kind in plugins.items():
        if kind in QT_PLUGINS and all(libraries[loaded] in kept for loaded in needs[name] if loaded in libraries):
            kept.add(name)
    return kept


def programs(folder):
    found = []
    for path in folder.rglob('*'):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                if stream.read(4) == b'\x7fELF':
                    found.append(path)
    return found


def leave_out_unused(app):
    """Remove from the app folder what the app never loads, which is most of what Qt's package holds.

    Afterwards no program file that is left may load one that was taken out.
    """
    before = sum(path.stat().st_size for path in app.rglob('*') if path.is_file() and not path.is_symlink())
    shipped = {path.name for path in programs(app)}
    qt = site_packages(app) / 'PySide6'
    for folder, patterns in ((app / 'python', PYTHON_UNUSED), (qt, QT_UNUSED)):
        for pattern in patterns:
            for path in list(folder.glob(pattern)):
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    needs = {path.relative_to(qt).as_posix(): needed_names(path) for path in programs(qt)}
    for name in needs.keys() - qt_needed(needs):
        (qt / name).unlink()
    for folder in sorted(qt.glob('Qt/plugins/*')):
        if not any(folder.iterdir()):
            folder.rmdir()
    left = programs(app)
    gone = shipped - {path.name for path in left}
    for path in left:
        missing = sorted(gone & needed_names(path))
        if missing:
            raise RuntimeError(f'{path.name} loads {", ".join(missing)}, which was left out of the app')
    after = sum(path.stat().st_size for path in app.rglob('*') if path.is_file() and not path.is_symlink())
    print(f'Left out what the app never loads: {(before - after) // 1_000_000} of {before // 1_000_000} MB', flush=True)


def copy_license_texts(app, names):
    """Copy the license texts named from packaging/licenses into app/licenses/other.

    Qt may only be passed on with the LGPL and the GPL it builds on, so both must be there and be version 3.
    """
    other = app / 'licenses/other'
    other.mkdir(parents=True, exist_ok=True)
    found = []
    for name in names:
        source = LICENSES / f'{name}.txt'
        if not source.is_file():
            raise RuntimeError(f'The license text {source.name} is missing from packaging/licenses')
        found.append(Path(shutil.copy2(source, other / source.name)))
    for name, title in (('LGPL-3.0', 'GNU LESSER GENERAL PUBLIC LICENSE'), ('GPL-3.0', 'GNU GENERAL PUBLIC LICENSE')):
        head = (other / f'{name}.txt').read_text('utf-8', 'replace')[:400] if (other / f'{name}.txt').is_file() else ''
        if title not in head or 'Version 3' not in head:
            raise RuntimeError(f'Qt cannot be passed on without the {name} license text in licenses/other/{name}.txt')
    return found


def collect_licenses(app):
    """Gather the license texts the packages ship into app/licenses, and the ones that come with none."""
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
    return found + copy_license_texts(app, LICENSE_TEXTS)


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

    moment = int(os.environ.get('SOURCE_DATE_EPOCH') or ARCHIVE_TIME)

    def plain(info):
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        info.mtime = moment
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
        require_version(run([launcher, '--version'], env=env, cwd=home, timeout=120, capture=True).stdout, release)
        require_ok(run([launcher, '--self-test'], env=env, cwd=home, timeout=300, capture=True))
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
    require_audit()
    release, app = version(), app_dir()
    prepare(app)
    work = BUILD / 'work'
    work.mkdir(exist_ok=True)
    python = bundle_python(app)
    wheel = build_wheel(work)
    uv_install(python, [f'{wheel}[{EXTRAS}]'])
    drop_entry_scripts(app)
    leave_out_unused(app)
    compile_bytecode(python, app, app / 'python/lib')
    if not list(site_packages(app).glob('comic_archive/__pycache__/desktop_app.*.pyc')):
        raise RuntimeError('The modules of the app were not compiled')
    collect_licenses(app)
    # list --format freeze reports installed versions even for CPython's preinstalled
    # pip, whose direct_url metadata points at the Python builder's private wheel path.
    listing = run(['uv', 'pip', 'list', '--format', 'freeze', '--python', python], capture=True).stdout
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
