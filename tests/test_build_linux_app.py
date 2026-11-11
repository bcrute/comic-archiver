import configparser
import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ElementTree

from support import linux_only
linux_only('The Linux package is built on Linux.')
from scripts import build_linux_app as build

PROJECT = Path(__file__).resolve().parents[1]


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(work.name).resolve()


class PinnedDownloadTests(WorkspaceCase):
    def test_pinned_values_describe_the_expected_standalone_python(self):
        self.assertEqual(len(build.PBS_SHA256), 64)
        int(build.PBS_SHA256, 16)
        self.assertTrue(build.PBS_URL.startswith('https://github.com/astral-sh/python-build-standalone/releases/download/'))
        self.assertIn('/' + build.PBS_TAG + '/', build.PBS_URL)
        self.assertTrue(build.PBS_URL.endswith(build.PBS_NAME.replace('+', '%2B')))
        self.assertIn('x86_64-unknown-linux-gnu', build.PBS_NAME)
        self.assertIn('3.14', build.PBS_NAME)

    def test_fetch_keeps_only_bytes_that_match_and_reuses_a_verified_copy(self):
        source = self.root / 'source.bin'
        source.write_bytes(b'verified bytes')
        digest = hashlib.sha256(b'verified bytes').hexdigest()
        target = self.root / 'downloads/copy.bin'
        self.assertEqual(build.fetch(source.as_uri(), target, digest.upper()), target)
        self.assertEqual(target.read_bytes(), b'verified bytes')
        self.assertFalse(list(target.parent.glob('*.partial')))
        source.unlink()
        build.fetch(source.as_uri(), target, digest)
        self.assertEqual(target.read_bytes(), b'verified bytes')

    def test_fetch_rejects_wrong_digest_malformed_pin_and_replaces_a_bad_cached_file(self):
        source = self.root / 'source.bin'
        source.write_bytes(b'tampered')
        target = self.root / 'downloads/copy.bin'
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            build.fetch(source.as_uri(), target, '0' * 64)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob('*')))
        for bad in ('', 'abc', 'z' * 64):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                build.fetch(source.as_uri(), target, bad)
        target.write_bytes(b'stale')
        build.fetch(source.as_uri(), target, hashlib.sha256(b'tampered').hexdigest())
        self.assertEqual(target.read_bytes(), b'tampered')

    def test_bundle_python_unpacks_the_verified_archive_and_rejects_other_layouts(self):
        for member, good in (('python/bin/python3', True), ('other/readme', False)):
            with self.subTest(member=member):
                source = self.root / 'tree'
                shutil.rmtree(source, ignore_errors=True)
                (source / member).parent.mkdir(parents=True)
                (source / member).write_text('x')
                archive = self.root / 'python.tar.gz'
                with tarfile.open(archive, 'w:gz') as package:
                    package.add(source / member.split('/')[0], arcname=member.split('/')[0])
                app = self.root / 'build/app'
                shutil.rmtree(app.parent, ignore_errors=True)
                app.mkdir(parents=True)
                with patch.object(build, 'fetch', return_value=archive) as fetch:
                    if good:
                        self.assertEqual(build.bundle_python(app), app / 'python/bin/python3')
                        self.assertTrue((app / 'python/bin/python3').is_file())
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'structure'):
                            build.bundle_python(app)
                self.assertEqual(fetch.call_args.args[2], build.PBS_SHA256)
                self.assertEqual([p.name for p in app.parent.iterdir()], ['app'])

    def test_platform_guard_accepts_only_linux_x86_64(self):
        build.require_linux_x86_64('Linux', 'x86_64')
        for system, machine in (('Windows', 'AMD64'), ('Darwin', 'x86_64'), ('Linux', 'aarch64')):
            with self.assertRaisesRegex(RuntimeError, 'Linux x86_64'):
                build.require_linux_x86_64(system, machine)


class PackagingTests(WorkspaceCase):
    def test_launcher_is_valid_posix_sh_and_sets_the_bundled_environment(self):
        launcher = PROJECT / 'packaging/comic-archiver'
        subprocess.run(['sh', '-n', str(launcher)], check=True)
        text = launcher.read_text()
        self.assertTrue(text.startswith('#!/bin/sh\n'))
        self.assertNotIn('PLAYWRIGHT', text)
        self.assertIn('-m comic_archive.desktop_app "$@"', text)
        self.assertIn('export PYTHONSAFEPATH=1', text)
        for name in ('QTWEBENGINE_DISABLE_SANDBOX', 'QTWEBENGINE_CHROMIUM_FLAGS',
                     'QTWEBENGINE_REMOTE_DEBUGGING', 'QTWEBENGINEPROCESS_PATH'):
            self.assertIn(name, text)
        self.assertNotIn('exec ', text)

    def test_desktop_entry_and_icon_are_well_formed(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(PROJECT / 'packaging/comic-archiver.desktop', encoding='utf-8')
        entry = parser['Desktop Entry']
        self.assertEqual((entry['Type'], entry['Name'], entry['Exec'], entry['Icon'], entry['Terminal']),
                         ('Application', 'Comic Archiver', 'comic-archiver', 'comic-archiver', 'false'))
        self.assertEqual(entry['Actions'], 'uninstall;')
        action = parser['Desktop Action uninstall']
        self.assertEqual((action['Name'], action['Exec']), ('Uninstall', 'comic-archiver --uninstall'))
        self.assertTrue(ElementTree.parse(PROJECT / 'packaging/comic-archiver.svg').getroot().tag.endswith('svg'))

    def test_install_packaging_lays_out_launcher_icon_and_licenses(self):
        app = self.root / 'ComicArchiver-1.2.3'
        build.install_packaging(app, '1.2.3')
        self.assertTrue(os.access(app / 'comic-archiver', os.X_OK))
        for name in ('share/applications/comic-archiver.desktop', 'README_FIRST.txt', 'LICENSE', 'THIRD-PARTY-NOTICES.md',
                     'share/icons/hicolor/scalable/apps/comic-archiver.svg'):
            self.assertTrue((app / name).is_file(), name)
        self.assertEqual((app / 'VERSION').read_text(), '1.2.3\n')
        self.assertIn('Copyright (c) 2026 Ben Crute', (app / 'LICENSE').read_text())
        self.assertIn('LGPL', (app / 'THIRD-PARTY-NOTICES.md').read_text())
        self.assertFalse((app / 'comic-archiver.desktop').exists())  # Only a menu entry with absolute paths works.

    def test_drop_entry_scripts_removes_only_scripts_pointing_at_the_build_folder(self):
        app = self.root / 'App'
        bin_ = app / 'python/bin'
        bin_.mkdir(parents=True)
        (bin_ / 'python3.14').write_bytes(b'\x7fELF binary')
        (bin_ / 'python3').symlink_to('python3.14')
        script(bin_ / 'comic-archive', f'#!{app}/python/bin/python3\nprint(1)\n')
        script(bin_ / 'pip3', '#!/install/bin/python3.14\nprint(1)\n')
        # uv switches to this form when the interpreter path is long or contains a space.
        script(bin_ / 'pyside6-uic', f"#!/bin/sh\n'''exec' \"{app}/python/bin/python3\" \"$0\" \"$@\"\n' '''\nprint(1)\n")
        build.drop_entry_scripts(app)
        self.assertEqual(sorted(p.name for p in bin_.iterdir()), ['pip3', 'python3', 'python3.14'])

    def test_collect_licenses_copies_package_texts_and_the_ones_no_package_brings(self):
        app = self.root / 'App'
        site = app / 'python/lib/python3.14/site-packages'
        (site / 'httpx-1.dist-info/licenses').mkdir(parents=True)
        (site / 'httpx-1.dist-info/licenses/LICENSE.md').write_text('BSD')
        (site / 'httpx-1.dist-info/METADATA').write_text('Name: httpx')
        (site / 'PySide6').mkdir()
        (site / 'PySide6/QtCore.abi3.so').write_text('binary')
        (app / 'python/lib/python3.14/LICENSE.txt').write_text('PSF')
        build.collect_licenses(app)
        kept = sorted(p.relative_to(app / 'licenses').as_posix() for p in (app / 'licenses').rglob('*') if p.is_file())
        self.assertEqual(kept, sorted(['httpx-1.dist-info/licenses/LICENSE.md', 'python3.14/LICENSE.txt',
                                       *(f'other/{name}.txt' for name in build.LICENSE_TEXTS)]))
        for name in build.LICENSE_TEXTS:
            self.assertEqual((app / f'licenses/other/{name}.txt').read_bytes(), (PROJECT / f'packaging/licenses/{name}.txt').read_bytes())

    def test_qt_is_not_passed_on_without_the_lgpl_and_the_gpl_it_builds_on(self):
        texts = self.root / 'texts'
        texts.mkdir()
        (texts / 'LGPL-3.0.txt').write_text('GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n')
        (texts / 'GPL-3.0.txt').write_text('GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n')
        (texts / 'zlib.txt').write_text('zlib')
        with patch.object(build, 'LICENSES', texts):
            found = build.copy_license_texts(self.root / 'one', ('GPL-3.0', 'LGPL-3.0', 'zlib'))
            self.assertEqual(sorted(p.name for p in found), ['GPL-3.0.txt', 'LGPL-3.0.txt', 'zlib.txt'])
            with self.assertRaisesRegex(RuntimeError, 'missing.txt is missing'):
                build.copy_license_texts(self.root / 'two', ('GPL-3.0', 'LGPL-3.0', 'missing'))
            with self.assertRaisesRegex(RuntimeError, 'without the GPL-3.0'):
                build.copy_license_texts(self.root / 'three', ('LGPL-3.0', 'zlib'))
            # A text that only names the license is not the license: certifi's MPL text names the LGPL.
            (texts / 'LGPL-3.0.txt').write_text('Mozilla Public License\n... the GNU Lesser General Public License ...')
            with self.assertRaisesRegex(RuntimeError, 'without the LGPL-3.0'):
                build.copy_license_texts(self.root / 'four', ('GPL-3.0', 'LGPL-3.0'))

    def test_every_license_text_kept_for_the_apps_says_where_it_comes_from(self):
        folder = PROJECT / 'packaging/licenses'
        sources = (folder / 'SOURCES.md').read_text('utf-8')
        for path in folder.glob('*.txt'):
            self.assertIn(f'`{path.name}`', sources)
        for name in build.LICENSE_TEXTS:
            self.assertTrue((folder / f'{name}.txt').is_file(), name)
        notices = (PROJECT / 'THIRD-PARTY-NOTICES.md').read_text('utf-8')
        self.assertIn('Copyright (c) 2026 Ben Crute', (PROJECT / 'LICENSE').read_text('utf-8'))
        self.assertIn('licenses/other/', notices)

    def test_frozen_requirements_pin_this_project_without_build_paths(self):
        text = build.frozen_requirements('zstandard==0.25.0\ncomic-archive @ file:///tmp/x.whl\n\nhttpx==0.28.1\n', '0.1.0')
        self.assertEqual(text, 'comic-archive==0.1.0\nhttpx==0.28.1\nzstandard==0.25.0\n')


def program_file(path, loads=(), dynamic=True):
    """Write a Linux program file that holds nothing but its dynamic section and the names in it."""
    base, strings_at = 0x400000, 64 + 56 * 2
    strings, entries = bytearray(b'\0'), []
    for name in loads:
        entries.append((1, len(strings))); strings.extend(name.encode() + b'\0')
    entries += [(5, base + strings_at), (0, 0)]
    table = b''.join(struct.pack('<qQ', tag, value) for tag, value in entries)
    table_at = strings_at + len(strings)
    size = table_at + len(table)
    head = b'\x7fELF\x02\x01\x01' + bytes(9) + struct.pack('<HHIQQQIHHHHHH', 3, 0x3e, 1, 0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    load = struct.pack('<IIQQQQQQ', 1, 4, 0, base, base, size, size, 0x1000)
    found = struct.pack('<IIQQQQQQ', 2 if dynamic else 4, 4, table_at, base + table_at, base + table_at, len(table), len(table), 8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + load + found + bytes(strings) + table)


class LeftOutTests(WorkspaceCase):
    def test_the_qt_modules_kept_in_the_package_are_the_ones_the_app_imports(self):
        # A module the app starts to import and the package lacks would only show as an app that does not start.
        imported = set()
        for source in (PROJECT / 'comic_archive').rglob('*.py'):
            text = source.read_text('utf-8')
            for names in re.findall(r'^\s*from PySide6 import ([\w, ]+)', text, re.M):
                imported.update(name.strip() for name in names.split(','))
            imported.update(re.findall(r'^\s*(?:from|import) PySide6\.(\w+)', text, re.M))
        self.assertEqual(imported, set(build.QT_MODULES))

    def test_what_a_program_file_loads_is_read_from_its_dynamic_section(self):
        program_file(self.root / 'a.so', loads=('libQt6Core.so.6', 'libc.so.6'))
        program_file(self.root / 'static', loads=('libQt6Core.so.6',), dynamic=False)
        self.assertEqual(build.needed_names(self.root / 'a.so'), {'libQt6Core.so.6', 'libc.so.6'})
        self.assertEqual(build.needed_names(self.root / 'static'), set())
        (self.root / 'text.so').write_bytes(b'not a program at all' * 40)
        with self.assertRaisesRegex(RuntimeError, 'text.so'): build.needed_names(self.root / 'text.so')

    def qt(self, **changed):
        """What each program file of a small Qt package loads, shaped like the real one."""
        lib = lambda name: 'Qt/lib/libQt6' + name + '.so.6'
        needs = {module + '.abi3.so': {f'libQt6{module[2:]}.so.6', 'libpyside6.abi3.so.6.11', 'libshiboken6.abi3.so.6.11'}
                 for module in build.QT_MODULES}
        needs.update({name: set() for name in ('libpyside6.abi3.so.6.11', 'Qt/lib/libicuuc.so.73', lib('Svg'), 'designer',
                                               'Qt/lib/libavcodec.so.61')})
        needs.update({lib('Core'): {'libicuuc.so.73', 'libc.so.6'}, lib('Gui'): {'libQt6Core.so.6'},
                      lib('Network'): {'libQt6Core.so.6'}, lib('Widgets'): {'libQt6Gui.so.6'},
                      lib('Quick'): {'libQt6Gui.so.6', 'libQt6Qml.so.6'}, lib('Qml'): {'libQt6Network.so.6'},
                      'QtQml.abi3.so': {'libQt6Qml.so.6'}, lib('WebEngineCore'): {'libQt6Quick.so.6', 'libnss3.so'},
                      'Qt/libexec/QtWebEngineProcess': {'libQt6WebEngineCore.so.6'},
                      lib('WebEngineWidgets'): {'libQt6WebEngineCore.so.6', 'libQt6PrintSupport.so.6'},
                      lib('PrintSupport'): {'libQt6Widgets.so.6'}, 'QtPrintSupport.abi3.so': {'libQt6PrintSupport.so.6'},
                      lib('XcbQpa'): {'libQt6Gui.so.6', 'libxcb.so.1'}, 'Qt/plugins/platforms/libqxcb.so': {'libQt6XcbQpa.so.6'},
                      lib('Multimedia'): {'libQt6Gui.so.6', 'libavcodec.so.61'}, 'QtMultimedia.abi3.so': {'libQt6Multimedia.so.6'},
                      'QtSvg.abi3.so': {'libQt6Svg.so.6'}, 'Qt/plugins/imageformats/libqico.so': {'libQt6Gui.so.6'},
                      'Qt/plugins/imageformats/libqsvg.so': {'libQt6Gui.so.6', 'libQt6Svg.so.6'},
                      'Qt/plugins/sqldrivers/libqsqlite.so': {'libQt6Core.so.6'}})
        needs.update(changed)
        return {name: names for name, names in needs.items() if names is not None}

    def test_qt_keeps_the_apps_modules_what_they_load_the_screen_plugins_and_the_plugins_that_need_nothing_more(self):
        kept = build.qt_needed(self.qt())
        left_out = {'Qt/lib/libQt6Multimedia.so.6', 'QtMultimedia.abi3.so', 'Qt/lib/libavcodec.so.61', 'Qt/lib/libQt6Svg.so.6',
                    'QtSvg.abi3.so', 'designer', 'Qt/plugins/imageformats/libqsvg.so', 'Qt/plugins/sqldrivers/libqsqlite.so'}
        self.assertEqual(kept, self.qt().keys() - left_out)
        # The X11 plugin is kept with the library only it loads; the window could not open without it.
        self.assertLessEqual({'Qt/plugins/platforms/libqxcb.so', 'Qt/lib/libQt6XcbQpa.so.6'}, kept)
        # No dynamic section shows that QtWebEngineWidgets imports QtPrintSupport, nor QtQuick QtQml: Python does that.
        self.assertLessEqual({'QtPrintSupport.abi3.so', 'QtQml.abi3.so'}, kept)
        for lacking in ('QtWebEngineWidgets.abi3.so', 'Qt/libexec/QtWebEngineProcess'):
            with self.assertRaisesRegex(RuntimeError, lacking): build.qt_needed(self.qt(**{lacking: None}))

    def python_tree(self, app):
        python, packages = app / 'python', 'lib/python3.14/site-packages/'
        for name, loads in (('bin/python3.14', ('libpython3.14.so.1.0',)), ('lib/libpython3.14.so.1.0', ()),
                            ('lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so', ('libtcl9tk9.0.so',)),
                            ('lib/libtcl9tk9.0.so', ('libtcl9.0.so',)), ('lib/libtcl9.0.so', ()),
                            ('lib/python3.14/lib-dynload/_dbm.cpython-314-x86_64-linux-gnu.so', ()),
                            (packages + 'PIL/_imaging.cpython-314-x86_64-linux-gnu.so', ()),
                            (packages + 'PIL/_imagingtk.cpython-314-x86_64-linux-gnu.so', ()),
                            (packages + 'shiboken6/libshiboken6.abi3.so.6.11', ())):
            program_file(python / name, loads)
        for name in ('bin/idle3.14', 'bin/pip3', 'include/python3.14/Python.h', 'lib/python3.14/os.py', 'lib/python3.14/turtle.py',
                     'lib/python3.14/__pycache__/turtle.cpython-314.pyc', 'lib/python3.14/tkinter/__init__.py',
                     'lib/python3.14/idlelib/idle.py', 'lib/python3.14/turtledemo/__main__.py', 'lib/python3.14/venv/__init__.py',
                     'lib/python3.14/ensurepip/__init__.py', 'lib/tcl9.0/init.tcl', 'lib/tk9.0/tk.tcl', 'lib/itcl4.3.8/itcl.tcl',
                     'lib/thread3.0.6/pkgIndex.tcl', packages + 'pip/__init__.py', packages + 'comic_archive/cli.py'):
            (python / name).parent.mkdir(parents=True, exist_ok=True); (python / name).write_text('x')

    def test_the_app_folder_loses_what_the_app_never_loads_and_keeps_the_rest(self):
        app = self.root / 'app'; python = app / 'python'
        self.python_tree(app)
        qt = build.site_packages(app) / 'PySide6'
        for name, loads in self.qt().items():
            program_file(qt / name, loads)
        for name in ('__init__.py', 'QtCore.pyi', 'support/__init__.py', 'lib/cmake/Shiboken6.cmake', 'include/pyside.h',
                     'Qt/qml/QtQuick/qmldir', 'Qt/metatypes/qt6core.json', 'Qt/translations/qt_de.qm',
                     'Qt/translations/qtwebengine_locales/en-US.pak', 'Qt/resources/qtwebengine_resources.pak',
                     'Qt/resources/icudtl.dat', 'Qt/resources/qtwebengine_resources.debug.pak',
                     'Qt/resources/qtwebengine_devtools_resources.pak'):
            (qt / name).parent.mkdir(parents=True, exist_ok=True); (qt / name).write_text('x')
        with contextlib.redirect_stdout(io.StringIO()) as said:
            build.leave_out_unused(app)
        self.assertRegex(said.getvalue(), r'^Left out what the app never loads: \d+ of \d+ MB\n$')
        left = {path.relative_to(python).as_posix() for path in python.rglob('*') if path.is_file()}
        packages = 'lib/python3.14/site-packages/'
        self.assertEqual({name for name in left if not name.startswith(packages + 'PySide6/')},
                         {'bin/python3.14', 'bin/pip3', 'include/python3.14/Python.h', 'lib/libpython3.14.so.1.0',
                          'lib/python3.14/os.py', packages + 'PIL/_imaging.cpython-314-x86_64-linux-gnu.so',
                          packages + 'shiboken6/libshiboken6.abi3.so.6.11', packages + 'pip/__init__.py',
                          packages + 'comic_archive/cli.py'})
        self.assertEqual({name[len(packages + 'PySide6/'):] for name in left if name.startswith(packages + 'PySide6/')},
                         build.qt_needed(self.qt()) | {'__init__.py', 'support/__init__.py', 'Qt/translations/qtwebengine_locales/en-US.pak',
                                                       'Qt/resources/qtwebengine_resources.pak', 'Qt/resources/icudtl.dat'})
        self.assertFalse((qt / 'Qt/plugins/sqldrivers').exists())
        self.assertFalse((python / 'lib/tcl9.0').exists())

    def test_a_kept_file_that_loads_one_that_was_left_out_stops_the_build(self):
        # Such an app starts on no computer; the build host must say so, not the first person to install it.
        app = self.root / 'app'
        self.python_tree(app)
        for name, loads in self.qt().items():
            program_file(app / 'python/lib/python3.14/site-packages/PySide6' / name, loads)
        program_file(app / 'python/lib/python3.14/lib-dynload/_tcl.cpython-314-x86_64-linux-gnu.so', ('libtcl9.0.so',))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, r'_tcl\.cpython.* loads libtcl9\.0\.so'):
            build.leave_out_unused(app)


class ArchiveTests(WorkspaceCase):
    def tree(self):
        app = self.root / 'ComicArchiver-1.0'
        (app / 'python/bin').mkdir(parents=True)
        (app / 'python/bin/python3.14').write_bytes(b'interpreter')
        (app / 'python/bin/python3.14').chmod(0o755)
        (app / 'python/bin/python3').symlink_to('python3.14')
        script(app / 'comic-archiver', '#!/bin/sh\n')
        (app / 'LICENSE').write_text('MIT')
        return app

    def test_archive_has_one_top_folder_keeps_links_and_modes_and_matches_its_checksum(self):
        app = self.tree()
        archive, checksum = build.make_archive(app, self.root / 'dist', '1.0')
        self.assertEqual(archive.name, 'ComicArchiver-1.0-linux-x86_64.tar.gz')
        self.assertEqual(checksum.name, archive.name + '.sha256')
        self.assertEqual(checksum.read_text(), f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
        self.assertFalse(list(archive.parent.glob('*.partial')))
        with tarfile.open(archive) as package:
            members = {m.name: m for m in package.getmembers()}
        self.assertTrue(all(n == app.name or n.startswith(app.name + '/') for n in members))
        link = members[app.name + '/python/bin/python3']
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, 'python3.14')
        self.assertTrue(members[app.name + '/comic-archiver'].mode & stat.S_IXUSR)
        self.assertEqual({(m.uid, m.gid, m.uname) for m in members.values()}, {(0, 0, 'root')})

    def test_archive_bytes_are_repeatable_for_the_same_tree(self):
        app = self.tree()
        first, _ = build.make_archive(app, self.root / 'one', '1.0')
        second, _ = build.make_archive(app, self.root / 'two', '1.0')
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_archive_bytes_do_not_depend_on_when_the_files_were_written(self):
        app = self.tree()
        with patch.dict(os.environ):
            os.environ.pop('SOURCE_DATE_EPOCH', None)
            first, _ = build.make_archive(app, self.root / 'one', '1.0')
            for path in (app, app / 'LICENSE', app / 'python/bin/python3.14'):
                os.utime(path, (1_000_000_000, 1_000_000_000))
            second, _ = build.make_archive(app, self.root / 'two', '1.0')
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(second) as package:
                self.assertEqual({member.mtime for member in package.getmembers()}, {build.ARCHIVE_TIME})
            os.environ['SOURCE_DATE_EPOCH'] = '1700000000'
            third, _ = build.make_archive(app, self.root / 'three', '1.0')
            with tarfile.open(third) as package:
                self.assertEqual({member.mtime for member in package.getmembers()}, {1700000000})


    def test_smoke_runs_against_the_unpacked_archive_not_the_build_tree(self):
        app = self.tree()
        archive, _ = build.make_archive(app, self.root / 'dist', '1.0')
        seen = []
        with patch.object(build, 'smoke', side_effect=lambda folder, release: seen.append((folder, release, os.access(folder / 'comic-archiver', os.X_OK),
                                                                                           (folder / 'python/bin/python3').is_symlink()))):
            build.smoke_archive(app, archive, '1.0')
        (folder, release, executable, linked), = seen
        self.assertFalse(app.exists())
        self.assertNotEqual(folder, app)
        self.assertIn(' ', str(folder))
        self.assertEqual((release, executable, linked), ('1.0', True, True))
        self.assertFalse(folder.exists())


class OrchestrationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.real_audit = build.require_audit
        patch.object(build, 'BUILD', self.root / 'build').start()
        patch.object(build, 'DIST', self.root / 'dist').start()
        patch('scripts.build_linux_app.shutil.which', return_value='/usr/bin/uv').start()
        self.installs = []

        def python(app):
            script(app / 'python/bin/python3', '#!/bin/sh\n')
            script(app / 'python/bin/comic-archive', f'#!{app}/python/bin/python3\n')
            (app / 'python/lib/python3.14/site-packages/PySide6').mkdir(parents=True)
            return app / 'python/bin/python3'

        self.bundle = patch.object(build, 'bundle_python', side_effect=python).start()
        patch.object(build, 'build_wheel', return_value=self.root / 'comic_archive-0.whl').start()
        patch.object(build, 'uv_install', side_effect=lambda *args: self.installs.append(args[1])).start()
        self.run = patch.object(build, 'run', return_value=MagicMock(stdout='httpx==0.28.1\ncomic-archive @ file:///x.whl\n')).start()
        self.left_out = patch.object(build, 'leave_out_unused').start()

        def compiled(python, app, folder):
            cache = app / 'python/lib/python3.14/site-packages/comic_archive/__pycache__'
            cache.mkdir(parents=True)
            (cache / 'desktop_app.cpython-314.pyc').write_bytes(b'compiled')

        self.compiled = patch.object(build, 'compile_bytecode', side_effect=compiled).start()
        self.audit = patch.object(build, 'require_audit').start()

    def test_build_assembles_and_packs_the_app(self):
        app, archive, checksum = build.build()
        release = build.version()
        self.assertEqual(archive, self.root / 'dist' / f'ComicArchiver-{release}-linux-x86_64.tar.gz')
        self.assertTrue(checksum.is_file())
        self.assertEqual(self.installs, [[f'{self.root}/comic_archive-0.whl[desktop,validation]']])
        self.assertEqual(self.run.call_args_list[0].args[0][:5], ['uv', 'pip', 'list', '--format', 'freeze'])
        audit = self.run.call_args.args[0]
        self.assertEqual(audit[1:], [PROJECT / 'scripts/audit_bundle.py', app / 'requirements-frozen.txt',
                                    '--output', app / 'dependency-audit.json'])
        with tarfile.open(archive) as package:
            names = set(package.getnames())
            frozen = package.extractfile(f'{app.name}/requirements-frozen.txt').read().decode()
        self.assertIn(f'{app.name}/comic-archiver', names)
        self.assertFalse([n for n in names if 'browsers' in n.split('/')])
        self.assertNotIn(f'{app.name}/python/bin/comic-archive', names)
        self.left_out.assert_called_once_with(app)
        self.compiled.assert_called_once_with(app / 'python/bin/python3', app, app / 'python/lib')
        self.assertIn(f'{app.name}/python/lib/python3.14/site-packages/comic_archive/__pycache__/desktop_app.cpython-314.pyc', names)
        self.assertIn(f'{app.name}/licenses/other/LGPL-3.0.txt', names)
        self.assertIn(f'{app.name}/share/applications/comic-archiver.desktop', names)
        self.assertEqual(frozen, f'comic-archive=={release}\nhttpx==0.28.1\n')

    def test_an_app_whose_modules_were_not_compiled_is_not_packed(self):
        self.compiled.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'not compiled'):
            build.build()
        self.assertFalse((self.root / 'dist').exists())

    def test_a_python_without_the_audit_tool_is_refused_before_anything_is_built(self):
        self.audit.side_effect = RuntimeError('pip-audit is required')
        with self.assertRaisesRegex(RuntimeError, 'pip-audit is required'):
            build.build()
        self.bundle.assert_not_called()
        self.audit.side_effect = None
        with patch('scripts.build_linux_app.importlib.util.find_spec', return_value=None) as found, \
                patch.object(build, 'require_audit', self.real_audit):
            with self.assertRaisesRegex(RuntimeError, 'pip-audit is required'):
                build.require_audit()
            found.assert_called_once_with('pip_audit')
            found.return_value = object()
            build.require_audit()

    def test_missing_uv_publishes_nothing(self):
        with patch('scripts.build_linux_app.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'uv is required'):
                build.build()
        self.bundle.assert_not_called()
        self.assertFalse((self.root / 'dist').exists())

    def test_the_app_installs_no_playwright(self):
        extras = tomllib.loads((PROJECT / 'pyproject.toml').read_text('utf-8'))['project']['optional-dependencies']
        wanted = [requirement for name in build.EXTRAS.split(',') for requirement in extras[name]]
        self.assertTrue(wanted)
        self.assertFalse([requirement for requirement in wanted if 'playwright' in requirement.lower()])

    def test_source_copy_excludes_bytecode_and_build_output(self):
        destination = self.root / 'src'
        build.copy_sources(destination)
        self.assertTrue((destination / 'comic_archive/local_library.py').is_file())
        self.assertTrue((destination / 'LICENSE').is_file())
        self.assertFalse(list(destination.rglob('*.pyc')))
        self.assertFalse((destination / 'build').exists())


class CommandLineTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        patch.object(build, 'BUILD', self.root / 'build').start()
        self.calls = []
        self.process = patch('scripts.build_linux_app.subprocess.run', side_effect=self.record).start()
        self.on_run = lambda args, kwargs: None

    def record(self, args, **kwargs):
        self.calls.append((args, kwargs))
        self.on_run(args, kwargs)
        return MagicMock(stdout='')

    def wheels(self, count):
        def write(args, kwargs):
            out = Path(args[args.index('--out-dir') + 1])
            for number in range(count):
                out.mkdir(parents=True, exist_ok=True)
                (out / f'comic_archive-{number}-py3-none-any.whl').write_bytes(b'wheel')
        self.on_run = write

    def test_wheel_is_built_with_uv_from_a_clean_copy_and_exactly_one_result_is_accepted(self):
        work = self.root / 'work'
        self.wheels(1)
        wheel = build.build_wheel(work)
        args, kwargs = self.calls[0]
        self.assertEqual(args, ['uv', 'build', '--wheel', '--build-constraints', str(build.PACKAGING / 'build-constraints.txt'),
                                '--require-hashes', '--out-dir', str(work / 'wheel'), str(work / 'src')])
        self.assertEqual(wheel, work / 'wheel/comic_archive-0-py3-none-any.whl')
        self.assertTrue(kwargs['check'])
        self.assertNotIn('PYTHONPATH', kwargs['env'])
        for count in (0, 2):
            self.wheels(count)
            with self.assertRaisesRegex(RuntimeError, 'exactly one'):
                build.build_wheel(work)

    def test_packages_install_into_the_bundled_python(self):
        with patch.dict(os.environ, {'VIRTUAL_ENV': '/leak', 'PYTHONPATH': '/leak'}):
            build.uv_install(Path('/app/python/bin/python3'), ['/w/x.whl[desktop,validation]'])
        command = ['uv', 'pip', 'install', '--python', '/app/python/bin/python3', '--break-system-packages', '--no-compile']
        self.assertEqual([args for args, _ in self.calls],
                         [[*command, '--require-hashes', '--requirement', str(build.PACKAGING / 'linux-requirements.txt')],
                          [*command, '--no-deps', '/w/x.whl[desktop,validation]'],
                          ['uv', 'pip', 'check', '--python', '/app/python/bin/python3']])
        for _, kwargs in self.calls:
            self.assertEqual(kwargs['env']['UV_PYTHON_DOWNLOADS'], 'never')
            self.assertTrue(all(name not in kwargs['env'] for name in ('VIRTUAL_ENV', 'PYTHONPATH')))

    def compile(self, version=None, write=None):
        app = self.root / 'build/ComicArchiver-1.0'
        folder = app / 'python/lib'
        (folder / 'pkg/__pycache__').mkdir(parents=True)

        def ran(args, kwargs):
            if 'compileall' in args and write is not None:
                (folder / 'pkg/__pycache__/module.cpython-314.pyc').write_bytes(write(app))

        self.on_run = ran
        self.process.side_effect = lambda args, **kwargs: (
            self.record(args, **kwargs), MagicMock(stdout=(version or build.PYTHON_RELEASE) + '\n'))[1]
        build.compile_bytecode(app / 'python/bin/python3', app, folder)
        return app, folder

    def test_modules_are_compiled_apart_from_their_install_and_never_name_the_build_folder(self):
        app, folder = self.compile(write=lambda app: b'code python/lib/pkg/module.py')
        python = str(app / 'python/bin/python3')
        self.assertEqual([args for args, _ in self.calls],
                         [[python, '-I', '-c', 'import platform; print(platform.python_version())'],
                          [python, '-I', '-m', 'compileall', '-qq', '-f', '-j', '0', '--invalidation-mode', 'unchecked-hash',
                           '-s', str(app), str(folder)]])
        self.assertNotIn('check', self.calls[1][1])  # Some packaged files are not meant to compile.
        self.assertTrue(all(name not in self.calls[1][1]['env'] for name in ('VIRTUAL_ENV', 'PYTHONPATH')))

    def test_a_compiled_module_that_names_the_build_folder_stops_the_build(self):
        with self.assertRaisesRegex(RuntimeError, 'names the build folder: python/lib/pkg/__pycache__/module'):
            self.compile(write=lambda app: b'code ' + os.fsencode(app) + b'/python/lib/pkg/module.py')

    def test_modules_are_compiled_only_by_the_python_version_that_is_bundled(self):
        with self.assertRaisesRegex(RuntimeError, 'compiled by Python 3.13.1'):
            self.compile(version='3.13.1')
        self.assertEqual(len(self.calls), 1)  # Nothing was compiled.

    def test_every_build_names_the_one_python_release(self):
        self.assertEqual(build.PBS_NAME, f'cpython-{build.PYTHON_RELEASE}+{build.PBS_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz')
        self.assertEqual(build.pbs_name('aarch64-apple-darwin'),
                         f'cpython-{build.PYTHON_RELEASE}+{build.PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz')
        for name in ('build_macos_app.py', 'build_windows_app.py'):
            self.assertNotIn(build.PYTHON_RELEASE, (PROJECT / 'scripts' / name).read_text('utf-8'), name)

    def test_what_builds_the_wheel_is_pinned_with_its_hashes_and_is_what_the_project_asks_for(self):
        text = (build.PACKAGING / 'build-constraints.txt').read_text()
        pins = re.findall(r'^([a-z0-9-]+)==(\S+) \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)', text, re.M)
        self.assertEqual([name for name, _, _ in pins], ['setuptools'])
        self.assertEqual(len(pins), len(re.findall(r'^[a-z0-9-]+==', text, re.M)))
        asked = tomllib.loads((PROJECT / 'pyproject.toml').read_text('utf-8'))['build-system']['requires']
        self.assertEqual(asked, ['setuptools>=77'])
        self.assertGreaterEqual(int(pins[0][1].split('.')[0]), 77)

    def test_every_bundled_package_is_pinned_with_its_hashes(self):
        for name in ('linux', 'windows'):
            text = (build.PACKAGING / f'{name}-requirements.txt').read_text()
            pins = re.findall(r'^([a-z0-9-]+)==\S+ \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)', text, re.M)
            with self.subTest(name):
                self.assertIn('pyside6', [pin for pin, _ in pins])
                self.assertEqual(len(pins), len(re.findall(r'^[a-z0-9-]+==', text, re.M)))

    def test_a_host_package_mirror_or_extra_index_never_reaches_the_bundled_install(self):
        leaks = dict.fromkeys(('UV_INDEX_URL', 'UV_EXTRA_INDEX_URL', 'UV_INDEX', 'UV_DEFAULT_INDEX', 'UV_FIND_LINKS',
                               'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_FIND_LINKS'), 'https://mirror.invalid/simple')
        kept = {'UV_CACHE_DIR': '/cache', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'COMIC_ARCHIVER_BUILD_DOWNLOAD_CACHE': '/downloads'}
        with patch.dict(os.environ, leaks | kept):
            build.uv_install(Path('/app/python/bin/python3'), ['/w/x.whl'])
        env = self.calls[0][1]['env']
        self.assertEqual([name for name in leaks if name in env], [])
        self.assertEqual({name: env.get(name) for name in kept}, kept)


class MainTests(WorkspaceCase):
    def invoke(self, *args):
        errors = io.StringIO()
        output = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
            code = build.main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_refuses_other_platforms_without_building(self):
        with patch('scripts.build_linux_app.platform.system', return_value='Windows'), patch.object(build, 'build') as made:
            code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn('Linux x86_64', errors)
        made.assert_not_called()

    def test_smoke_runs_only_when_requested_and_after_the_archive_exists(self):
        archive, checksum = self.root / 'a.tar.gz', self.root / 'a.tar.gz.sha256'
        checksum.write_text('abc  a.tar.gz\n')
        with patch.object(build, 'require_linux_x86_64'), patch.object(build, 'build', return_value=(self.root, archive, checksum)), \
                patch.object(build, 'smoke_archive') as smoke:
            code, output, _ = self.invoke()
            smoke.assert_not_called()
            self.assertEqual((code, json.loads(output)['smoke']), (0, False))
            code, output, _ = self.invoke('--smoke')
        smoke.assert_called_once_with(self.root, archive, build.version())
        self.assertEqual((code, json.loads(output)['sha256']), (0, 'abc'))

    def test_failures_return_one_with_a_message(self):
        with patch.object(build, 'require_linux_x86_64'):
            with patch.object(build, 'build', side_effect=RuntimeError('boom')):
                code, _, errors = self.invoke()
            self.assertEqual(code, 1)
            self.assertIn('boom', errors)
            with patch.object(build, 'build', side_effect=subprocess.CalledProcessError(2, ['uv'])):
                self.assertEqual(self.invoke()[0], 1)


class SmokeTests(WorkspaceCase):
    def app(self, version_line='comic-archive 9.9.9', self_test='{"ok": true}', exit_code=0, doctor_exit=0):
        app = self.root / 'App'
        shutil.rmtree(app, ignore_errors=True)
        script(app / 'comic-archiver', f'''#!/bin/sh
case "$1" in
    --version) echo "{version_line}";;
    --self-test) printf '%s\\n' "$QT_QPA_PLATFORM" > "$(dirname "$0")/qt.txt"; echo noise; echo '{self_test}'; exit {exit_code};;
esac
''')
        script(app / 'python/bin/python3', '#!/bin/sh\n{ printf "%s " "$@"; printf "|%s|%s|%s" "$QT_QPA_PLATFORM" "$DISPLAY" "$WAYLAND_DISPLAY"; } > "$(dirname "$0")/doctor.txt"\n' + f'exit {doctor_exit}\n')
        return app

    def test_smoke_checks_version_selftest_offscreen_and_the_built_in_engine(self):
        app = self.app()
        with patch.dict(os.environ, {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0'}):
            build.smoke(app, '9.9.9')
        self.assertEqual((app / 'qt.txt').read_text(), 'offscreen\n')
        # The engine check runs without a display and without help from the environment, as it does inside the app.
        self.assertEqual((app / 'python/bin/doctor.txt').read_text(), '-m comic_archive --browser qt --headless doctor |||')

    def test_smoke_fails_on_wrong_version_selftest_false_or_nonzero_exit(self):
        with self.assertRaisesRegex(RuntimeError, 'version'):
            build.smoke(self.app('comic-archive 1.0.0'), '9.9.9')
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'ok true'):
                build.smoke(self.app(self_test='{"ok": false}'), '9.9.9')
            with self.assertRaisesRegex(RuntimeError, 'ok true'):
                build.smoke(self.app(self_test='not json'), '9.9.9')
            with self.assertRaises(subprocess.CalledProcessError):
                build.smoke(self.app(exit_code=1), '9.9.9')
            with self.assertRaises(subprocess.CalledProcessError):
                build.smoke(self.app(doctor_exit=3), '9.9.9')

    def test_smoke_uses_an_isolated_home_and_never_the_existing_sign_in(self):
        app = self.app()
        owner = self.root / 'existing-home'
        owner.mkdir()
        state = owner / 'state.json'
        state.write_text('PRIVATE_EXISTING_SESSION')
        before = state.read_bytes(), state.stat().st_mtime_ns
        environments = []

        def execute(args, *, env, **kwargs):
            environments.append(dict(env))
            home = Path(env['HOME'])
            self.assertNotEqual(home, owner)
            self.assertTrue(home.is_dir())
            for key in ('XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
                self.assertTrue(Path(env[key]).is_relative_to(home))
            self.assertNotIn('DISPLAY', env)
            self.assertNotIn('WAYLAND_DISPLAY', env)
            output = 'comic-archive 9.9.9' if args[-1] == '--version' else '{"ok": true}'
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr='')

        with patch.dict(os.environ, HOME=str(owner), DISPLAY=':0', WAYLAND_DISPLAY='wayland-0',
                        XDG_STATE_HOME=str(owner), XDG_DATA_HOME=str(owner),
                        XDG_CONFIG_HOME=str(owner), XDG_CACHE_HOME=str(owner)), \
                patch.object(build, 'run', side_effect=execute):
            build.smoke(app, '9.9.9')
        self.assertEqual(len(environments), 3)
        self.assertEqual(len({env['HOME'] for env in environments}), 1)
        self.assertFalse(Path(environments[0]['HOME']).exists())
        self.assertEqual((state.read_bytes(), state.stat().st_mtime_ns), before)


class LauncherBehaviourTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / 'home'
        self.data = self.root / 'data'
        self.note = self.root / 'note.txt'
        self.record = self.root / 'record.txt'
        fakebin = self.root / 'fakebin'
        script(fakebin / 'zenity', '#!/bin/sh\nprintf "%s\\n" "$@" > "$NOTE"\n')
        self.fakebin = fakebin
        self.path = f'{fakebin}{os.pathsep}{os.environ.get("PATH", "")}'

    def install(self, name='Comic App'):
        app = self.root / name
        app.mkdir(parents=True)
        shutil.copy2(PROJECT / 'packaging/comic-archiver', app / 'comic-archiver')
        (app / 'share/applications').mkdir(parents=True)
        shutil.copy2(PROJECT / 'packaging/comic-archiver.desktop', app / 'share/applications/comic-archiver.desktop')
        script(app / 'python/bin/python3', '#!/bin/sh\nprintf "%s\\n" "$PYTHONNOUSERSITE" "${PYTHONPATH-unset}" "$PYTHONSAFEPATH" "${COMIC_ARCHIVER_PREVIOUS-unset}" "$@" > "$RECORD"\n'
                                           'sleep "${FAKE_SLEEP:-0}"\nexit "${FAKE_STATUS:-0}"\n')
        return app

    def launch(self, app, *args, status=0, run_seconds=0, grace=None):
        env = dict(os.environ, HOME=str(self.home), XDG_DATA_HOME=str(self.data), PATH=self.path, RECORD=str(self.record),
                   NOTE=str(self.note), FAKE_STATUS=str(status), FAKE_SLEEP=str(run_seconds), PYTHONPATH='leak')
        if grace is not None:
            env['COMIC_ARCHIVER_START_GRACE'] = str(grace)
        return subprocess.run([str(app / 'comic-archiver'), *args], env=env, capture_output=True, text=True, timeout=60)

    @property
    def entry(self):
        return self.data / 'applications/comic-archiver.desktop'

    def test_runs_bundled_python_with_private_environment_and_arguments(self):
        app = self.install()
        done = self.launch(app, '--project-root', 'a b')
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.record.read_text().splitlines(),
                         ['1', 'unset', '1', 'unset',
                          '-m', 'comic_archive.desktop_app', '--project-root', 'a b'])

    def test_registers_a_menu_entry_with_absolute_paths_and_repairs_a_stale_one(self):
        app = self.install()
        self.launch(app)
        text = self.entry.read_text()
        self.assertIn(f'Exec="{app}/comic-archiver"\n', text)
        self.assertIn(f'[Desktop Action uninstall]\nName=Uninstall\nExec="{app}/comic-archiver" --uninstall\n', text)
        self.assertIn(f'Icon={app}/share/icons/hicolor/scalable/apps/comic-archiver.svg\n', text)
        self.assertIn('Name=Comic Archiver\n', text)
        self.entry.write_text('stale')
        self.launch(app)
        self.assertEqual(self.entry.read_text(), text)
        self.assertEqual([p.name for p in self.entry.parent.iterdir()], ['comic-archiver.desktop'])

    def test_the_copy_the_menu_entry_started_until_now_is_passed_on_to_the_app(self):
        old, new = self.install('Old'), self.install('New')
        self.launch(old)
        self.assertEqual(self.record.read_text().splitlines()[3], 'unset')
        self.launch(new)
        self.assertEqual(self.record.read_text().splitlines()[3], str(old))
        self.launch(new)
        self.assertEqual(self.record.read_text().splitlines()[3], 'unset')  # Itself is no older copy.
        self.entry.write_text(f'[Desktop Action uninstall]\nExec="{old}/comic-archiver" --uninstall\n')
        self.launch(new)
        self.assertEqual(self.record.read_text().splitlines()[3], 'unset')

    def test_version_self_test_and_uninstall_runs_do_not_write_a_menu_entry(self):
        app = self.install()
        for flag in ('--version', '--self-test', '--uninstall'):
            self.assertEqual(self.launch(app, flag).returncode, 0)
            self.assertEqual(self.record.read_text().splitlines()[-1], flag)
        self.assertFalse(self.data.exists())

    def test_paths_the_desktop_format_cannot_quote_get_no_entry_but_still_run(self):
        app = self.install('odd"name')
        self.assertEqual(self.launch(app).returncode, 0)
        self.assertTrue(self.record.is_file())
        self.assertFalse(self.entry.exists())

    def library_list(self, listing):
        script(self.fakebin / 'ldconfig', f'#!/bin/sh\ncat <<EOF\n{listing}\nEOF\n')

    def test_an_early_crash_gets_plain_words_and_names_packages_only_when_the_library_is_missing(self):
        app = self.install()
        self.library_list('\tlibxcb.so.1 (libc6,x86-64) => /lib/libxcb.so.1')
        done = self.launch(app, status=134)
        self.assertEqual(done.returncode, 134)
        text = self.note.read_text()
        self.assertIn('may be missing a graphics component', text)
        self.assertIn('desktop.log', text)
        self.assertIn('Technical details', text)
        self.assertIn('libxcb-cursor0', text)
        self.note.unlink()
        self.library_list('\tlibxcb-cursor.so.0 (libc6,x86-64) => /lib/libxcb-cursor.so.0')
        self.launch(app, status=139)
        text = self.note.read_text()
        self.assertIn('may be missing a graphics component', text)
        self.assertNotIn('sudo apt', text); self.assertNotIn('libxcb-cursor0', text)

    def test_a_missing_system_library_list_never_blames_a_specific_package(self):
        app = self.install()
        script(self.fakebin / 'ldconfig', '#!/bin/sh\nexit 1\n')
        self.launch(app, status=139)
        self.assertNotIn('sudo apt', self.note.read_text())

    def test_other_statuses_and_late_crashes_stay_quiet(self):
        app = self.install()
        for status in (0, 1, 130, 143):
            self.assertEqual(self.launch(app, status=status).returncode, status)
            self.assertFalse(self.note.exists())
        # A crash after the window has been up for a while is not a start-up problem.
        self.assertEqual(self.launch(app, status=139, run_seconds=2, grace=1).returncode, 139)
        self.assertFalse(self.note.exists())


if __name__ == '__main__':
    unittest.main()
