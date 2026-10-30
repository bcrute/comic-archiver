import contextlib
import io
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from comic_archive import desktop_app
from scripts import build_windows_app as build

PROJECT = Path(__file__).resolve().parents[1]
PACKAGING = PROJECT / 'packaging/windows'


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()


def signature_image():
    """PE metadata fixture: a certificate table, without claiming a cryptographically valid signer."""
    data = bytearray(0x210)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', data, 0x98, 0x20b)
    struct.pack_into('<II', data, 0x98 + 112 + 8 * 4, 0x200, 16)
    struct.pack_into('<IHH', data, 0x200, 16, 0x200, 2)
    return bytes(data)


class PinnedDownloadTests(WorkspaceCase):
    def test_the_python_for_windows_is_the_version_that_compiles_its_modules_on_linux(self):
        # Compiled modules are only read by the Python version that wrote them.
        version = re.match(r'cpython-([0-9.]+)\+', build.common.PBS_NAME).group(1)
        self.assertEqual(build.PYTHON_RELEASE, version)
        self.assertTrue(build.PYTHON_RELEASE.startswith(build.PYTHON_VERSION + '.'))
        self.assertEqual(build.PYTHON_NAME, f'python-{version}-embed-amd64.zip')
        self.assertEqual(build.PYTHON_URL, f'https://www.python.org/ftp/python/{version}/{build.PYTHON_NAME}')
        self.assertEqual(len(build.PYTHON_SHA256), 64); int(build.PYTHON_SHA256, 16)

    def test_a_python_archive_without_the_windows_programs_is_refused(self):
        def unpacked(*names):
            def unpack(archive, destination):
                for name in names:
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(signature_image() if path.suffix in ('.exe', '.dll', '.pyd') else b'x')
            return unpack
        app = self.root / 'app'
        with patch.object(build.common, 'fetch', return_value=self.root / 'archive') as fetch:
            with patch.object(build, 'unpack_windows_python', unpacked('python.exe', 'pythonw.exe', 'python3.dll',
                              build.PYTHON_STEM + '.dll', build.PYTHON_STEM + '.zip')):
                build.bundle_python(app)
            self.assertEqual(fetch.call_args.args[2], build.PYTHON_SHA256)
            self.assertEqual((app / 'python' / (build.PYTHON_STEM + '._pth')).read_text(),
                             build.PYTHON_STEM + '.zip\n.\nLib/site-packages\nimport site\n')
            with patch.object(build, 'unpack_windows_python', unpacked('bin/python3', 'Lib/os.py')):
                with self.assertRaisesRegex(RuntimeError, 'lacks python.exe'): build.bundle_python(self.root / 'other')

    def test_unsigned_malformed_or_out_of_bounds_certificate_data_is_refused(self):
        path = self.root / 'python.exe'
        path.write_bytes(signature_image())
        self.assertTrue(build.carries_signature(path))
        unsigned = bytearray(signature_image()); struct.pack_into('<II', unsigned, 0x98 + 112 + 8 * 4, 0, 0)
        outside = bytearray(signature_image()); struct.pack_into('<II', outside, 0x98 + 112 + 8 * 4, 0x200, 5000)
        wrong_kind = bytearray(signature_image()); struct.pack_into('<H', wrong_kind, 0x206, 3)
        for data in (b'not a PE file', signature_image()[:100], unsigned, outside, wrong_kind):
            with self.subTest(data=bytes(data[:8])):
                path.write_bytes(data)
                self.assertFalse(build.carries_signature(path))

    def test_the_bundle_rejects_an_unsigned_interpreter_before_writing_its_search_path(self):
        archive = self.root / 'vendor.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name in ('python.exe', 'pythonw.exe', 'python3.dll', build.PYTHON_STEM + '.dll'):
                package.writestr(name, b'unsigned' if name == 'python.exe' else signature_image())
            package.writestr(build.PYTHON_STEM + '.zip', b'library')
        app = self.root / 'app'
        with patch.object(build.common, 'fetch', return_value=archive):
            with self.assertRaisesRegex(RuntimeError, 'lacks an embedded signature: python.exe'):
                build.bundle_python(app)
        self.assertFalse((app / 'python' / (build.PYTHON_STEM + '._pth')).exists())

    def test_vendor_zip_refuses_nested_paths_drives_and_symlinks_before_writing(self):
        archive = self.root / 'vendor.zip'
        for name in ('../outside', '/outside', 'C:outside', 'nested/file', 'nested\\file', '.'):
            with self.subTest(name=name):
                with zipfile.ZipFile(archive, 'w') as package:
                    package.writestr('python.exe', b'first file')
                    package.writestr(name, b'bad path')
                with self.assertRaisesRegex(RuntimeError, 'archive structure'):
                    build.unpack_windows_python(archive, self.root / 'refused')
                self.assertFalse((self.root / 'refused').exists())
        with zipfile.ZipFile(archive, 'w') as package:
            item = zipfile.ZipInfo('python.exe'); item.external_attr = (0o120777 << 16)
            package.writestr(item, 'outside')
        with self.assertRaisesRegex(RuntimeError, 'archive structure'):
            build.unpack_windows_python(archive, self.root / 'refused')
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('python.exe', b'validated vendor file')
        build.unpack_windows_python(archive, self.root / 'accepted')
        self.assertEqual((self.root / 'accepted/python.exe').read_bytes(), b'validated vendor file')


class PackagingTests(WorkspaceCase):
    def test_the_shortcuts_the_start_file_and_the_smoke_start_the_app_the_same_way(self):
        parameters = ' '.join(build.START)
        self.assertEqual(parameters, '-I -X utf8 -m comic_archive.desktop_app')
        start = (PACKAGING / 'Comic Archiver.cmd').read_bytes()
        self.assertIn(b'"%~dp0python\\pythonw.exe" ' + parameters.encode() + b' %*', start)
        self.assertNotIn(b'\n', start.replace(b'\r\n', b''))  # Windows line endings throughout.
        installer = (PACKAGING / 'comic-archiver.iss').read_text('utf-8')
        # Every shortcut and the start after installing take the parameters the build gives, and none of their own.
        starts = [line for line in installer.splitlines() if line.startswith(('Name: "{auto', 'Filename:'))]
        self.assertEqual(len(starts), 3)
        for line in starts:
            self.assertIn('Filename: "{#AppProgram}"; Parameters: "{#StartParameters}"; WorkingDir: "{app}"', line)
        self.assertIn('#define AppProgram "{app}\\python\\pythonw.exe"', installer)

    def test_the_installer_needs_no_administrator_and_names_the_window_as_the_app_does(self):
        installer = (PACKAGING / 'comic-archiver.iss').read_text('utf-8')
        self.assertIn('\nPrivilegesRequired=lowest\n', installer.replace('\r\n', '\n'))
        self.assertIn(f'#define AppModelId "{desktop_app.WINDOWS_APP_ID}"', installer)
        self.assertEqual(installer.count('AppUserModelID: "{#AppModelId}"'), 2)

    def test_uninstalling_has_the_app_forget_every_sign_in_before_any_file_goes_and_never_while_it_is_open(self):
        installer = (PACKAGING / 'comic-archiver.iss').read_text('utf-8').replace('\r\n', '\n')
        self.assertIn('\n#define UninstallParameters StartParameters + " --uninstall --quiet"\n', installer)
        options = desktop_app.arguments(['--uninstall', '--quiet'])
        self.assertTrue(options.uninstall and options.quiet)
        code = installer.split('\n[Code]\n', 1)[1]
        self.assertIn('function InitializeUninstall(): Boolean;', code)
        self.assertIn("Exec(ExpandConstant('{#AppProgram}'), '{#UninstallParameters}', ExpandConstant('{app}'), SW_HIDE,", code)
        self.assertIn('ewWaitUntilTerminated, Code)', code)
        self.assertIn(f'Result := Code = {desktop_app.UNINSTALLED};', code)
        self.assertIn(f'if Code = {desktop_app.UNINSTALL_BLOCKED} then', code)
        self.assertIn(f"SuppressibleMsgBox('{desktop_app.UNINSTALL_OPEN}', mbError, MB_OK, IDOK)", code)
        # Silently, as when a newer version's installer removes this one, a failure stops the uninstall.
        self.assertIn('MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;', code)

    def test_installing_over_an_open_app_has_it_close_first_or_asks_the_person_to(self):
        installer = (PACKAGING / 'comic-archiver.iss').read_text('utf-8').replace('\r\n', '\n')
        self.assertIn('\n#define CloseParameters StartParameters + " --close-for-update"\n', installer)
        self.assertTrue(desktop_app.arguments(['--close-for-update']).close_for_update)
        code = installer.split('\n[Code]\n', 1)[1]
        self.assertIn('function PrepareToInstall(var NeedsRestart: Boolean): String;', code)
        self.assertIn("Exec(ExpandConstant('{#AppProgram}'), '{#CloseParameters}', ExpandConstant('{app}'), SW_HIDE,", code)
        # Whatever that answered, nothing is replaced while a program in the app's folder runs.
        self.assertIn("Folder := Lowercase(AddBackslash(ExpandConstant('{app}')));", code)
        self.assertIn('  while AppOpen() do\n', code)
        # Silently, a setup over an open app stops instead of failing on files in use.
        self.assertIn("SuppressibleMsgBox('Comic Archiver is open. Close it, then click Retry.', mbError, MB_RETRYCANCEL,\n"
                      "                          IDCANCEL) = IDCANCEL then", code)

    def test_the_icon_holds_every_size_windows_asks_for(self):
        data = (PACKAGING / 'comic-archiver.ico').read_bytes()
        self.assertEqual(data[:4], b'\0\0\1\0')
        count = int.from_bytes(data[4:6], 'little')
        self.assertEqual(sorted(data[6 + 16 * index] or 256 for index in range(count)), [16, 20, 24, 32, 40, 48, 64, 128, 256])

    def test_install_packaging_lays_out_what_the_installer_and_the_app_look_for(self):
        app = self.root / 'ComicArchiver-1.2.3'; app.mkdir()
        build.install_packaging(app, '1.2.3')
        for name in ('comic-archiver.ico', 'Comic Archiver.cmd', 'README_FIRST.txt', 'LICENSE', 'THIRD-PARTY-NOTICES.md'):
            self.assertTrue((app / name).is_file(), name)
        self.assertEqual((app / 'VERSION').read_text(), '1.2.3\n')

    def test_the_installed_packages_are_listed_from_what_was_installed(self):
        app = self.root / 'app'
        for folder, text in (('PySide6-6.11.2.dist-info', 'Metadata-Version: 2.4\nName: PySide6\nVersion: 6.11.2\nSummary: Qt: for Python\n\nName: not-this\n'),
                             ('comic_archive-1.2.3.dist-info', 'Name: comic-archive\nVersion: 1.2.3\n')):
            (build.site_packages(app) / folder).mkdir(parents=True); (build.site_packages(app) / folder / 'METADATA').write_text(text)
        self.assertEqual(build.installed(app), 'PySide6==6.11.2\ncomic-archive==1.2.3\n')
        self.assertEqual(build.common.frozen_requirements(build.installed(app), '1.2.3'), 'comic-archive==1.2.3\nPySide6==6.11.2\n')

    def test_licenses_are_collected_from_the_windows_layout_and_the_qt_lgpl_is_demanded(self):
        app = self.root / 'app'; packages = build.site_packages(app)
        for path, text in ((app / 'python/LICENSE.txt', 'PSF'), (packages / 'PySide6/LICENSE.LGPLv3', 'GNU LESSER GENERAL PUBLIC LICENSE'),
                           (packages / 'httpx-1.dist-info/licenses/LICENSE.md', 'BSD'), (packages / 'httpx-1.dist-info/METADATA', 'no')):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        found = sorted(p.relative_to(app / 'licenses').as_posix() for p in build.collect_licenses(app))
        self.assertEqual(found, ['PySide6/LICENSE.LGPLv3', 'httpx-1.dist-info/licenses/LICENSE.md', 'python/LICENSE.txt'])
        (packages / 'PySide6/LICENSE.LGPLv3').write_text('something else')
        with self.assertRaisesRegex(RuntimeError, 'LGPL'): build.collect_licenses(app)


def program_file(path, loads=(), on_first_use=()):
    """Write a Windows program file that holds nothing but its two import tables."""
    start, body = 0x1000, bytearray()  # Where its one section lies in memory.

    def table(names, entry_size, name_at):
        if not names:
            return 0
        strings = []
        for name in names:
            strings.append(start + len(body)); body.extend(name.encode() + b'\0')
        address = start + len(body)
        for string in strings:
            entry = bytearray(entry_size); struct.pack_into('<I', entry, name_at, string); body.extend(entry)
        body.extend(bytes(entry_size))
        return address

    optional = bytearray(240); struct.pack_into('<H', optional, 0, 0x20b)
    struct.pack_into('<I', optional, 112 + 8 * 1, table(loads, 20, 12))
    struct.pack_into('<I', optional, 112 + 8 * 13, table(on_first_use, 32, 4))
    section = b'.rdata\0\0' + struct.pack('<4I', len(body), start, len(body), 0x200) + bytes(16)
    head = b'MZ' + bytes(58) + struct.pack('<I', 64) + b'PE\0\0' + struct.pack('<HH12xHH', 0x8664, 1, len(optional), 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((head + optional + section).ljust(0x200, b'\0') + body)


class LeftOutTests(WorkspaceCase):
    def test_the_qt_modules_kept_in_the_package_are_the_ones_the_app_imports(self):
        # A module the app starts to import and the package lacks would only show on Windows, as an app that does not start.
        imported = set()
        for source in (PROJECT / 'comic_archive').rglob('*.py'):
            text = source.read_text('utf-8')
            for names in re.findall(r'^\s*from PySide6 import ([\w, ]+)', text, re.M):
                imported.update(name.strip() for name in names.split(','))
            imported.update(re.findall(r'^\s*(?:from|import) PySide6\.(\w+)', text, re.M))
        self.assertEqual(imported, set(build.QT_MODULES))

    def test_what_a_program_file_loads_is_read_from_both_of_its_tables(self):
        program_file(self.root / 'a.dll', loads=('Qt6Core.dll', 'KERNEL32.dll'), on_first_use=('Qt6Gui.dll',))
        program_file(self.root / 'none.dll')
        self.assertEqual(build.imported_names(self.root / 'a.dll'), {'qt6core.dll', 'kernel32.dll', 'qt6gui.dll'})
        self.assertEqual(build.imported_names(self.root / 'none.dll'), set())
        (self.root / 'text.dll').write_bytes(b'not a program at all' * 40)
        with self.assertRaisesRegex(RuntimeError, 'text.dll'): build.imported_names(self.root / 'text.dll')

    def qt(self, **changed):
        """What each program file of a small Qt package loads, shaped like the real one."""
        loads = {module + '.pyd': {'qt6' + module[2:].lower() + '.dll', 'python3.dll', 'pyside6.abi3.dll'} for module in build.QT_MODULES}
        loads.update({name: set() for name in ('Qt6Core.dll', 'pyside6.abi3.dll', 'opengl32sw.dll', 'Qt6Svg.dll', 'designer.exe')})
        loads.update({'Qt6Gui.dll': {'qt6core.dll'}, 'Qt6Network.dll': {'qt6core.dll'}, 'Qt6Widgets.dll': {'qt6gui.dll'},
                      'Qt6Quick.dll': {'qt6gui.dll', 'qt6qml.dll'}, 'Qt6Qml.dll': {'qt6network.dll'}, 'QtQml.pyd': {'qt6qml.dll'},
                      'Qt6WebEngineCore.dll': {'qt6quick.dll', 'user32.dll'}, 'QtWebEngineProcess.exe': {'qt6webenginecore.dll'},
                      'Qt6WebEngineWidgets.dll': {'qt6webenginecore.dll', 'qt6printsupport.dll'},
                      'Qt6PrintSupport.dll': {'qt6widgets.dll'}, 'QtPrintSupport.pyd': {'qt6printsupport.dll'},
                      'Qt6Charts.dll': {'qt6widgets.dll'}, 'QtCharts.pyd': {'qt6charts.dll'}, 'QtSvg.pyd': {'qt6svg.dll'},
                      'plugins/platforms/qwindows.dll': {'qt6gui.dll'}, 'plugins/imageformats/qico.dll': {'qt6gui.dll'},
                      'plugins/imageformats/qsvg.dll': {'qt6gui.dll', 'qt6svg.dll'}, 'plugins/sqldrivers/qsqlite.dll': {'qt6core.dll'}})
        loads.update(changed)
        return {name: names for name, names in loads.items() if names is not None}

    def test_qt_keeps_the_apps_modules_what_they_load_and_the_plugins_that_need_nothing_more(self):
        kept = build.qt_needed(self.qt())
        left_out = {'Qt6Charts.dll', 'QtCharts.pyd', 'Qt6Svg.dll', 'QtSvg.pyd', 'designer.exe',
                    'plugins/imageformats/qsvg.dll', 'plugins/sqldrivers/qsqlite.dll'}
        self.assertEqual(kept, self.qt().keys() - left_out)
        # No table shows that QtWebEngineWidgets imports QtPrintSupport, nor QtQuick QtQml: Python does that as they start.
        self.assertLessEqual({'QtPrintSupport.pyd', 'QtQml.pyd'}, kept)
        for lacking in ('QtWebEngineWidgets.pyd', 'QtWebEngineProcess.exe'):
            with self.assertRaisesRegex(RuntimeError, lacking): build.qt_needed(self.qt(**{lacking: None}))

    def test_the_app_folder_loses_what_the_app_never_loads_and_keeps_the_rest(self):
        app = self.root / 'app'; python = app / 'python'; qt = build.site_packages(app) / 'PySide6'
        for name, loads in self.qt().items():
            program_file(qt / name, loads)
        for name, loads in (('python.exe', ('python314.dll',)), ('python314.dll', ()), ('DLLs/_ssl.pyd', ('libssl-3-x64.dll',)),
                            ('DLLs/libssl-3-x64.dll', ()), ('DLLs/_tkinter.pyd', ('tcl90.dll',)), ('DLLs/tcl90.dll', ('zlib1.dll',)),
                            ('DLLs/zlib1.dll', ()), ('DLLs/_testcapi.pyd', ()), ('Lib/site-packages/pip/_vendor/distlib/t64.exe', ()),
                            ('Lib/site-packages/PIL/_imaging.cp314-win_amd64.pyd', ()), ('Lib/site-packages/PIL/_imagingtk.cp314-win_amd64.pyd', ())):
            program_file(python / name, loads)
        other = ('python/Lib/os.py', 'python/Lib/turtle.py', 'python/Lib/tkinter/__init__.py', 'python/Lib/venv/__init__.py',
                 'python/Lib/site-packages/pip-26.dist-info/METADATA', 'python/Lib/site-packages/comic_archive/cli.py',
                 'python/include/Python.h', 'python/tcl/init.tcl')
        qt_other = ('__init__.py', 'QtCore.pyi', 'LICENSE.LGPLv3', 'qml/QtQuick/qmldir', 'metatypes/qt6core.json', 'translations/qt_de.qm',
                    'translations/qtwebengine_locales/en-US.pak', 'resources/qtwebengine_resources.pak', 'resources/icudtl.dat',
                    'resources/qtwebengine_resources.debug.pak', 'resources/qtwebengine_devtools_resources.pak')
        for path in [*(app / name for name in other), *(qt / name for name in qt_other)]:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text('x')
        with contextlib.redirect_stdout(io.StringIO()) as said:
            build.leave_out_unused(app)
        self.assertRegex(said.getvalue(), r'^Left out what the app never loads: \d+ of \d+ MB\n$')
        left = {path.relative_to(app / 'python').as_posix() for path in python.rglob('*') if path.is_file()}
        packages = 'Lib/site-packages/'
        self.assertEqual({name for name in left if not name.startswith(packages + 'PySide6/')},
                         {'python.exe', 'python314.dll', 'DLLs/_ssl.pyd', 'DLLs/libssl-3-x64.dll', 'Lib/os.py',
                          packages + 'PIL/_imaging.cp314-win_amd64.pyd', packages + 'comic_archive/cli.py'})
        self.assertEqual({name[len(packages + 'PySide6/'):] for name in left if name.startswith(packages + 'PySide6/')},
                         build.qt_needed(self.qt()) | {'__init__.py', 'LICENSE.LGPLv3', 'translations/qtwebengine_locales/en-US.pak',
                                                       'resources/qtwebengine_resources.pak', 'resources/icudtl.dat'})
        self.assertFalse((qt / 'plugins/sqldrivers').exists())

    def test_a_kept_file_that_loads_one_that_was_left_out_stops_the_build(self):
        # Such an app starts on no computer; the build host must say so, not the first person to install it.
        app = self.root / 'app'
        for name, loads in self.qt().items():
            program_file(build.site_packages(app) / 'PySide6' / name, loads)
        program_file(app / 'python/DLLs/zlib1.dll')
        program_file(app / 'python/DLLs/_zlib.pyd', ('ZLIB1.dll',))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, r'_zlib\.pyd loads zlib1\.dll'):
            build.leave_out_unused(app)


class ArchiveTests(WorkspaceCase):
    def test_the_zip_has_one_top_folder_and_matches_its_checksum(self):
        app = self.root / 'ComicArchiver-1.2.3'
        (app / 'python/Lib').mkdir(parents=True); (app / 'python/python.exe').write_bytes(b'MZ'); (app / 'VERSION').write_text('1.2.3\n')
        archive, checksum = build.make_archive(app, self.root / 'dist', '1.2.3')
        self.assertEqual(archive.name, 'ComicArchiver-1.2.3-windows-x86_64.zip')
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(sorted(package.namelist()), ['ComicArchiver-1.2.3/VERSION', 'ComicArchiver-1.2.3/python/python.exe'])
        self.assertEqual(checksum.read_text(), f'{build.common.file_sha256(archive)}  {archive.name}\n')
        self.assertEqual([p.name for p in (self.root / 'dist').iterdir() if p.name.endswith('.partial')], [])

    def test_the_zip_bytes_do_not_depend_on_file_times(self):
        app = self.root / 'ComicArchiver-1.2.3'
        (app / 'python').mkdir(parents=True); (app / 'python/python.exe').write_bytes(b'MZ' * 4096); (app / 'VERSION').write_text('1.2.3\n')
        first, _ = build.make_archive(app, self.root / 'one', '1.2.3')
        os.utime(app / 'VERSION', (1_000_000_000, 1_000_000_000))
        second, _ = build.make_archive(app, self.root / 'two', '1.2.3')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(second) as package:
            self.assertEqual({(info.date_time, info.compress_type) for info in package.infolist()},
                             {(build.ZIP_DATE, zipfile.ZIP_DEFLATED)})
            self.assertEqual(package.read('ComicArchiver-1.2.3/python/python.exe'), b'MZ' * 4096)


class MainTests(WorkspaceCase):
    def run_main(self, *arguments):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            return build.main(list(arguments)), errors.getvalue()

    def test_starting_the_app_and_building_the_installer_are_refused_off_windows_before_anything_is_built(self):
        with patch.object(build, 'on_windows', return_value=False), patch.object(build, 'build') as built:
            for flag in ('--smoke', '--installer'):
                status, said = self.run_main(flag)
                self.assertEqual(status, 1); self.assertIn('need Windows', said)
            built.assert_not_called()

    def test_a_missing_inno_setup_is_reported_before_anything_is_built(self):
        with patch.object(build, 'on_windows', return_value=True), patch.object(build, 'build') as built, \
                patch.object(build.shutil, 'which', return_value=None), patch.dict(build.os.environ, {'LOCALAPPDATA': str(self.root)}):
            status, said = self.run_main('--installer', '--iscc', str(self.root / 'absent.exe'))
            self.assertEqual(status, 1); self.assertIn('Inno Setup', said)
            built.assert_not_called()

    def test_inno_setup_is_found_where_it_was_given_or_where_it_installs_itself(self):
        mine = self.root / 'Programs/Inno Setup 6/ISCC.exe'; mine.parent.mkdir(parents=True); mine.write_bytes(b'MZ')
        given = self.root / 'elsewhere.exe'; given.write_bytes(b'MZ')
        with patch.object(build.shutil, 'which', return_value=None):
            self.assertEqual(build.find_iscc(None, {'LOCALAPPDATA': str(self.root)}), mine)
            self.assertEqual(build.find_iscc(str(given), {'LOCALAPPDATA': str(self.root)}), given)
            newer = self.root / 'Programs/Inno Setup 7/ISCC.exe'; newer.parent.mkdir(parents=True); newer.write_bytes(b'MZ')
            self.assertEqual(build.find_iscc(None, {'LOCALAPPDATA': str(self.root)}), newer)
            with self.assertRaisesRegex(RuntimeError, 'not found'): build.find_iscc(None, {})


if __name__ == '__main__':
    unittest.main()
