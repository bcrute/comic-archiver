"""Build a relocatable macOS .app with its own Python and Qt; run only on the fleet.

Uses the shared wheel, checksum, license and audit helpers. No site credentials
or distribution signing identity are needed. The resulting app is ad-hoc signed.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile

import build_linux_app as shared

# Public python-build-standalone artifact checksums, not credentials.
X86_64_SHA256 = '85f8e54798e8e6aa58d2253f013d815c5616343cc0d15cbea882fca78bdfeae2'
ARM64_SHA256 = 'f15ec3d15460dd66c2eb88639b152cd9040e7d1ece11e38bf89e8f53f0171e02'
PINS = {'x86_64': X86_64_SHA256, 'arm64': ARM64_SHA256}
BUILD = shared.PROJECT / 'build/macos'
MACH_CPU = {0x01000007: 'x86_64', 0x0100000C: 'arm64'}
THIN_MACH = {b'\xce\xfa\xed\xfe': '<', b'\xcf\xfa\xed\xfe': '<',
             b'\xfe\xed\xfa\xce': '>', b'\xfe\xed\xfa\xcf': '>'}
FAT_MACH = {b'\xca\xfe\xba\xbe': '>', b'\xca\xfe\xba\xbf': '>',
            b'\xbe\xba\xfe\xca': '<', b'\xbf\xba\xfe\xca': '<'}
OPTIONAL_QT = ('Qt3D', 'QtQuick3D', 'QtDesigner', 'QtGraphs')
OPTIONAL_PLUGINS = ('assetimporters', 'designer', 'geometryloaders', 'qmllint', 'qmltooling',
                    'renderers', 'renderplugins', 'sceneparsers')
FRAMEWORK_LOAD = re.compile(r'/([A-Za-z0-9_]+)\.framework/')


def environment(**extra):
    # Retain the fleet's acquiring cache and CA. Never introduce a proxy or TLS bypass.
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1')
    env.update(extra)
    return env


def run(args, **kwargs):
    extra = kwargs.pop('env', {})
    return shared.run(args, env=environment(**extra), **kwargs)


def architecture():
    machine = platform.machine()
    if platform.system() != 'Darwin' or machine not in PINS:
        raise RuntimeError('Build the Mac app on macOS x86_64 or arm64.')
    return machine


def build_network_probe(folder):
    arch = architecture()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/'macos-network-probe'
    run(['xcrun', 'clang', '-Os', '-arch', arch, '-mmacosx-version-min=13.0', '-fblocks', '-framework', 'Network',
         shared.PACKAGING/'macos/network_probe.c', '-o', target])
    run(['codesign', '--force', '--sign', '-', target])
    return target


def launcher(contents):
    """A native bundle entry point finds the interpreter relative to itself."""
    source = BUILD / 'launcher.c'
    source.write_text(r'''#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], python[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, resolved)) return 1;
    char *slash = strrchr(resolved, '/');
    if (!slash) return 1;
    *slash = '\0';
    if (snprintf(python, sizeof(python), "%s/../Resources/python/bin/python3", resolved) >= sizeof(python)) return 1;
    char **args = calloc((size_t)argc + 6, sizeof(char *));
    if (!args) return 1;
    args[0] = python; args[1] = "-I"; args[2] = "-B"; args[3] = "-m"; args[4] = "comic_archive.desktop_app";
    for (int i = 1; i < argc; i++) args[i + 4] = argv[i];
    unsetenv("PYTHONHOME"); unsetenv("PYTHONPATH");
    /* The signed bundle stays immutable, including in Python worker children. */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    execv(python, args);
    perror("Comic Archiver could not start");
    return 1;
}
''')
    executable = contents/'MacOS/ComicArchiver'
    executable.parent.mkdir(parents=True)
    run(['xcrun', 'clang', '-Os', '-arch', architecture(), '-mmacosx-version-min=13.0', source, '-o', executable])
    return executable


def icon(resources):
    """Render the existing application artwork into Finder's icon sizes."""
    from PySide6.QtGui import QGuiApplication
    from make_windows_icon import drawn
    application = QGuiApplication.instance() or QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix='icon-', dir=BUILD) as temporary:
        iconset = Path(temporary)/'ComicArchiver.iconset'
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ''), (2, '@2x')):
                drawn(size*scale).save(iconset/f'icon_{size}x{size}{suffix}.png')
        run(['iconutil', '-c', 'icns', '-o', resources/'ComicArchiver.icns', iconset])
    del application


def mach_architectures(path):
    with path.open('rb') as stream:
        header = stream.read(8)
    if len(header) < 8:
        return ()
    magic = header[:4]
    if magic in THIN_MACH:
        cpu, = struct.unpack(THIN_MACH[magic] + 'I', header[4:])
        return (MACH_CPU.get(cpu, f'cpu-{cpu:x}'),)
    if magic in FAT_MACH and 1 <= struct.unpack(FAT_MACH[magic] + 'I', header[4:])[0] <= 16:
        return tuple(run(['xcrun', 'lipo', '-archs', path], capture=True).stdout.split())
    return ()


def bundle_size(folder):
    return sum(path.stat().st_size for path in folder.rglob('*') if not path.is_symlink() and path.is_file())


def keep_architecture(folder, arch):
    binaries = []
    thinned = 0
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        arches = mach_architectures(path)
        if not arches:
            continue
        binaries.append(path)
        if arches == (arch,):
            continue
        if arch not in arches:
            raise RuntimeError(f'{path.relative_to(folder)} has no {arch} slice: {arches}')
        # A Mach-O slice retains its own embedded signature. Preserve the file
        # mode and layout so nested framework and helper signatures can be verified.
        with tempfile.TemporaryDirectory(prefix='.thin-', dir=path.parent) as temporary:
            output = Path(temporary) / path.name
            run(['xcrun', 'lipo', path, '-thin', arch, '-output', output])
            if mach_architectures(output) != (arch,):
                raise RuntimeError(f'Unexpected architecture after thinning {path.name}.')
            output.chmod(stat.S_IMODE(path.stat().st_mode))
            output.replace(path)
        thinned += 1
    return binaries, thinned


def framework_loads(binaries):
    """Read LC_LOAD_DYLIB paths with the platform tool, including weak links."""
    needs = {}
    for index in range(0, len(binaries), 32):
        batch = binaries[index:index+32]
        names = {str(path) + ':': path for path in batch}
        current = None
        for line in run(['xcrun', 'otool', '-L', *batch], capture=True).stdout.splitlines():
            if line in names:
                current = names[line]
                needs[current] = set()
            elif current is not None and line.startswith('\t'):
                needs[current].update(FRAMEWORK_LOAD.findall(line))
        if any(path not in needs for path in batch):
            raise RuntimeError('Could not determine the linked Qt frameworks for every bundled binary.')
    return needs


def prune_qt(qt, binaries):
    """Remove optional Qt families only when retained Mach-O files do not link them."""
    candidates = {path.stem: path for path in (qt/'Qt/lib').glob('*.framework')
                  if path.stem.startswith(OPTIONAL_QT)}
    plugins = [qt/'Qt/plugins'/kind for kind in OPTIONAL_PLUGINS]
    plugins = [path for path in plugins if path.is_dir()]
    owners = {}
    for binary in binaries:
        framework = next((parent.stem for parent in binary.parents if parent.suffix == '.framework'), None)
        owners[binary] = binary.name.split('.', 1)[0] if binary.parent == qt else framework
    loads = framework_loads([path for path in binaries if not any(plugin in path.parents for plugin in plugins)])
    omitted = set(candidates)
    while omitted:
        referenced = {name for path, linked in loads.items() if owners[path] not in omitted for name in linked}
        kept = omitted & referenced
        if not kept:
            break
        omitted -= kept
    for name in sorted(omitted):
        shutil.rmtree(candidates[name])
        (qt/f'{name}.abi3.so').unlink(missing_ok=True)
    for plugin in plugins:
        shutil.rmtree(plugin)
    return sorted(omitted)


def build():
    arch = architecture()
    shared.require_audit()
    release = shared.version()
    app = BUILD/'Comic Archiver.app'
    shutil.rmtree(app, ignore_errors=True)
    resources = app/'Contents/Resources'
    resources.mkdir(parents=True)
    name = shared.pbs_name(('aarch64' if arch == 'arm64' else arch) + '-apple-darwin')
    base = os.environ.get('COMIC_ARCHIVER_BUILD_DOWNLOAD_BASE', shared.PBS_BASE)
    archive = shared.fetch(base.rstrip('/')+'/'+name.replace('+', '%2B'), BUILD/'downloads'/name, PINS[arch])
    with tarfile.open(archive) as package:
        package.extractall(resources, filter='data')
    python = resources/'python/bin/python3'
    source, out = BUILD/'work/src', BUILD/'work/wheel'
    shutil.rmtree(BUILD/'work', ignore_errors=True)
    shared.copy_sources(source)
    # A preceding source-checkout probe may have compiled this helper. Keep the
    # wheel platform-neutral; the bundle compiles its native helper after install.
    (source/'comic_archive/native/macos-network-probe').unlink(missing_ok=True)
    run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', out, source])
    wheels = list(out.glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one fresh wheel')
    wheel = wheels[0]
    # Python's own pip avoids adding a build-host tool dependency to the application.
    # Nothing is compiled here: a module compiled where it was installed names the build folder.
    run([python, '-m', 'pip', 'install', '--no-compile', '--require-hashes', '-r', shared.PACKAGING/'macos-requirements.txt'])
    run([python, '-m', 'pip', 'install', '--no-compile', '--no-deps', wheel])
    # The wheel went in without its dependencies: the pinned packages must be all it asks for.
    run([python, '-m', 'pip', 'check'])
    build_network_probe(shared.site_packages(resources)/'comic_archive/native')
    qt = shared.site_packages(resources)/'PySide6'
    original_size = bundle_size(resources)
    for relative in ('include', 'typesystems', 'glue', 'Qt/qml', 'Qt/metatypes'):
        shutil.rmtree(qt/relative, ignore_errors=True)
    for stub in qt.glob('*.pyi'):
        stub.unlink()
    binaries, thinned = keep_architecture(resources, arch)
    after_thinning = bundle_size(resources)
    removed = prune_qt(qt, binaries)
    print(json.dumps({'target_architecture': arch, 'thinned_binaries': thinned, 'removed_qt_frameworks': removed,
                      'input_bytes': original_size, 'thinned_bytes': after_thinning,
                      'trimmed_bytes': bundle_size(resources)}), flush=True)
    shared.drop_entry_scripts(resources)
    shared.compile_bytecode(python, resources, resources/'python/lib')
    shared.collect_licenses(resources)
    listing = run([python, '-m', 'pip', 'list', '--format', 'freeze'], capture=True).stdout
    requirements = resources/'requirements-frozen.txt'
    requirements.write_text(shared.frozen_requirements(listing, release))
    run([sys.executable, shared.PROJECT/'scripts/audit_bundle.py', requirements,
         '--output', resources/'dependency-audit.json', '--platform', 'macos'])
    for filename in ('LICENSE', 'THIRD-PARTY-NOTICES.md'):
        shutil.copy2(shared.PROJECT/filename, resources/filename)
    shutil.copy2(shared.PACKAGING/'macos/README_FIRST.txt', resources/'README_FIRST.txt')
    (resources/'VERSION').write_text(release+'\n')
    icon(resources)
    with (app/'Contents/Info.plist').open('wb') as stream:
        plistlib.dump({'CFBundleExecutable':'ComicArchiver', 'CFBundleName':'Comic Archiver',
                      'CFBundleDisplayName':'Comic Archiver', 'CFBundleIdentifier':'org.comicarchiver.desktop',
                      'CFBundlePackageType':'APPL', 'CFBundleShortVersionString':release,
                      'CFBundleVersion':release, 'CFBundleIconFile':'ComicArchiver.icns',
                      'LSMinimumSystemVersion':'13.0',
                      'NSHighResolutionCapable':True}, stream)
    launcher(app/'Contents')
    run(['codesign', '--force', '--sign', '-', app])
    run(['codesign', '--verify', '--deep', '--strict', app])
    shared.DIST.mkdir(exist_ok=True)
    destination = shared.DIST/f'ComicArchiver-{release}-macos-{arch}.zip'
    partial = destination.with_suffix('.partial.zip')
    partial.unlink(missing_ok=True)
    run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, partial])
    partial.replace(destination)
    destination.with_suffix('.zip.sha256').write_text(f'{shared.file_sha256(destination)}  {destination.name}\n')
    return app, destination


def smoke(app, archive):
    # Remove the build tree and launch a relocated copy of the delivered ZIP.
    shutil.rmtree(app)
    with tempfile.TemporaryDirectory(prefix='comic mac relocated ') as temporary:
        root = Path(temporary)
        run(['ditto', '-x', '-k', archive, root])
        moved = root/app.name
        run(['codesign', '--verify', '--deep', '--strict', moved])
        # Every start of the delivered app runs in a home of its own: none can read or change the
        # settings, the sign-ins or a library of whoever builds it.
        home = root/'Synthetic home'
        home.mkdir()
        isolated = {'HOME':str(home), 'XDG_STATE_HOME':str(home/'state'),
                    'XDG_CACHE_HOME':str(home/'cache'), 'XDG_DATA_HOME':str(home/'data')}
        shared.require_version(run([moved/'Contents/MacOS/ComicArchiver', '--version'], cwd=root, env=isolated,
                                   timeout=120, capture=True).stdout, shared.version())
        shared.require_ok(run([moved/'Contents/MacOS/ComicArchiver', '--self-test'], cwd=root, timeout=300,
                              env=isolated | {'QT_QPA_PLATFORM':'cocoa'}, capture=True))
        python = moved/'Contents/Resources/python/bin/python3'
        run([python, '-I', '-B', '-c',
             'import os,subprocess; from pathlib import Path; from comic_archive import macos; '
             'assert macos.runs_command(os.getpid(), ["-I"]); '
             'subprocess.run([str(Path(macos.__file__).parent/"native/macos-network-probe")], check=True, timeout=5)'],
            cwd=root, env=isolated, timeout=30)
        run([python, '-I', '-B', '-m', 'comic_archive', '--browser', 'qt', '--headless', 'doctor'], cwd=root,
            env=isolated, timeout=180)
        # Exercise the delivered removal command against real, synthetic state.
        run([python, '-I', '-B', '-c', '''
from comic_archive import app_runtime, sites
from comic_archive.session import store_session
library = app_runtime.default_root()
store_session({'cookies':[{'name':'session', 'value':'SYNTHETIC_MAC_SMOKE',
                          'domain':sites.DEFAULT.cookie_domains[0]}], 'origins':[]}, library/'.comic-session', sites.DEFAULT)
saved = library/'Library/Series/001.cbz'
saved.parent.mkdir(parents=True, exist_ok=True)
saved.write_bytes(b'synthetic saved episode')
cloud = library/'.comic-shared/cloud'
cloud.mkdir(parents=True)
(cloud/'fixture.conf').write_text('[b2]\\nkey = SYNTHETIC_MAC_SMOKE\\n')
app_runtime.remember_library(library)
from pathlib import Path
for kept in ('Library/Caches', 'Library/Application Support'):
    (Path.home()/kept/app_runtime.FOLDER_NAME/'deep').mkdir(parents=True)
    (Path.home()/kept/'Another app').mkdir()
'''], cwd=root, env=isolated, timeout=30)
        run([moved/'Contents/MacOS/ComicArchiver', '--uninstall', '--quiet'],
            cwd=root, env=isolated, timeout=60)
        run([python, '-I', '-B', '-c', '''
from comic_archive import app_runtime
library = app_runtime.default_root()
assert (library/'Library/Series/001.cbz').read_bytes() == b'synthetic saved episode'
assert not (library/'.comic-session/state.json').exists()
assert not (library/'.comic-session/state.tmp').exists()
assert not (library/'.comic-shared/cloud/fixture.conf').exists()
from pathlib import Path
for kept in ('Library/Caches', 'Library/Application Support'):
    assert not (Path.home()/kept/app_runtime.FOLDER_NAME).exists()
    assert (Path.home()/kept/'Another app').is_dir()
print('Packaged uninstall removed synthetic sign-in and key; saved episode preserved.')
'''], cwd=root, env=isolated, timeout=30)
        run(['codesign', '--verify', '--deep', '--strict', moved])


def main():
    if not __debug__:
        raise SystemExit('The smoke checks are asserts: run this script without -O.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--prepare-native', action='store_true', help='prepare the source checkout network helper')
    args = parser.parse_args()
    if args.prepare_native:
        build_network_probe(shared.PROJECT/'comic_archive/native')
        return
    app, archive = build()
    if args.smoke:
        smoke(app, archive)
    print(json.dumps({'archive':str(archive), 'sha256':shared.file_sha256(archive),
                      'smoke':args.smoke, 'signing':'ad-hoc', 'notarized':False}), flush=True)


if __name__ == '__main__':
    main()
