"""The Mac build cuts every program file down to the one processor and removes the parts of Qt nothing loads.

The Mac's own tools are stood in for here, so these checks run on any build machine. That the finished app
starts is shown only by the build's own run of it on a Mac.
"""
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if os.name == 'nt':
    raise unittest.SkipTest('The Mac app is laid out with links and file modes that Windows does not keep.')
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
import build_macos_app as build  # noqa: E402

CPU = {'x86_64': 0x01000007, 'arm64': 0x0100000C}
UNIVERSAL = {b'\xca\xfe\xba\xbe': '>', b'\xbe\xba\xfe\xca': '<'}
PACKAGE = 'python/lib/python3.14/site-packages/PySide6/'
# The four families of Qt the app never uses, as a small package holds them; the last three have no Python module.
FAMILIES = ('Qt3DCore', 'Qt3DRender', 'QtQuick3D', 'QtDesigner', 'QtGraphs', 'QtGraphsWidgets',
            'QtQuick3DUtils', 'QtQuick3DRuntimeRender', 'QtDesignerComponents')
UNUSED_PLUGINS = ('assetimporters', 'designer', 'geometryloaders', 'qmllint', 'qmltooling', 'renderers', 'renderplugins',
                  'sceneparsers')


def thin(arch, body=b'', order='<', magic=0xFEEDFACF):
    """A program file for one processor, as far as the build reads it: the magic number, then the processor."""
    return struct.pack(order + 'II', magic, CPU.get(arch, arch)) + body


def universal(slices, order='>'):
    """A program file for several processors: how many, where each one's part lies and how long it is, then the parts."""
    start, table, data = 8 + 20 * len(slices), b'', b''
    for arch, part in slices.items():
        table += struct.pack(order + '5I', CPU[arch], 3, start + len(data), len(part), 0)
        data += part
    return struct.pack(order + 'II', 0xCAFEBABE, len(slices)) + table + data


def parts(path):
    """Each processor's part of a file made by universal(). Any other file fails, as it does for lipo."""
    data = Path(path).read_bytes()
    try:
        order = UNIVERSAL[data[:4]]
        count, = struct.unpack_from(order + 'I', data, 4)
        entries = [struct.unpack_from(order + '5I', data, 8 + 20 * index) for index in range(count)]
    except (KeyError, struct.error):
        raise subprocess.CalledProcessError(1, 'lipo') from None
    names = {cpu: arch for arch, cpu in CPU.items()}
    return {names[cpu]: data[offset:offset + size] for cpu, _, offset, size, _ in entries}


def finished(text=''):
    return subprocess.CompletedProcess([], 0, stdout=text, stderr='')


class MacTools:
    """Stands in for the tools of a Mac: lipo reads and cuts the universal files above, otool lists what was noted."""
    def __init__(self):
        self.loads, self.fails, self.mixes_up = {}, (), ()

    def __call__(self, args, capture=False, **options):
        tool, *rest = [str(arg) for arg in args][1:]
        if tool == 'lipo' and rest[0] == '-archs':
            return finished(' '.join(parts(rest[1])) + '\n')
        if tool == 'lipo':
            source, _, arch, _, output = rest
            found = parts(source)
            if Path(source).name in self.fails or arch not in found:
                raise subprocess.CalledProcessError(1, 'lipo')
            Path(output).write_bytes(found['x86_64' if Path(source).name in self.mixes_up else arch])
            return finished()
        if tool == 'otool':
            return finished(''.join(f'{path}:\n' + ''.join(f'\t{load} (compatibility version 6.0.0, current version 6.11.2)\n'
                                                           for load in self.loads[Path(path)])
                                    for path in rest[1:] if Path(path) in self.loads))
        raise AssertionError(args)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.resources = self.root / 'Comic Archiver.app/Contents/Resources'
        self.tools = MacTools()
        replaced = patch.object(build, 'run', self.tools)
        replaced.start()
        self.addCleanup(replaced.stop)

    def put(self, name, data, mode=0o644):
        path = self.resources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def snapshot(self):
        """Every path in the app: where a link leads, or a file's permission bits and bytes."""
        return {path.relative_to(self.resources).as_posix():
                os.readlink(path) if path.is_symlink() else None if path.is_dir()
                else (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
                for path in self.resources.rglob('*')}


class ArchitectureTests(WorkspaceCase):
    def test_a_program_for_one_processor_is_read_from_its_own_header_without_any_tool(self):
        cases = {'intel': (thin('x86_64'), ('x86_64',)), 'arm': (thin('arm64', b'the rest of the file'), ('arm64',)),
                 'other byte order': (thin('arm64', order='>'), ('arm64',)),
                 'intel 32 bit': (thin(7, magic=0xFEEDFACE), ('cpu-7',)),
                 'powerpc': (thin(0x12, order='>', magic=0xFEEDFACE), ('cpu-12',))}
        with patch.object(build, 'run', side_effect=AssertionError('no tool is needed')):
            for name, (data, expected) in cases.items():
                with self.subTest(name):
                    self.assertEqual(build.mach_architectures(self.put(name, data)), expected)

    def test_a_universal_program_names_every_processor_it_holds_as_the_mac_tool_reads_them(self):
        both = {'x86_64': thin('x86_64', b'for Intel'), 'arm64': thin('arm64', b'for Apple silicon')}
        self.assertEqual(build.mach_architectures(self.put('both', universal(both))), ('x86_64', 'arm64'))
        self.assertEqual(build.mach_architectures(self.put('other byte order', universal(both, order='<'))), ('x86_64', 'arm64'))
        self.assertEqual(build.mach_architectures(self.put('one', universal({'arm64': thin('arm64')}))), ('arm64',))
        # A universal file with 64-bit offsets starts the same way but for the last digit of its magic number.
        wide = self.put('wide', struct.pack('>II', 0xCAFEBABF, 2))
        with patch.object(build, 'run', return_value=finished('x86_64 arm64\n')):
            self.assertEqual(build.mach_architectures(wide), ('x86_64', 'arm64'))

    def test_a_file_that_is_no_mac_program_has_no_processor_and_starts_no_tool(self):
        cases = {'empty': b'', 'text': b'#!/bin/sh\nexec python3 "$@"\n', 'linux': b'\x7fELF\x02\x01\x01\x00' + bytes(56),
                 'cut short': thin('arm64')[:7], 'magic only': universal({})[:4], 'no parts': universal({}),
                 # A compiled Java class starts like a universal program; where the count would be it has its version.
                 'java': struct.pack('>IHH', 0xCAFEBABE, 0, 65), 'too many parts': struct.pack('>II', 0xCAFEBABE, 17)}
        with patch.object(build, 'run', side_effect=AssertionError('no tool is needed')):
            for name, data in cases.items():
                with self.subTest(name):
                    self.assertEqual(build.mach_architectures(self.put(name, data)), ())


class ThinningTests(WorkspaceCase):
    def both(self, name):
        """A universal program whose two parts can be told apart, and from those of every other file."""
        return universal({'x86_64': thin('x86_64', name.encode() + b' for Intel'),
                          'arm64': thin('arm64', name.encode() + b' for Apple silicon')})

    def test_universal_programs_are_cut_down_to_this_mac_and_nothing_else_in_the_app_changes(self):
        framework = PACKAGE + 'Qt/lib/QtCore.framework/'
        cut = {PACKAGE + 'QtCore.abi3.so': 0o755, framework + 'Versions/A/QtCore': 0o755,
               PACKAGE + 'Qt/plugins/platforms/libqcocoa.dylib': 0o600}
        for name, mode in cut.items():
            self.put(name, self.both(name), mode)
        already = [self.put('python/bin/python3.14', thin('arm64', b'the interpreter'), 0o755),
                   self.put(PACKAGE + 'libpyside6.abi3.6.11.dylib', thin('arm64', b'bindings'), 0o755)]
        self.put(PACKAGE + '__init__.py', b'import sys\n')
        self.put(framework + 'Versions/A/Resources/Info.plist', b'<plist/>')
        self.put(PACKAGE + 'Qt/resources/icudtl.dat', bytes(range(256)))
        # A framework is reached through links: each program in it is one file, however many names lead to it.
        (self.resources / framework / 'Versions/Current').symlink_to('A')
        (self.resources / framework / 'QtCore').symlink_to('Versions/Current/QtCore')
        (self.resources / framework / 'Resources').symlink_to('Versions/Current/Resources')
        (self.resources / 'python/bin/python3').symlink_to('python3.14')
        # A link that leads out of the app is not the app's file to change.
        outside = self.root / 'elsewhere/libssl.3.dylib'
        outside.parent.mkdir()
        outside.write_bytes(self.both('libssl'))
        (self.resources / 'python/lib').mkdir(exist_ok=True)
        (self.resources / 'python/lib/libssl.3.dylib').symlink_to(outside)
        expected = self.snapshot()
        for name, mode in cut.items():
            expected[name] = (mode, thin('arm64', name.encode() + b' for Apple silicon'))
        binaries, thinned = build.keep_architecture(self.resources, 'arm64')
        self.assertEqual(self.snapshot(), expected)
        self.assertEqual(thinned, 3)
        self.assertEqual(binaries, sorted([*already, *(self.resources / name for name in cut)]))
        self.assertEqual(outside.read_bytes(), self.both('libssl'))

    def test_a_program_with_no_part_for_this_mac_stops_the_build_and_is_named(self):
        # It could not be loaded on the Mac the app is built for; the app would start and then fail.
        self.put('python/lib/libintel.dylib', thin('x86_64', b'only for Intel'))
        with self.assertRaisesRegex(RuntimeError, r'python/lib/libintel\.dylib has no arm64 slice'):
            build.keep_architecture(self.resources, 'arm64')
        self.put('python/lib/libintel.dylib', universal({'x86_64': thin('x86_64', b'only for Intel')}))
        with self.assertRaisesRegex(RuntimeError, r'python/lib/libintel\.dylib has no arm64 slice'):
            build.keep_architecture(self.resources, 'arm64')

    def test_a_failing_mac_tool_stops_the_build_and_leaves_the_file_whole(self):
        self.put('lib/QtGui', self.both('QtGui'), 0o755)
        before = self.snapshot()
        self.tools.fails = ('QtGui',)
        with self.assertRaises(subprocess.CalledProcessError):
            build.keep_architecture(self.resources, 'arm64')
        self.assertEqual(self.snapshot(), before)  # The file is as it was, and no working folder is left beside it.
        # A universal file that ends inside its table of parts cannot be read by the tool either.
        self.tools.fails = ()
        self.put('lib/QtCut', self.both('QtCut')[:20])
        with self.assertRaises(subprocess.CalledProcessError):
            build.keep_architecture(self.resources, 'arm64')
        self.assertEqual(self.snapshot()['lib/QtGui'], before['lib/QtGui'])

    def test_a_result_that_is_not_for_this_mac_alone_stops_the_build_and_replaces_nothing(self):
        self.put('lib/QtGui', self.both('QtGui'), 0o755)
        before = self.snapshot()
        self.tools.mixes_up = ('QtGui',)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected architecture after thinning QtGui'):
            build.keep_architecture(self.resources, 'arm64')
        self.assertEqual(self.snapshot(), before)


class LoadedFrameworkTests(WorkspaceCase):
    def test_the_frameworks_each_program_loads_are_read_from_the_mac_tools_listing(self):
        core = self.resources / PACKAGE / 'Qt/lib/QtCore.framework/Versions/A/QtCore'
        cocoa = self.resources / PACKAGE / 'Qt/plugins/platforms/libqcocoa.dylib'
        probe = self.resources / 'python/lib/python3.14/site-packages/comic_archive/native/macos-network-probe'
        listing = (f'{core}:\n'
                   '\t@rpath/QtCore.framework/Versions/A/QtCore (compatibility version 6.0.0, current version 6.11.2)\n'
                   '\t/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit (compatibility version 1.0.0, current version 275.0.0)\n'
                   '\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1700.255.5)\n'
                   '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1345.120.2)\n'
                   f'{cocoa}:\n'
                   '\t@rpath/QtGui.framework/Versions/A/QtGui (compatibility version 6.0.0, current version 6.11.2)\n'
                   '\t/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit (compatibility version 45.0.0, current version 2487.70.107)\n'
                   '\t@rpath/QtCore.framework/Versions/A/QtCore (compatibility version 6.0.0, current version 6.11.2)\n'
                   '\t/System/Library/Frameworks/UniformTypeIdentifiers.framework/Versions/A/UniformTypeIdentifiers'
                   ' (compatibility version 1.0.0, current version 709.0.0, weak)\n'
                   '\t@rpath/libpyside6.abi3.6.11.dylib (compatibility version 6.11.0, current version 6.11.2)\n'
                   f'{probe}:\n'
                   '\t/System/Library/Frameworks/Network.framework/Versions/A/Network (compatibility version 1.0.0, current version 1.0.0)\n'
                   '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1345.120.2)\n')
        with patch.object(build, 'run', return_value=finished(listing)):
            loads = build.framework_loads([core, cocoa, probe])
        self.assertEqual(loads, {core: {'QtCore', 'IOKit'}, cocoa: {'QtGui', 'AppKit', 'QtCore', 'UniformTypeIdentifiers'},
                                 probe: {'Network'}})

    def test_every_program_is_answered_for_however_many_there_are(self):
        binaries = [self.resources / f'lib/Qt{number}.framework/Versions/A/Qt{number}' for number in range(70)]
        for number, path in enumerate(binaries):
            self.tools.loads[path] = [f'@rpath/Qt{number}.framework/Versions/A/Qt{number}', '/usr/lib/libSystem.B.dylib']
        self.assertEqual(build.framework_loads(binaries), {path: {f'Qt{number}'} for number, path in enumerate(binaries)})
        self.assertEqual(build.framework_loads([]), {})

    def test_a_program_the_mac_tool_does_not_answer_for_stops_the_build(self):
        # What it loads would count as nothing, and a framework it needs could then be taken out of the app.
        known, unknown = self.resources / 'lib/libknown.dylib', self.resources / 'lib/libunknown.dylib'
        self.tools.loads[known] = ['/usr/lib/libSystem.B.dylib']
        with self.assertRaisesRegex(RuntimeError, 'Could not determine the linked Qt frameworks'):
            build.framework_loads([known, unknown])
        # A universal file is listed once for each processor, under another heading; none may reach this step.
        listing = (f'{unknown} (architecture arm64):\n'
                   '\t@rpath/QtCore.framework/Versions/A/QtCore (compatibility version 6.0.0, current version 6.11.2)\n')
        with patch.object(build, 'run', return_value=finished(listing)), \
                self.assertRaisesRegex(RuntimeError, 'Could not determine the linked Qt frameworks'):
            build.framework_loads([unknown])


class PruningTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.qt = self.resources / PACKAGE
        self.needs = {}

    def program(self, name, *frameworks):
        """A program file in Qt's package that loads these frameworks, which otool lists for it."""
        path = self.put(PACKAGE + name, thin('arm64', name.encode()), 0o755)
        self.needs[path] = frameworks
        self.tools.loads[path] = [*(f'@rpath/{framework}.framework/Versions/A/{framework}' for framework in frameworks),
                                  '/usr/lib/libSystem.B.dylib']
        return path

    def framework(self, name, *frameworks, module=True):
        """A Qt framework, listed first among what it loads itself, and the Python module made for it."""
        self.program(f'Qt/lib/{name}.framework/Versions/A/{name}', name, *frameworks)
        self.put(PACKAGE + f'Qt/lib/{name}.framework/Versions/A/Resources/Info.plist', b'<plist/>')
        if module:
            self.program(name + '.abi3.so', name, 'QtCore')

    def package(self):
        """A small Qt package shaped like the one for the Mac: what the app uses, and the four families it does not."""
        shutil.rmtree(self.resources, ignore_errors=True)
        self.tools.loads.clear()
        self.needs.clear()
        self.put(PACKAGE + '__init__.py', b'import sys\n')
        self.program('libpyside6.abi3.6.11.dylib', 'QtCore')
        for name, loads in (('QtCore', ()), ('QtGui', ('QtCore',)), ('QtNetwork', ('QtCore',)), ('QtWidgets', ('QtGui', 'QtCore')),
                            ('QtOpenGL', ('QtGui',)), ('QtQml', ('QtNetwork', 'QtCore')), ('QtQuick', ('QtQml', 'QtGui')),
                            ('QtWebEngineCore', ('QtQuick', 'QtNetwork')), ('QtWebEngineWidgets', ('QtWebEngineCore', 'QtWidgets')),
                            ('Qt3DCore', ('QtGui',)), ('Qt3DRender', ('Qt3DCore', 'QtOpenGL')), ('QtQuick3D', ('QtQuick3DRuntimeRender', 'QtQuick')),
                            ('QtDesigner', ('QtWidgets',)), ('QtGraphs', ('QtQuick3D', 'QtQuick')), ('QtGraphsWidgets', ('QtGraphs', 'QtWidgets'))):
            self.framework(name, *loads)
        for name, loads in (('QtQuick3DUtils', ('QtGui',)), ('QtQuick3DRuntimeRender', ('QtQuick3DUtils', 'QtQuick')),
                            ('QtDesignerComponents', ('QtDesigner',))):
            self.framework(name, *loads, module=False)
        # The pages are drawn by a program of its own, started from inside the framework, with files beside it.
        engine = 'Qt/lib/QtWebEngineCore.framework/Versions/A/'
        self.program(engine + 'Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess', 'QtWebEngineCore', 'QtCore')
        for name in ('Resources/qtwebengine_resources.pak', 'Resources/icudtl.dat', 'Resources/qtwebengine_locales/en-US.pak'):
            self.put(PACKAGE + engine + name, b'x')
        for name, loads in (('platforms/libqcocoa.dylib', ('QtGui', 'QtCore')), ('styles/libqmacstyle.dylib', ('QtWidgets',)),
                            ('imageformats/libqjpeg.dylib', ('QtGui',)), ('imageformats/libqwebp.dylib', ('QtGui',)),
                            ('imageformats/libqgif.dylib', ('QtGui',)), ('tls/libqsecuretransportbackend.dylib', ('QtNetwork',)),
                            ('designer/libqquickwidget.dylib', ('QtDesigner', 'QtWidgets')), ('renderers/libopenglrenderer.dylib', ('Qt3DRender',)),
                            ('sceneparsers/libgltfsceneimport.dylib', ('Qt3DRender',)), ('renderplugins/libscene2d.dylib', ('Qt3DRender',)),
                            ('geometryloaders/libdefaultgeometryloader.dylib', ('Qt3DRender',)), ('assetimporters/libassimp.dylib', ('QtQuick3D',)),
                            ('qmltooling/libqmldbg_debugger.dylib', ('QtQml',)), ('qmllint/libquicklintplugin.dylib', ('QtQml',))):
            self.program('Qt/plugins/' + name, *loads)

    def prune(self):
        """The two steps as the build runs them: find every program file of the app, then what Qt can lose."""
        binaries, _ = build.keep_architecture(self.resources, 'arm64')
        return build.prune_qt(self.qt, binaries)

    def left(self):
        return {path.relative_to(self.qt).as_posix() for path in self.qt.rglob('*') if path.is_file()}

    def after_losing(self, before, families):
        """The package's files once these frameworks, their Python modules and the unused kinds of plugin are gone."""
        gone = (*(f'Qt/lib/{name}.framework/' for name in families), *(name + '.abi3.so' for name in families),
                *(f'Qt/plugins/{kind}/' for kind in UNUSED_PLUGINS))
        return {name for name in before if not name.startswith(gone)}

    def assert_every_framework_a_program_loads_is_still_there(self):
        # An app in which this does not hold starts on no Mac, or stops when the missing framework is asked for.
        for path, frameworks in self.needs.items():
            for name in frameworks if path.exists() else ():
                self.assertTrue((self.qt / f'Qt/lib/{name}.framework/Versions/A/{name}').is_file(), f'{path.name} loads {name}')

    def test_the_families_nothing_kept_loads_go_with_their_python_modules_and_the_unused_plugins(self):
        self.package()
        before = self.left()
        self.assertEqual(self.prune(), sorted(FAMILIES))
        self.assertEqual(self.left(), self.after_losing(before, FAMILIES))
        for folder in (*(f'Qt/lib/{name}.framework' for name in FAMILIES), *(f'Qt/plugins/{kind}' for kind in UNUSED_PLUGINS)):
            self.assertFalse((self.qt / folder).exists(), folder)
        self.assert_every_framework_a_program_loads_is_still_there()

    def test_a_family_a_kept_program_loads_stays_with_everything_it_loads_in_turn(self):
        self.package()
        self.program('Qt/plugins/styles/libqchartstyle.dylib', 'QtGraphsWidgets')
        before = self.left()
        gone = ('Qt3DCore', 'Qt3DRender', 'QtDesigner', 'QtDesignerComponents')
        # Kept one after the other: QtGraphsWidgets, then QtGraphs, QtQuick3D, QtQuick3DRuntimeRender, QtQuick3DUtils.
        self.assertEqual(self.prune(), sorted(gone))
        self.assertEqual(self.left(), self.after_losing(before, gone))
        self.assertLessEqual({'QtGraphs.abi3.so', 'QtGraphsWidgets.abi3.so', 'QtQuick3D.abi3.so',
                              'Qt/lib/QtQuick3DUtils.framework/Versions/A/QtQuick3DUtils'}, self.left())
        self.assert_every_framework_a_program_loads_is_still_there()

    def test_each_of_the_four_families_stays_when_any_kind_of_kept_program_loads_it(self):
        # A plugin of a kept kind, a program beside the modules, a Python module, and another framework.
        cases = (('Qt/plugins/imageformats/libqmodel.dylib', ('Qt3DRender',), {'Qt3DRender', 'Qt3DCore'}),
                 ('Designer.app/Contents/MacOS/Designer', ('QtDesignerComponents',), {'QtDesignerComponents', 'QtDesigner'}),
                 ('QtDataVisualization.abi3.so', ('QtQuick3DRuntimeRender',), {'QtQuick3DRuntimeRender', 'QtQuick3DUtils'}),
                 ('Qt/lib/QtCharts.framework/Versions/A/QtCharts', ('QtCharts', 'QtGraphs'),
                  {'QtGraphs', 'QtQuick3D', 'QtQuick3DRuntimeRender', 'QtQuick3DUtils'}))
        for loader, loads, stays in cases:
            with self.subTest(loader):
                self.package()
                self.program(loader, *loads)
                before = self.left()
                self.assertEqual(self.prune(), sorted(set(FAMILIES) - stays))
                self.assertEqual(self.left(), self.after_losing(before, set(FAMILIES) - stays))
                self.assert_every_framework_a_program_loads_is_still_there()

    def test_what_the_window_and_the_pages_need_to_run_is_never_removed(self):
        self.package()
        self.prune()
        left, engine = self.left(), 'Qt/lib/QtWebEngineCore.framework/Versions/A/'
        for name in ('Qt/plugins/platforms/libqcocoa.dylib', 'Qt/plugins/styles/libqmacstyle.dylib',
                     'Qt/plugins/imageformats/libqjpeg.dylib', 'Qt/plugins/imageformats/libqwebp.dylib',
                     'Qt/plugins/imageformats/libqgif.dylib', 'Qt/plugins/tls/libqsecuretransportbackend.dylib',
                     engine + 'QtWebEngineCore', engine + 'Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess',
                     engine + 'Resources/qtwebengine_resources.pak', engine + 'Resources/icudtl.dat',
                     engine + 'Resources/qtwebengine_locales/en-US.pak'):
            self.assertIn(name, left)
        # Nothing the app imports or asks Qt for can come to match a name that is removed.
        self.assertFalse([module for module in build.shared.QT_MODULES if module.startswith(build.OPTIONAL_QT)])
        self.assertFalse({'platforms', 'styles', *build.shared.QT_PLUGINS} & set(build.OPTIONAL_PLUGINS))


if __name__ == '__main__':
    unittest.main()
