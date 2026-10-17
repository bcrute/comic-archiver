"""Build the self-contained Windows x86_64 Comic Archiver app: its folder, a zip of it and its installer.

Run on a CI host that has uv, never on the development PC:

    python3 scripts/build_windows_app.py                    # the app folder and its zip; on Linux or Windows
    python scripts/build_windows_app.py --smoke --installer # on Windows only

The folder is put together from files alone: the pinned CPython for Windows (SHA-256 below) and the
Windows packages from PyPI. Nothing in it is run to make it, so a Linux host can make it. Starting
the app (--smoke) and building the installer (--installer, with Inno Setup 6 or 7) need Windows.

Qt's package holds every module Qt has, and Python comes with the means to install packages and to
build against it. Only what the app loads is kept in the folder.

Output: dist/ComicArchiver-<version>-windows-x86_64.zip with a .sha256 file, and with --installer
dist/ComicArchiver-<version>-windows-x86_64-setup.exe. This script reads no Tapas session and makes
no Tapas requests.
"""
import argparse
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
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_linux_app as common  # noqa: E402  The two builds share the download, wheel and run steps.

PROJECT = common.PROJECT
PACKAGING = PROJECT / 'packaging/windows'
BUILD = PROJECT / 'build/windows'
DIST = PROJECT / 'dist'
APP = common.APP
PBS_NAME = 'cpython-3.14.8+20261009-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'
PBS_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/' + common.PBS_TAG + '/'
           + PBS_NAME.replace('+', '%2B'))
PBS_SHA256 = '5b6fa7ddeabe23ae83d7b54e5043eb4fa1ccb10a73fa4c6b0f541dea8160d505'
PYTHON_VERSION = '3.14'
PLATFORM = 'x86_64-pc-windows-msvc'
# How the app is started. -I keeps Python to this folder (no PYTHON* settings, no user packages, no code
# from the folder it was started in); -X utf8 reads and writes text the same way on every computer.
START = ('-I', '-X', 'utf8', '-m', 'comic_archive.desktop_app')
# Inno Setup 7 reads the same definition as 6; the newer one is taken when both are installed.
# Qt comes as one package holding every module Qt has. These are the ones the app imports; what they load is
# worked out from the files themselves, and the rest of Qt's program files are left out of the app.
QT_MODULES = ('QtCore', 'QtGui', 'QtNetwork', 'QtQuick', 'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWidgets')
# Loaded by name while the app runs, which no file shows: the process that draws the pages, and the drawing in
# software that Qt turns to on a computer whose graphics it cannot use.
QT_LOADED_BY_NAME = ('QtWebEngineProcess.exe', 'opengl32sw.dll')
# The kinds of plugin a window with web pages asks Qt for. The others belong to modules that are left out.
QT_PLUGINS = ('imageformats', 'networkinformation', 'platforms', 'styles', 'tls')
# In Qt's package for people who write programs with Qt, not for running one: the QML modules (the window is
# made of widgets), translations of Qt's own dialogs (the app shows none in another language), the pages'
# engine in its debugging form and the tools that inspect a page, type descriptions, headers and tools.
QT_UNUSED = ('qml', 'translations/*.qm', 'resources/*.debug.*', 'resources/qtwebengine_devtools_resources.pak',
             'metatypes', 'typesystems', 'include', 'glue', 'lib', 'doc', 'scripts', 'QtAsyncio', '*.pyi', '*.lib')
# In the Python for Windows, and never loaded by the app: installing packages, building against Python,
# Tk windows (with Tcl and the two libraries only Tcl uses) and the modules of Python's own tests.
PYTHON_UNUSED = ('Lib/site-packages/pip', 'Lib/site-packages/pip-*.dist-info', 'Lib/ensurepip', 'Lib/venv', 'Scripts',
                 'include', 'libs', 'Lib/idlelib', 'Lib/tkinter', 'Lib/turtledemo', 'Lib/turtle.py', 'tcl',
                 'DLLs/_tkinter.pyd', 'DLLs/tcl*.dll', 'DLLs/libtommath.dll', 'DLLs/zlib1.dll',
                 'Lib/site-packages/PIL/_imagingtk.*', 'DLLs/_test*.pyd', 'DLLs/_ctypes_test.pyd')
PROGRAM_FILES = ('.dll', '.pyd', '.exe')
ISCC_PLACES = tuple((name, f'{folder}Inno Setup {version}/ISCC.exe') for version in (7, 6)
                    for name, folder in (('LOCALAPPDATA', 'Programs/'), ('ProgramFiles', ''), ('ProgramFiles(x86)', '')))


def on_windows():
    return platform.system() == 'Windows' and platform.machine() in ('AMD64', 'x86_64')


def app_dir():
    return BUILD / f'{APP}-{common.version()}'


def site_packages(app):
    return app / 'python/Lib/site-packages'


def unpack_python(archive, destination):
    """Unpack a checksum-verified CPython archive so that destination is its 'python' folder."""
    work = Path(tempfile.mkdtemp(prefix='unpack-', dir=destination.parent))
    try:
        with tarfile.open(archive) as package:
            package.extractall(work, filter='data')
        if not (work / 'python').is_dir():
            raise RuntimeError('Unexpected Python archive structure')
        (work / 'python').replace(destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def bundle_python(app):
    unpack_python(common.fetch(PBS_URL, BUILD / 'downloads' / PBS_NAME, PBS_SHA256), app / 'python')
    for name in ('python.exe', 'pythonw.exe', 'Lib/os.py'):
        if not (app / 'python' / name).is_file():
            raise RuntimeError('The Python for Windows lacks ' + name)


def install_packages(app, wheel):
    """Put the Windows builds of the app's packages into the bundled Python without running it."""
    common.run(['uv', 'pip', 'install', '--target', site_packages(app), '--python-platform', PLATFORM,
                '--python-version', PYTHON_VERSION, '--only-binary', ':all:', '--no-compile',
                f'{wheel}[{common.EXTRAS}]'])
    # Starters for the command line that name the build path; the app is started through its shortcut.
    shutil.rmtree(site_packages(app) / 'bin', ignore_errors=True)
    for name in ('PySide6/QtWebEngineCore.pyd', 'PySide6/QtWebEngineProcess.exe', 'zstandard', 'PIL', 'comic_archive'):
        if not (site_packages(app) / name).exists():
            raise RuntimeError('The Windows packages lack ' + name)


def imported_names(path):
    """The names, in lower case, of the program files that Windows loads along with this one.

    Read from the file's two import tables: the ordinary one, and the one for what is loaded on first use.
    """
    data = Path(path).read_bytes()
    try:
        header = struct.unpack_from('<I', data, 0x3c)[0]
        if data[:2] != b'MZ' or data[header:header + 4] != b'PE\0\0':
            raise ValueError
        count, optional_size = struct.unpack_from('<H', data, header + 6)[0], struct.unpack_from('<H', data, header + 20)[0]
        optional = header + 24
        tables = optional + {0x10b: 96, 0x20b: 112}[struct.unpack_from('<H', data, optional)[0]]
        # Each section: its size and place in memory, its size and place in the file.
        sections = [struct.unpack_from('<4I', data, optional + optional_size + 40 * n + 8) for n in range(count)]

        def place(address):
            for size, start, stored, offset in sections:
                if start <= address < start + max(size, stored):
                    return offset + address - start
            raise ValueError

        names = set()
        for table, entry_size, name_at in ((1, 20, 12), (13, 32, 4)):
            address = struct.unpack_from('<I', data, tables + 8 * table)[0]
            entry = place(address) if address else None
            while entry is not None and any(data[entry:entry + entry_size]):
                name = place(struct.unpack_from('<I', data, entry + name_at)[0])
                names.add(data[name:data.index(b'\0', name)].decode('ascii').lower())
                entry += entry_size
        return names
    except (ValueError, KeyError, struct.error):
        raise RuntimeError(f'{Path(path).name} is not a Windows program file that can be read') from None


def qt_needed(imports):
    """The program files of Qt's package that the app can come to load.

    imports gives, for every program file in the package's folder (by its path inside it), the names it
    loads. Kept are the app's own Qt modules, the files named above, whatever those load in turn, and the
    plugins of the kinds the app asks for that load nothing but what is kept.
    """
    top = {name.lower(): name for name in imports if '/' not in name}
    wanted = [*(module + '.pyd' for module in QT_MODULES), *QT_LOADED_BY_NAME]
    absent = sorted(name for name in wanted if name not in imports)
    if absent:
        raise RuntimeError('The Qt package lacks ' + ', '.join(absent))
    kept = set()
    while wanted:
        name = wanted.pop()
        if name in kept:
            continue
        kept.add(name)
        wanted += [top[loaded] for loaded in imports[name] if loaded in top]
        # A Qt library's Python module is imported by the Python modules of the libraries built on it,
        # which no import table shows: QtWebEngineWidgets imports QtPrintSupport.
        if name.startswith('Qt6') and name.endswith('.dll'):
            wanted += [top[module] for module in ['qt' + name[3:-4].lower() + '.pyd'] if module in top]
    for name in imports:
        parts = name.split('/')
        if (len(parts) == 3 and parts[0] == 'plugins' and parts[1] in QT_PLUGINS
                and all(top[loaded] in kept for loaded in imports[name] if loaded in top)):
            kept.add(name)
    return kept


def programs(folder):
    return [path for path in folder.rglob('*') if path.is_file() and path.suffix.lower() in PROGRAM_FILES]


def leave_out_unused(app):
    """Remove from the app folder what the app never loads, which is most of what Qt's package holds.

    Afterwards no program file that is left may load one that was taken out.
    """
    before = sum(path.stat().st_size for path in app.rglob('*') if path.is_file())
    shipped = {path.name.lower() for path in programs(app)}
    qt = site_packages(app) / 'PySide6'
    for folder, patterns in ((app / 'python', PYTHON_UNUSED), (qt, QT_UNUSED)):
        for pattern in patterns:
            for path in list(folder.glob(pattern)):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
    loads = {path.relative_to(qt).as_posix(): imported_names(path) for path in programs(qt)}
    for name in loads.keys() - qt_needed(loads):
        (qt / name).unlink()
    for folder in sorted(qt.glob('plugins/*')):
        if not any(folder.iterdir()):
            folder.rmdir()
    left = programs(app)
    gone = shipped - {path.name.lower() for path in left}
    for path in left:
        missing = sorted(gone & imported_names(path))
        if missing:
            raise RuntimeError(f'{path.name} loads {", ".join(missing)}, which was left out of the app')
    after = sum(path.stat().st_size for path in app.rglob('*') if path.is_file())
    print(f'Left out what the app never loads: {(before - after) // 1_000_000} of {before // 1_000_000} MB', flush=True)


def compiling_python(app):
    """A Python of the bundled version that runs on this computer: the bundled one, or its pinned Linux build."""
    if on_windows():
        return app / 'python/python.exe'
    common.require_linux_x86_64()
    folder = BUILD / 'compiler'
    if not (folder / 'bin/python3').is_file():
        shutil.rmtree(folder, ignore_errors=True)
        unpack_python(common.fetch(common.PBS_URL, BUILD / 'downloads' / common.PBS_NAME, common.PBS_SHA256), folder)
    return folder / 'bin/python3'


def compile_bytecode(app):
    """Compile every module now, so the first start is not slow and an install that cannot be written to still starts fast.

    The compiled files are marked as never to be compared with their sources: a zip rounds file times,
    and a compiled file that looks out of date is compiled again at every start.
    """
    command = [compiling_python(app), '-I', '-m', 'compileall', '-qq', '-j', '0', '--invalidation-mode', 'unchecked-hash',
               app / 'python/Lib']
    print('Build stage: ' + ' '.join(str(a) for a in command), flush=True)
    # Its exit status is not the test: packages ship a few files that are not meant to compile.
    subprocess.run([str(a) for a in command], env=common.clean_env(), stdout=subprocess.DEVNULL, timeout=1800)
    for module in ('__pycache__/os', 'site-packages/comic_archive/__pycache__/desktop_app', 'site-packages/PySide6/__pycache__/__init__'):
        if not list((app / 'python/Lib').glob(module + '.*.pyc')):
            raise RuntimeError('The modules of the app were not compiled')


def installed(app):
    """The packages in the bundled Python, as 'name==version' lines, read from what was installed."""
    found = []
    for path in site_packages(app).glob('*.dist-info/METADATA'):
        fields = dict(line.split(': ', 1) for line in path.read_text('utf-8', 'replace').split('\n\n')[0].splitlines()
                      if ': ' in line and not line.startswith(' '))
        found.append(f"{fields['Name']}=={fields['Version']}")
    # Sorted as text: file names sort differently on Windows and on Linux, and the list must not.
    return '\n'.join(sorted(found)) + '\n'


def collect_licences(app):
    """Gather the licence texts the packages ship into app/licenses; the Qt LGPL text must be among them."""
    found = []
    for source in [app / 'python', site_packages(app) / 'PySide6', *site_packages(app).glob('*.dist-info')]:
        prefixes = ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE')
        for path in (source.rglob('*') if source.suffix == '.dist-info' else source.glob('*')):
            if path.is_file() and not path.is_symlink() and path.name.upper().startswith(prefixes):
                target = app / 'licenses' / source.name / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                found.append(target)
    if not any('LESSER GENERAL PUBLIC LICENSE' in p.read_text('utf-8', 'replace').upper() for p in found):
        raise RuntimeError('No LGPL licence text for Qt was found in the installed packages; add it before shipping')
    return found


def install_packaging(app, release):
    for name in ('comic-archiver.ico', 'README_FIRST.txt', 'Comic Archiver.cmd'):
        shutil.copy2(PACKAGING / name, app / name)
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.md'):
        shutil.copy2(PROJECT / name, app / name)
    (app / 'VERSION').write_text(release + '\n')


def archive_name(release):
    return f'{APP}-{release}-windows-x86_64.zip'


def installer_name(release):
    return f'{APP}-{release}-windows-x86_64-setup.exe'


def make_archive(app, destination, release):
    """Write destination/<name>.zip of app (one top-level folder) and its sha256 file."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / archive_name(release)
    temporary = target.with_suffix(target.suffix + '.partial')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as package:
            for path in sorted(app.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    package.write(path, (Path(app.name) / path.relative_to(app)).as_posix())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = target.with_name(target.name + '.sha256')
    checksum.write_text(f'{common.file_sha256(target)}  {target.name}\n')
    return target, checksum


def build():
    if not shutil.which('uv'):
        raise RuntimeError('uv is required on the build host')
    release, app = common.version(), app_dir()
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)
    work = BUILD / 'work'
    work.mkdir(exist_ok=True)
    bundle_python(app)
    install_packages(app, common.build_wheel(work))
    leave_out_unused(app)
    compile_bytecode(app)
    collect_licences(app)
    (app / 'requirements-frozen.txt').write_text(common.frozen_requirements(installed(app), release))
    install_packaging(app, release)
    return app, *make_archive(app, DIST, release)


def smoke(app, release):
    """Start the built app as its shortcut does: version, off-screen self-test, then the built-in engine's own check."""
    python = app / 'python/python.exe'  # The shortcut uses pythonw.exe, which is the same program without a console to print to.
    with tempfile.TemporaryDirectory(prefix='comic smoke ') as temporary:
        home = Path(temporary)
        env = common.clean_env(USERPROFILE=str(home), LOCALAPPDATA=str(home / 'local'), APPDATA=str(home / 'roaming'),
                               QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1',
                               QTWEBENGINE_CHROMIUM_FLAGS=os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu'))
        shown = common.run([python, *START, '--version'], env=env, cwd=home, timeout=120, capture=True).stdout
        if release not in shown:
            raise RuntimeError('Packaged app reported an unexpected version')
        done = common.run([python, *START, '--self-test'], env=env, cwd=home, timeout=300, capture=True)
        lines = done.stdout.strip().splitlines()
        try:
            ok = bool(lines) and json.loads(lines[-1]).get('ok') is True
        except ValueError:
            ok = False
        if not ok:
            print(done.stdout[-3000:], done.stderr[-3000:], sep='\n')
            raise RuntimeError('Packaged app self-test did not report ok true')
        print('Self-test report: ' + lines[-1], flush=True)
        # The downloader has to hide itself and do without a graphics card unaided, as it does inside the app.
        for name in ('QT_QPA_PLATFORM', 'QTWEBENGINE_CHROMIUM_FLAGS'):
            env.pop(name, None)
        common.run([python, '-I', '-X', 'utf8', '-m', 'comic_archive', '--browser', 'qt', '--headless', 'doctor'],
                   env=env, cwd=home, timeout=120)


def smoke_archive(archive, release):
    """Smoke what people receive: unpacked in a path with a space, apart from the build tree."""
    with tempfile.TemporaryDirectory(prefix='comic archiver smoke ') as temporary:
        with zipfile.ZipFile(archive) as package:
            package.extractall(temporary)  # noqa: S202 - the release archive this build has just made
        smoke(Path(temporary) / f'{APP}-{release}', release)


def find_iscc(given=None, environ=None):
    environ = os.environ if environ is None else environ
    places = [given] if given else [shutil.which('ISCC'), *(Path(environ[name]) / rest for name, rest in ISCC_PLACES
                                                             if environ.get(name))]
    for place in places:
        if place and Path(place).is_file():
            return Path(place)
    raise RuntimeError('Inno Setup 6 or 7 (ISCC.exe) was not found; install it or give its place with --iscc')


def make_installer(app, destination, release, iscc):
    common.run([iscc, '/Qp', f'/DAppVersion={release}', f'/DSourceDir={app}', f'/DOutputDir={destination}',
                f'/DOutputName={installer_name(release)[:-4]}', f"/DStartParameters={' '.join(START)}",
                PACKAGING / 'comic-archiver.iss'], timeout=3600)
    target = destination / installer_name(release)
    if not target.is_file():
        raise RuntimeError('Inno Setup wrote no installer')
    checksum = target.with_name(target.name + '.sha256')
    checksum.write_text(f'{common.file_sha256(target)}  {target.name}\n')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--smoke', action='store_true', help='unpack the zip elsewhere and start the app with --self-test off-screen (Windows only)')
    parser.add_argument('--installer', action='store_true', help='also build the installer with Inno Setup (Windows only)')
    parser.add_argument('--iscc', help='where ISCC.exe is, if it is not in its usual place')
    args = parser.parse_args(argv)
    result = {}
    try:
        if (args.smoke or args.installer) and not on_windows():
            raise RuntimeError('--smoke and --installer need Windows x86_64; without them the app folder can be built here')
        iscc = find_iscc(args.iscc) if args.installer else None
        app, archive, checksum = build()
        result = {'archive': str(archive), 'sha256': checksum.read_text().split()[0], 'smoke': args.smoke}
        if args.smoke:
            smoke_archive(archive, common.version())
        if args.installer:
            result['installer'] = str(make_installer(app, DIST, common.version(), iscc))
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        print('Windows app build failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
