import configparser
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
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
        self.assertNotIn('exec ', text)

    def test_desktop_entry_and_icon_are_well_formed(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(PROJECT / 'packaging/comic-archiver.desktop', encoding='utf-8')
        entry = parser['Desktop Entry']
        self.assertEqual((entry['Type'], entry['Name'], entry['Exec'], entry['Icon'], entry['Terminal']),
                         ('Application', 'Comic Archiver', 'comic-archiver', 'comic-archiver', 'false'))
        self.assertTrue(ElementTree.parse(PROJECT / 'packaging/comic-archiver.svg').getroot().tag.endswith('svg'))

    def test_install_packaging_lays_out_launcher_icon_and_licences(self):
        app = self.root / 'ComicArchiver-1.2.3'
        build.install_packaging(app, '1.2.3')
        self.assertTrue(os.access(app / 'comic-archiver', os.X_OK))
        for name in ('share/applications/comic-archiver.desktop', 'README_FIRST.txt', 'LICENSE', 'THIRD-PARTY-NOTICES.md',
                     'share/icons/hicolor/scalable/apps/comic-archiver.svg'):
            self.assertTrue((app / name).is_file(), name)
        self.assertEqual((app / 'VERSION').read_text(), '1.2.3\n')
        self.assertIn('Copyright (c) 2026 Comic Archiver contributors', (app / 'LICENSE').read_text())
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

    def test_collect_licences_copies_package_texts_and_demands_the_qt_lgpl(self):
        app = self.root / 'App'
        site = app / 'python/lib/python3.14/site-packages'
        (site / 'httpx-1.dist-info/licenses').mkdir(parents=True)
        (site / 'httpx-1.dist-info/licenses/LICENSE.md').write_text('BSD')
        (site / 'httpx-1.dist-info/METADATA').write_text('Name: httpx')
        (app / 'python/lib/python3.14/LICENSE.txt').write_text('PSF')
        with self.assertRaisesRegex(RuntimeError, 'LGPL'):
            build.collect_licences(app)
        (site / 'PySide6').mkdir()
        (site / 'PySide6/LICENSE.LGPLv3').write_text('GNU LESSER GENERAL PUBLIC LICENSE Version 3')
        (site / 'PySide6/QtCore.so').write_text('binary')
        build.collect_licences(app)
        kept = sorted(p.relative_to(app / 'licenses').as_posix() for p in (app / 'licenses').rglob('*') if p.is_file())
        self.assertEqual(kept, ['PySide6/LICENSE.LGPLv3', 'httpx-1.dist-info/licenses/LICENSE.md', 'python3.14/LICENSE.txt'])

    def test_frozen_requirements_pin_this_project_without_build_paths(self):
        text = build.frozen_requirements('zstandard==0.25.0\ncomic-archive @ file:///tmp/x.whl\n\nhttpx==0.28.1\n', '0.1.0')
        self.assertEqual(text, 'comic-archive==0.1.0\nhttpx==0.28.1\nzstandard==0.25.0\n')


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
        patch.object(build, 'BUILD', self.root / 'build').start()
        patch.object(build, 'DIST', self.root / 'dist').start()
        patch('scripts.build_linux_app.shutil.which', return_value='/usr/bin/uv').start()
        self.installs = []

        def python(app):
            script(app / 'python/bin/python3', '#!/bin/sh\n')
            script(app / 'python/bin/comic-archive', f'#!{app}/python/bin/python3\n')
            (app / 'python/lib/python3.14/site-packages/PySide6').mkdir(parents=True)
            (app / 'python/lib/python3.14/site-packages/PySide6/LICENSE.LGPLv3').write_text('GNU Lesser General Public License')
            return app / 'python/bin/python3'

        self.bundle = patch.object(build, 'bundle_python', side_effect=python).start()
        patch.object(build, 'build_wheel', return_value=self.root / 'comic_archive-0.whl').start()
        patch.object(build, 'uv_install', side_effect=lambda *args: self.installs.append(args[1])).start()
        self.run = patch.object(build, 'run', return_value=MagicMock(stdout='httpx==0.28.1\ncomic-archive @ file:///x.whl\n')).start()

    def test_build_assembles_and_packs_the_app(self):
        app, archive, checksum = build.build()
        release = build.version()
        self.assertEqual(archive, self.root / 'dist' / f'ComicArchiver-{release}-linux-x86_64.tar.gz')
        self.assertTrue(checksum.is_file())
        self.assertEqual(self.installs, [[f'{self.root}/comic_archive-0.whl[desktop,validation]']])
        self.assertEqual(self.run.call_args.args[0][:3], ['uv', 'pip', 'freeze'])
        with tarfile.open(archive) as package:
            names = set(package.getnames())
            frozen = package.extractfile(f'{app.name}/requirements-frozen.txt').read().decode()
        self.assertIn(f'{app.name}/comic-archiver', names)
        self.assertFalse([n for n in names if 'browsers' in n.split('/')])
        self.assertNotIn(f'{app.name}/python/bin/comic-archive', names)
        self.assertIn(f'{app.name}/licenses/PySide6/LICENSE.LGPLv3', names)
        self.assertIn(f'{app.name}/share/applications/comic-archiver.desktop', names)
        self.assertEqual(frozen, f'comic-archive=={release}\nhttpx==0.28.1\n')

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
        self.assertTrue((destination / 'comic_archive/local_web.py').is_file())
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
        self.assertEqual(args, ['uv', 'build', '--wheel', '--out-dir', str(work / 'wheel'), str(work / 'src')])
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
        args, kwargs = self.calls[0]
        self.assertEqual(args, ['uv', 'pip', 'install', '--python', '/app/python/bin/python3', '--break-system-packages',
                                '--compile-bytecode', '/w/x.whl[desktop,validation]'])
        self.assertEqual(kwargs['env']['UV_PYTHON_DOWNLOADS'], 'never')
        self.assertTrue(all(name not in kwargs['env'] for name in ('VIRTUAL_ENV', 'PYTHONPATH')))


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
        script(app / 'python/bin/python3', '#!/bin/sh\nprintf "%s\\n" "$PYTHONNOUSERSITE" "${PYTHONPATH-unset}" "$PYTHONSAFEPATH" "$@" > "$RECORD"\n'
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
                         ['1', 'unset', '1',
                          '-m', 'comic_archive.desktop_app', '--project-root', 'a b'])

    def test_registers_a_menu_entry_with_absolute_paths_and_repairs_a_stale_one(self):
        app = self.install()
        self.launch(app)
        text = self.entry.read_text()
        self.assertIn(f'Exec="{app}/comic-archiver"\n', text)
        self.assertIn(f'Icon={app}/share/icons/hicolor/scalable/apps/comic-archiver.svg\n', text)
        self.assertIn('Name=Comic Archiver\n', text)
        self.entry.write_text('stale')
        self.launch(app)
        self.assertEqual(self.entry.read_text(), text)
        self.assertEqual([p.name for p in self.entry.parent.iterdir()], ['comic-archiver.desktop'])

    def test_version_and_self_test_runs_do_not_write_outside_the_app(self):
        app = self.install()
        for flag in ('--version', '--self-test'):
            self.assertEqual(self.launch(app, flag).returncode, 0)
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
