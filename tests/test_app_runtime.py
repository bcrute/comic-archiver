from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE, PRIVATE_FOLDER, STATE_DEFAULT, STATE_HOME, home_environment, junction
from comic_archive import app_runtime, archive_paths, sites, support_log
from comic_archive.app_actions import Actions
from comic_archive.account_ui import DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL
from comic_archive.errors import LibraryBusy, LibraryMoved
from comic_archive.local_library import LOCAL_OWNER
from comic_archive.models import ArchiveError
from comic_archive.session import load_session
from comic_archive.window_signin import WindowSignIn
from comic_archive.worker import WORKER_VERSION


class AppRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root, self.state = self.home / 'Comic Archiver', self.home / 'state'
        self.background = {'status': Mock(return_value={'running': False, 'state': 'stopped'}),
                           'start': Mock(return_value={'running': True}), 'stop': Mock(return_value={'running': False})}
        for wrapper in [patch.dict(os.environ, home_environment(self.home, self.state)),
                        patch('comic_archive.app_runtime.RETRY_SECONDS', .01),
                        # Python's own hooks and crash file stay as the tests found them.
                        patch('comic_archive.support_log.catch_unexpected'), patch('comic_archive.support_log.catch_crashes'),
                        *(patch('comic_archive.app_runtime.background.' + name, value) for name, value in self.background.items())]:
            wrapper.start(); self.addCleanup(wrapper.stop)
        self.addCleanup(self.reset_logging)
        self.runtime = app_runtime.Runtime(self.root)
        self.addCleanup(self.runtime.stop, 0)

    def sites(self, root=None):
        """Every site of the library folder, as the window opens them."""
        library = app_runtime.Library(root or self.root)
        self.addCleanup(library.stop, 0)
        return library

    def reset_logging(self):
        for log in (app_runtime.LOG, logging.getLogger(support_log.DOWNLOAD_LOG)):
            for handler in list(log.handlers):
                log.removeHandler(handler); handler.close()
            log.addHandler(logging.NullHandler())

    def sign_in(self):
        session = self.root / 'Tapas/.comic-session'; session.mkdir(parents=True, exist_ok=True)
        (session / 'state.json').write_text('{}')
        return session

    def library(self, runtime):
        """The folder of saved episodes the window reads through this runtime now."""
        registry = runtime.registry
        return registry.resolve(LOCAL_OWNER, registry.workspace_id).library

    def legacy_libraries(self):
        """Existing provider data before normalization, built without starting the app."""
        from comic_archive.local_library import LocalWorkspaceRegistry
        result = {}
        for site in (sites.DEFAULT, sites.named('webtoon'), sites.named('tappytoon')):
            place = self.root if site == sites.DEFAULT else self.root / site.name
            place.mkdir(parents=True, exist_ok=True)
            registry = LocalWorkspaceRegistry(place)
            self.addCleanup(registry.cloud_signin.close)
            session = place / '.comic-session/state.json'
            session.write_text(json.dumps({'cookies': [], 'origins': []}))
            archive = place / 'Library/Series/episode.cbz'; archive.parent.mkdir()
            archive.write_bytes(b'original archive')
            result[site.key] = {'workspace_id':registry.workspace_id, 'inode':archive.stat().st_ino,
                                'state':session.read_bytes(), 'library_inode':(place / 'Library').stat().st_ino}
        return result

    def test_normal_app_startup_automatically_moves_legacy_data_without_an_external_step(self):
        before = self.legacy_libraries()
        library = self.sites()
        library.start()  # The only entry point used; never call the migration helper here.
        self.assertEqual(set(library.runtimes), set(before))
        for key, previous in before.items():
            runtime = library.runtime(key)
            expected = self.root / sites.named(key).name / 'Library'
            self.assertEqual(runtime.folder, expected)
            self.assertEqual(runtime.registry.workspace_id, previous['workspace_id'])
            self.assertEqual(expected.stat().st_ino, previous['library_inode'])
            self.assertEqual((expected / 'Series/episode.cbz').stat().st_ino, previous['inode'])
            self.assertEqual((expected / 'Series/episode.cbz').read_bytes(), b'original archive')
            self.assertEqual(runtime.state.read_bytes(), previous['state'])
        self.assertFalse((self.root / 'Library').exists())
        self.assertFalse((self.root / '.comic-session').exists())
        self.background['start'].assert_not_called()
        library.stop(0)
        again = self.sites(); again.start()
        self.assertEqual(again.runtime('tapas').registry.workspace_id, before['tapas']['workspace_id'])
        self.assertEqual(again.runtime('tapas').folder.stat().st_ino, before['tapas']['library_inode'])

    def test_normal_app_startup_resumes_an_interrupted_move_on_the_next_launch(self):
        from comic_archive import site_layout
        before = self.legacy_libraries()
        rename = os.rename
        def interrupted(source, destination):
            if Path(source) == self.root / '.comic-session':
                raise OSError('interrupted')
            return rename(source, destination)
        first = self.sites()
        with patch('comic_archive.site_layout.os.rename', side_effect=interrupted), self.assertRaises(ArchiveError):
            first.start()
        self.assertFalse(first.runtimes)
        self.assertTrue((self.root / '.comic-shared' / site_layout.PENDING).is_file())
        again = self.sites(); again.start()  # Restart alone must finish; no external prepare call.
        self.assertEqual(set(again.runtimes), set(before))
        for key, previous in before.items():
            runtime = again.runtime(key)
            self.assertEqual(runtime.registry.workspace_id, previous['workspace_id'])
            self.assertEqual((runtime.folder / 'Series/episode.cbz').stat().st_ino, previous['inode'])
            self.assertEqual(runtime.state.read_bytes(), previous['state'])
        self.assertFalse((self.root / '.comic-shared' / site_layout.PENDING).exists())
        self.background['start'].assert_not_called()

    def test_default_library_is_one_fixed_folder_in_the_real_home_never_the_working_directory(self):
        self.assertEqual(app_runtime.default_root(), self.home / 'Comic Archiver')
        real = self.home / 'real'; real.mkdir()
        link = self.home / 'link'; link.symlink_to(real, target_is_directory=True)
        with patch.dict(os.environ, home_environment(link)):
            root = app_runtime.default_root()
            self.assertEqual(root, real / 'Comic Archiver')
            self.assertNotIn(Path.cwd(), (root, root.parent))
            runtime = app_runtime.Runtime(root); self.addCleanup(runtime.stop, 0)
            runtime.start()
        self.assertTrue((real / 'Comic Archiver/Tapas/Library').is_dir())

    def test_the_apps_private_folders_beside_and_inside_the_library_are_hidden_from_the_file_manager(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        library = self.sites()
        with patch('comic_archive.osdep.hide') as hide, patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            library.start()
            self.assertIsNone(library.change_site(example))
        hidden = {call.args[0] for call in hide.call_args_list}
        for place in (self.root / 'Tapas', self.root / 'Example'):
            for name in ('.comic-session', '.comic-web', 'Library/.archive-state'):
                self.assertIn(place / name, hidden)
                self.assertEqual(stat.S_IMODE((place / name).stat().st_mode), PRIVATE_FOLDER)

    def test_the_open_library_folder_is_never_made_again_once_it_is_renamed(self):
        self.runtime.start()
        self.assertFalse(self.runtime.vanished())
        self.root.rename(self.home / 'Renamed')
        self.assertTrue(self.runtime.vanished())
        with self.assertRaises(LibraryMoved):
            archive_paths.directory(self.root / 'Tapas/Library/Series')
        # Nor while its downloader is stopped.
        self.runtime.stop(0)
        self.assertFalse(self.root.exists())

    def test_a_library_folder_renamed_while_closed_is_not_made_again_but_a_new_one_is_made_as_ever(self):
        self.runtime.start(); self.runtime.stop(0)
        self.root.rename(self.home / 'Renamed')
        self.assertTrue(app_runtime.vanished(self.root))
        again = app_runtime.Runtime(self.root); self.addCleanup(again.stop, 0)
        with self.assertRaises(LibraryMoved) as caught:
            again.start()
        self.assertEqual(str(caught.exception), app_runtime.GONE.format(folder=self.root))
        self.assertFalse(self.root.exists())
        # Opened where it is now, it is the same library.
        moved = app_runtime.Runtime(self.home / 'Renamed'); self.addCleanup(moved.stop, 0)
        moved.start()
        self.assertEqual(self.library(moved), self.home / 'Renamed/Tapas/Library')
        # A folder never opened before is made, as on a first start.
        self.assertFalse(app_runtime.vanished(self.home / 'New'))
        fresh = app_runtime.Runtime(self.home / 'New'); self.addCleanup(fresh.stop, 0)
        fresh.start()
        self.assertTrue((self.home / 'New/Tapas/Library').is_dir())

    def test_a_picked_folder_becomes_a_library_folder_without_mixing_into_other_files(self):
        pick = app_runtime.library_root
        empty, photos, named = self.home / 'empty', self.home / 'photos', self.home / 'drive/Comic Archiver'
        empty.mkdir(); photos.mkdir(); (photos / 'holiday.jpg').write_text('x'); named.mkdir(parents=True); (named / 'notes.txt').write_text('x')
        existing = self.home / 'comics'; (existing / 'Library/.archive-state').mkdir(parents=True)
        plain = self.home / 'books/Library'; plain.mkdir(parents=True); (plain / 'novel.epub').write_text('x')
        self.assertEqual((app_runtime.is_library(existing), app_runtime.is_library(plain.parent), app_runtime.present(existing),
                          app_runtime.present(self.home / 'gone'), app_runtime.present(Path('/media/\0'))), (True, False, True, False, False))
        link = self.home / 'link'; link.symlink_to(photos, target_is_directory=True)
        for chosen, root in [(empty, empty), (self.home / 'not-there-yet', self.home / 'not-there-yet'),
                             (photos, photos / 'Comic Archiver'), (named, named),
                             (existing, existing), (existing / 'Library', existing),  # The folder of saved files itself.
                             (plain, plain / 'Comic Archiver'),  # Somebody else's folder that happens to be called Library,
                             (plain.parent, plain.parent / 'Comic Archiver'),  # and the folder that folder is in.
                             (link, photos / 'Comic Archiver')]:  # Storage refuses symlinked parents.
            self.assertEqual(pick(str(chosen)), root)
        with patch.object(Path, 'resolve', side_effect=RuntimeError('loop')):
            self.assertEqual(pick(str(photos)), photos)  # Left for start() to refuse in its own words.
        with patch.object(Path, 'iterdir', side_effect=PermissionError('PRIVATE')):
            self.assertEqual(pick(str(empty)), empty / 'Comic Archiver')

    def test_opening_a_library_folder_notes_it_so_uninstalling_finds_its_sign_ins(self):
        order = Mock(side_effect=lambda: order.noted.append(list(app_runtime.opened_libraries())))
        order.noted = []
        with patch('comic_archive.app_runtime.cloud_runtime.note_connections', order):
            self.runtime.start()
        self.assertEqual(app_runtime.opened_libraries(), [self.root])
        # The cloud connections in use are noted first: none this folder brings counts as one in use before.
        self.assertEqual(order.noted, [[]])
        with patch('comic_archive.app_runtime.cloud_runtime.note_connections', side_effect=ArchiveError('PRIVATE')), \
                self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            other = app_runtime.Runtime(self.home / 'Other'); self.addCleanup(other.stop, 0)
            other.start()
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.assertEqual(app_runtime.opened_libraries(), [self.root, self.home / 'Other'])

    def test_the_chosen_library_folder_is_kept_for_the_next_start_and_the_standard_one_needs_no_record(self):
        saved, drive = self.state / 'comic-archiver/library', self.home / 'drive/Comic Archiver'
        self.assertIsNone(app_runtime.saved_root())
        app_runtime.remember_root(drive)
        self.assertEqual((app_runtime.saved_root(), saved.read_text()), (drive, f'{drive}\n'))
        self.assertEqual(sorted(path.name for path in saved.parent.iterdir()), ['library'])
        for text in ('', 'relative/folder\n', '\n', '/media/dri\0ve\n'):
            saved.write_text(text)
            self.assertIsNone(app_runtime.saved_root())
        app_runtime.remember_root(drive)
        for root in (None, app_runtime.default_root()):
            saved.write_text(f'{drive}\n')
            app_runtime.remember_root(root)
            self.assertFalse(saved.exists())
        app_runtime.remember_root(None)  # Nothing to forget.
        with patch('comic_archive.app_runtime.state_dir', side_effect=ArchiveError('blocked')):
            app_runtime.remember_root(drive)
            self.assertIsNone(app_runtime.saved_root())
        self.assertFalse(saved.exists())

    def test_the_window_is_told_the_folder_of_saved_episodes_it_opens_and_offers_to_change(self):
        self.assertIsNone(self.runtime.folder)
        self.runtime.start()
        self.assertEqual((self.runtime.folder, self.runtime.root, self.library(self.runtime)),
                         (self.root / 'Tapas/Library', self.root, self.root / 'Tapas/Library'))

    def test_changing_the_library_folder_restarts_there_keeps_the_sign_in_and_never_moves_saved_episodes(self):
        def signed_in(root, value):
            cookie = dict(name='session', value=value, domain='.tapas.io', path='/', expires=-1, httpOnly=True, secure=True, sameSite='Lax')
            (sites.folder(root, sites.DEFAULT) / '.comic-session/state.json').write_text(json.dumps({'cookies': [cookie], 'origins': []}))

        def cookie(root):
            return load_session(sites.folder(root, sites.DEFAULT) / '.comic-session')['cookies'][0]['value']
        library = self.sites()
        library.start()
        signed_in(self.root, 'PRIVATE_FIRST')
        (self.root / 'Tapas/Library/kept.cbz').write_text('saved')
        drive = self.home / 'drive'; drive.mkdir(); (drive / 'holiday.jpg').write_text('x')
        target = app_runtime.library_root(drive)
        fresh, problem = app_runtime.move(library, target); self.addCleanup(fresh.stop, 0)
        self.assertEqual((problem, fresh.root, fresh.theme, fresh.folder), (None, target, library.theme, target / 'Tapas/Library'))
        # The old library and downloader are closed before the new ones open.
        self.assertEqual(library.stop(), 'stopped_before')
        self.assertEqual(self.background['stop'].call_args.args[0], self.root / 'Tapas/.comic-session')
        self.assertEqual(self.library(fresh), target / 'Tapas/Library')
        # A folder with nothing in it starts signed in as before, privately, and its downloader starts.
        self.assertEqual((cookie(target), stat.S_IMODE((target / 'Tapas/.comic-session/state.json').stat().st_mode)), ('PRIVATE_FIRST', PRIVATE_FILE))
        self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [target / 'Tapas/.comic-session'])
        self.assertEqual(((self.root / 'Tapas/Library/kept.cbz').read_text(), (target / 'Tapas/Library/kept.cbz').exists()), ('saved', False))
        self.assertEqual((app_runtime.saved_root(), cookie(self.root)), (target, 'PRIVATE_FIRST'))
        # An existing library keeps its own sign-in; the standard folder needs no record.
        signed_in(target, 'PRIVATE_SECOND')
        again, problem = app_runtime.move(fresh, self.root); self.addCleanup(again.stop, 0)
        self.assertEqual((problem, again.root, cookie(self.root), app_runtime.saved_root()), (None, self.root, 'PRIVATE_FIRST', None))
        # An existing library without a sign-in is not handed somebody's: it may belong to another account.
        other = self.home / 'other'; (other / 'Tapas/Library').mkdir(parents=True)
        third, problem = app_runtime.move(again, other); self.addCleanup(third.stop, 0)
        self.assertEqual((problem, (other / 'Tapas/.comic-session/state.json').exists(), self.background['start'].call_count), (None, False, 2))
        # A sign-in that cannot be read is left behind rather than stopping the change.
        (other / 'Tapas/.comic-session/state.json').write_text('PRIVATE nonsense')
        empty = self.home / 'empty'
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            fourth, problem = app_runtime.move(third, empty); self.addCleanup(fourth.stop, 0)
        self.assertEqual((problem, fourth.root, (empty / 'Tapas/.comic-session/state.json').exists()), (None, empty, False))
        self.assertNotIn('PRIVATE', ''.join(logs.output))

    def test_a_folder_that_cannot_be_used_leaves_the_app_on_the_old_one_with_the_reason(self):
        library = self.sites()
        library.start()
        self.sign_in()
        blocked = self.home / 'blocked'; blocked.write_text('a file in the way')
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            back, problem = app_runtime.move(library, blocked / 'Comic Archiver'); self.addCleanup(back.stop, 0)
        self.assertEqual((problem, back.root, back.theme), (app_runtime.UNWRITABLE, self.root, library.theme))
        self.assertNotIn(str(self.home), ''.join(logs.output))
        self.assertIsNot(back, library)
        self.assertEqual((self.library(back), app_runtime.saved_root()), (self.root / 'Tapas/Library', None))
        self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [self.root / 'Tapas/.comic-session'])
        link = self.home / 'link'; link.symlink_to(self.home / 'elsewhere', target_is_directory=True)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            again, problem = app_runtime.move(back, link / 'Comic Archiver'); self.addCleanup(again.stop, 0)
        self.assertIn('shortcut (symlink)', problem); self.assertEqual(again.root, self.root)

    def test_a_sites_records_for_support_come_from_its_own_library_with_its_folder_to_leave_out(self):
        self.runtime.start()
        worker = self.root / 'Tapas/.comic-session/worker'; worker.mkdir(parents=True, exist_ok=True)
        (worker / 'background.log').write_text('Task job-1 completed.\n')
        text, folder = Actions(lambda: self.runtime).support_records()
        self.assertTrue(text.startswith('=== Tapas: episodes not saved\n'))
        self.assertTrue(text.endswith('=== Tapas: the downloader’s log\nTask job-1 completed.'), text)
        self.assertEqual(folder, str(self.root))
        with patch.object(self.runtime.registry, 'resolve', side_effect=ArchiveError('PRIVATE')):
            self.assertEqual(Actions(lambda: self.runtime).support_records(),
                             ('=== Tapas: its library cannot be read now', str(self.root)))

    def test_each_site_has_its_own_folder_sign_in_and_downloader_and_every_site_runs_at_once(self):
        example, saved = replace(sites.DEFAULT, key='example', name='Example', available=True), self.state / 'comic-archiver/site'
        third = replace(sites.DEFAULT, key='third', name='Third', available=True)
        library = self.sites()

        def context(key):
            value = Actions(lambda: library.runtime(key)).context()
            return [site['key'] for site in value['sites'] if site['current']], value['workspace_id']
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example, third,
                                                 replace(sites.DEFAULT, key='later', name='Later', available=False))):
            library.start(); self.sign_in()
            (self.root / 'Tapas/Library/kept.cbz').write_text('saved')
            # A site never used makes no folder of its own.
            self.assertEqual((list(library.runtimes), (self.root / 'Example').exists()), (['tapas'], False))
            self.assertEqual(library.start_worker(), 'running')
            tapas = context('tapas')[1]
            self.assertEqual(context('tapas')[0], ['tapas'])
            self.assertIsNone(library.change_site(example))
            place = self.root / 'Example'
            self.assertEqual((library.site, library.root, library.folder, library.session),
                             (example, self.root, place / 'Library', place / '.comic-session'))
            self.assertEqual(list(library.runtimes), ['tapas', 'example'])
            # Each site's calls read its own library.
            self.assertEqual(context('example')[0], ['example'])
            self.assertNotEqual(context('example')[1], tapas)
            self.assertEqual(self.library(library.runtime('example')), place / 'Library')
            # Tapas's downloader goes on; one site's sign-in is never handed to another, so the new one waits for its own.
            self.background['stop'].assert_not_called()
            self.assertFalse((place / '.comic-session/state.json').exists())
            self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [self.root / 'Tapas/.comic-session'])
            self.assertEqual((app_runtime.saved_site(), saved.read_text()), (example, 'example\n'))
            # Back to Tapas: as it was, nothing opened or stopped again; no record is needed.
            self.assertIsNone(library.change_site(sites.DEFAULT))
            self.assertEqual((library.session, (self.root / 'Tapas/Library/kept.cbz').read_text(), library.runtime('example').site),
                             (self.root / 'Tapas/.comic-session', 'saved', example))
            self.assertEqual((self.background['start'].call_count, self.background['stop'].call_count), (1, 0))
            self.assertEqual((saved.exists(), app_runtime.saved_site()), (False, sites.DEFAULT))
            # The next start opens every site this folder has, the chosen one first; closing stops them all.
            self.assertEqual(library.stop(0), 'stopped')
            self.assertEqual(self.background['stop'].call_count, 2)
            again = self.sites()
            again.start()
            self.assertEqual(list(again.runtimes), ['tapas', 'example'])
            # Another library folder keeps the chosen site, and each site open there opens in the new one too.
            self.assertIsNone(again.change_site(example))
            drive = self.home / 'drive'
            with self.assertLogs('comic_archive.desktop', 'WARNING'):  # The made-up sign-in above cannot be carried.
                moved, problem = app_runtime.move(again, drive)
            self.addCleanup(moved.stop, 0)
            self.assertEqual((problem, moved.site, moved.folder), (None, example, drive / 'Example/Library'))
            self.assertEqual(sorted(moved.runtimes), ['example', 'tapas'])
            self.assertTrue(app_runtime.is_library(drive))  # A folder only another site has saved into is a library too.
            # A site that can no longer be chosen, or nonsense, counts as none.
            for text in ('later\n', 'PRIVATE nonsense', ''):
                saved.write_text(text)
                self.assertEqual(app_runtime.saved_site(), sites.DEFAULT)
            # A site whose folder cannot be made leaves the chosen site as it was, every downloader untouched, with the reason.
            self.assertIsNone(moved.change_site(sites.DEFAULT))
            (drive / 'Third').write_text('a file in the way')
            stops = self.background['stop'].call_count
            with self.assertLogs('comic_archive.desktop', 'WARNING'):
                problem = moved.change_site(third)
            self.assertEqual((problem, moved.site, saved.exists(), self.background['stop'].call_count),
                             (app_runtime.UNWRITABLE, sites.DEFAULT, False, stops))
            self.assertEqual((sorted(moved.runtimes), self.library(moved)), (['example', 'tapas'], drive / 'Tapas/Library'))

    def test_every_sites_library_is_linked_to_the_shared_settings_of_the_library_folder_as_it_is_opened(self):
        from comic_archive import shared
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        store = self.root / '.comic-shared'
        library = self.sites()
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)), patch('comic_archive.osdep.hide') as hide:
            library.start()
            self.assertIsNone(library.change_site(example))
            self.assertEqual(library.site, example)
            self.assertEqual([shared.folder(place / 'Library') for place in (self.root / 'Tapas', self.root / 'Example')], [store, store])
        self.assertIn(store, {call.args[0] for call in hide.call_args_list})
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), PRIVATE_FOLDER)

    def test_a_library_that_cannot_be_linked_still_opens_with_its_own_settings_and_the_log_names_no_private_text(self):
        for error in (ArchiveError('PRIVATE /home/someone'), PermissionError('PRIVATE /home/someone')):
            with self.subTest(error=type(error).__name__):
                runtime = app_runtime.Runtime(self.root); self.addCleanup(runtime.stop, 0)
                with patch('comic_archive.shared.link', side_effect=error), self.assertLogs('comic_archive.desktop', 'WARNING') as logged:
                    runtime.start()
                self.assertEqual(self.library(runtime), self.root / 'Tapas/Library')
                self.assertNotIn('PRIVATE', '\n'.join(logged.output))
                runtime.stop(0)

    def test_the_theme_follows_the_computer_until_one_is_chosen_and_the_choice_outlives_the_app(self):
        theme, saved = self.runtime.theme, self.state / 'comic-archiver/theme'
        self.assertEqual((theme.get(), app_runtime.THEMES), ('system', ('system', 'light', 'dark')))
        self.runtime.start()
        self.assertIs(self.runtime.theme, theme)  # The window's header changes it.
        theme.set('dark')
        self.assertEqual((theme.get(), saved.read_text(), app_runtime.Theme().get()), ('dark', 'dark\n', 'dark'))
        self.assertEqual(sorted(path.name for path in saved.parent.iterdir() if path.name.startswith('theme')), ['theme'])
        with self.assertRaises(ArchiveError):
            theme.set('sepia')
        self.assertEqual((theme.get(), saved.read_text()), ('dark', 'dark\n'))
        saved.write_text('PRIVATE nonsense')
        self.assertEqual(app_runtime.Theme().get(), 'system')
        # A settings folder that cannot be used keeps the choice for this run only.
        with patch('comic_archive.app_runtime.state_dir', side_effect=ArchiveError('blocked')):
            fresh = app_runtime.Theme()
            self.assertEqual(fresh.get(), 'system')
            fresh.set('light')
            self.assertEqual(fresh.get(), 'light')
        self.assertEqual(saved.read_text(), 'PRIVATE nonsense')

    def test_start_opens_the_private_library_for_the_window_without_a_worker(self):
        self.assertIsNone(self.runtime.start())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), PRIVATE_FOLDER)
        registry = self.runtime.registry
        self.assertEqual((self.library(self.runtime), registry.ids(LOCAL_OWNER), registry.ids('someone-else')),
                         (self.root / 'Tapas/Library', [registry.workspace_id], []))
        self.assertEqual((self.runtime.root, self.runtime.site), (self.root, sites.DEFAULT))
        signin = registry.site_signin
        self.assertIsInstance(signin, WindowSignIn)  # No separate browser window: the app's own window signs in.
        self.assertIs(signin, self.runtime.signin)
        self.assertEqual((signin.browser, signin.workspace.session), (DESKTOP_BROWSER, self.root / 'Tapas/.comic-session'))
        self.assertEqual(signin.status(), {'status': 'idle'})
        self.assertEqual(self.runtime.start_worker(), 'signed_out')
        self.background['start'].assert_not_called()
        self.assertFalse(self.runtime.busy())

    def test_unusable_library_folder_or_library_fails_before_any_worker_and_leaves_nothing_running(self):
        link = self.home / 'link'; link.symlink_to(self.home, target_is_directory=True)
        blocked = self.home / 'file'; blocked.write_text('not a folder')
        for root in (link / 'Comic Archiver', blocked, self.home / 'missing/parent'):
            runtime = app_runtime.Runtime(root)
            with self.assertRaises(ArchiveError): runtime.start()
            self.assertEqual(runtime.start_worker(), 'signed_out')
            self.assertFalse(runtime.busy())
            self.assertEqual(runtime.stop(), 'not_started')
        self.assertEqual(blocked.read_text(), 'not a folder')
        with patch('comic_archive.app_runtime.LocalWorkspaceRegistry', side_effect=ArchiveError('Library key is invalid')):
            with self.assertRaises(ArchiveError): self.runtime.start()
        self.assertIsNone(self.runtime.registry)
        self.background['start'].assert_not_called(); self.background['stop'].assert_not_called()

    def test_a_start_that_fails_part_way_leaves_no_library_open_and_stop_still_works(self):
        with patch.object(app_runtime.Runtime, '_reopen', side_effect=RuntimeError('PRIVATE')):
            with self.assertRaises(RuntimeError): self.runtime.start()
        self.assertEqual((self.runtime.registry, self.runtime.signin), (None, None))
        # Closing the window afterwards neither fails nor asks a downloader that was never started.
        self.assertEqual(self.runtime.stop(), 'not_started')
        self.background['stop'].assert_not_called()
        # An open library has both its sign-ins closed with it.
        other = app_runtime.Runtime(self.root)
        other.registry = Mock()
        self.assertEqual(other.stop(0), 'stopped')
        other.registry.cloud_signin.close.assert_called_once_with(); other.registry.site_signin.close.assert_called_once_with()

    def test_saved_sign_in_starts_the_slow_built_in_engine_downloader_and_replaces_an_older_version(self):
        session = self.sign_in(); self.runtime.start()
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.background['start'].assert_called_once_with(session, DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL, restart=False)
        self.assertEqual((DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL), ('qt', 1.0))
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION - 1}
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.assertTrue(self.background['start'].call_args.kwargs['restart'])
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION}
        self.background['start'].return_value = {'running': True, 'already_running': True}
        self.assertEqual(self.runtime.start_worker(), 'running')
        self.assertFalse(self.background['start'].call_args.kwargs['restart'])
        self.assertIsNone(self.runtime._retry)

    def test_window_sign_in_is_the_librarys_own_and_shares_the_launch_lock_and_closing_flag(self):
        session = self.sign_in(); self.runtime.start()
        registry, signin = self.runtime.registry, self.runtime.signin
        self.assertIs(registry.site_signin, signin)
        self.assertEqual(signin.start(), {'status': 'running'})
        self.assertEqual(registry.site_signin_status(LOCAL_OWNER, registry.workspace_id), {'status': 'running'})
        self.assertEqual(self.runtime.busy(), 'signin')
        self.assertTrue(signin.take())
        cookies = [{'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': '.tapas.io', 'path': '/', 'expires': None,
                    'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}]
        locked = []
        self.background['start'].side_effect = lambda *args, **kwargs: locked.append(self.runtime._launch.locked()) or {'running': True}
        with patch('comic_archive.window_signin.NetworkGuard') as guard:
            self.assertEqual(signin.finish('11', cookies), {'status': 'connected'})
            guard.assert_called_once_with(self.root / 'Tapas/Library')
            self.assertEqual(locked, [True])  # The start-up launch and stop() wait on the same lock.
            self.background['stop'].assert_called_once_with(session, wait_seconds=35)
            self.background['start'].assert_called_once_with(session, DESKTOP_BROWSER, DESKTOP_REQUEST_INTERVAL)
            self.assertEqual([item['name'] for item in load_session(session)['cookies']], ['session'])
            self.assertIsNone(self.runtime.busy())
            self.assertEqual(signin.start(), {'status': 'running'})
            self.runtime.stop(0)
            self.assertEqual(signin.status(), {'status': 'cancelled', 'code': 'signin_incomplete'})
            # A sign-in that only now finds an account must not start a downloader behind the closed app.
            signin.start()
            self.assertEqual(signin.finish('11', cookies), {'status': 'cancelled', 'code': 'signin_incomplete'})
        self.background['start'].assert_called_once()

    def test_draining_or_launching_downloader_defers_then_starts_once_after_it_and_sign_in_have_gone(self):
        self.sign_in(); self.runtime.start()
        self.background['status'].reset_mock()  # Starting looks once, for a batch that closing interrupted.
        signin = self.runtime.registry.site_signin
        draining = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        gone = {'running': False}
        for first in ({'running': True, 'start_deferred': True, 'reason': 'worker_draining'}, LibraryBusy('PRIVATE')):
            self.background['start'].reset_mock()
            self.background['start'].side_effect = [first, {'running': True}]
            # First attempt, then: draining, legacy claim, sign-in running, free, and the retry's own check.
            self.background['status'].side_effect = [draining, draining, {'running': None}, gone, gone, gone]
            with patch.object(signin, 'status', side_effect=[{'status': 'running'}, {'status': 'idle'}]) as signing:
                self.assertEqual(self.runtime.start_worker(), 'deferred')
                self.runtime._retry.join(10)
                self.assertFalse(self.runtime._retry.is_alive())
                self.assertEqual(signing.call_count, 2)
            self.assertEqual(self.background['start'].call_count, 2)
            self.assertEqual(self.background['status'].call_count, 6)
            self.background['status'].reset_mock()

    def test_deferred_start_yields_to_a_sign_in_started_worker_unreadable_state_and_closing(self):
        self.sign_in(); self.runtime.start()
        current = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        self.background['start'].return_value = {'running': True, 'start_deferred': True}
        for later in ({'running': True, 'stop_requested': False}, ArchiveError('PRIVATE')):
            self.background['start'].reset_mock()
            self.background['status'].side_effect = [current, later]
            self.assertEqual(self.runtime.start_worker(), 'deferred')
            self.runtime._retry.join(10)
            self.assertFalse(self.runtime._retry.is_alive())
            self.background['start'].assert_called_once()
        self.background['start'].reset_mock()
        self.background['status'].side_effect = [current, OSError('PRIVATE'), {'running': False}, {'running': False}]
        self.assertEqual(self.runtime.start_worker(), 'deferred')
        self.runtime._retry.join(10)
        self.assertFalse(self.runtime._retry.is_alive())
        self.assertEqual(self.background['start'].call_count, 2)  # retried after the unreadable moment
        self.background['start'].reset_mock()
        self.background['status'].side_effect = None
        self.background['status'].return_value = current
        self.assertEqual(self.runtime.start_worker(), 'deferred')
        self.runtime.stop(0)
        self.runtime._retry.join(10)
        self.assertFalse(self.runtime._retry.is_alive())
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.background['start'].assert_called_once()

    def test_a_deferred_start_goes_on_when_another_site_is_chosen(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        library = self.sites()
        self.sign_in(); library.start()
        self.background['start'].return_value = {'running': True, 'start_deferred': True}
        self.background['status'].return_value = {'running': True, 'worker_version': WORKER_VERSION, 'stop_requested': True}
        self.assertEqual(library.start_worker(), 'deferred')
        retry = library.runtime()._retry
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            self.assertIsNone(library.change_site(example))
        # Tapas's wait goes on: once its old downloader has gone its own starts; the other site, not signed in, gets none.
        self.background['start'].return_value = {'running': True}
        self.background['status'].return_value = {'running': False}
        retry.join(10)
        self.assertFalse(retry.is_alive())
        self.assertEqual([call.args[0] for call in self.background['start'].call_args_list], [self.root / 'Tapas/.comic-session'] * 2)

    def test_downloader_start_failures_keep_the_library_open_and_log_no_private_text(self):
        log = app_runtime.configure_logging()
        self.sign_in(); self.runtime.start()
        for failure in (ArchiveError('PRIVATE_COOKIE'), OSError('/PRIVATE_PATH')):
            self.background['start'].side_effect = failure
            self.assertEqual(self.runtime.start_worker(), 'failed')
        self.background['start'].side_effect = None
        for result in ({'running': False, 'start_failed': True}, {'running': None, 'startup_pending': True}):
            self.background['start'].return_value = result
            self.assertEqual(self.runtime.start_worker(), 'failed')
        self.assertIsNone(self.runtime._retry)
        self.assertEqual(self.library(self.runtime), self.root / 'Tapas/Library')
        text = log.read_text()
        self.assertIn('ArchiveError', text); self.assertIn('OSError', text)
        self.assertNotIn('PRIVATE', text)

    def test_busy_names_a_download_worker_task_or_sign_in_and_never_guesses_from_unreadable_state(self):
        self.assertIsNone(self.runtime.busy())
        self.runtime.start()
        registry = self.runtime.registry
        self.assertIsNone(self.runtime.busy())
        with patch.object(registry.site_signin, 'status', return_value={'status': 'running'}):
            self.assertEqual(self.runtime.busy(), 'signin')
        with patch.object(registry, 'account_status', return_value={'status': 'running'}) as account:
            self.assertEqual(self.runtime.busy(), 'download')
            account.return_value = {'status': 'stopped'}
            self.assertIsNone(self.runtime.busy())
        self.background['status'].return_value = {'running': True, 'state': 'busy'}
        self.assertEqual(self.runtime.busy(), 'download')
        self.background['status'].return_value = {'running': True, 'state': 'ready'}
        self.assertIsNone(self.runtime.busy())
        log = app_runtime.configure_logging()
        for failure in (ArchiveError('PRIVATE'), OSError('PRIVATE')):
            self.background['status'].side_effect = failure
            self.assertIsNone(self.runtime.busy())
        self.assertEqual(log.read_text().count('download state is unreadable'), 2)
        self.assertNotIn('PRIVATE', log.read_text())

    def test_library_that_exists_but_cannot_be_used_gets_a_plain_message_and_a_class_only_log_entry(self):
        log = app_runtime.configure_logging()
        for failure in (PermissionError('/PRIVATE'), sqlite3.OperationalError('PRIVATE'), OSError(28, 'PRIVATE')):
            runtime = app_runtime.Runtime(self.root)
            with patch('comic_archive.app_runtime.LocalWorkspaceRegistry', side_effect=failure):
                with self.assertRaises(ArchiveError) as caught: runtime.start()
            self.assertEqual(str(caught.exception), app_runtime.UNWRITABLE)
            self.assertIsNone(runtime.registry)
        self.assertNotIn('PRIVATE', log.read_text()); self.assertIn('OperationalError', log.read_text())
        # The library's own reason, already in plain words, is passed on as it is.
        with patch('comic_archive.app_runtime.LocalWorkspaceRegistry', side_effect=ArchiveError('Library key is invalid; existing files are retained.')):
            with self.assertRaisesRegex(ArchiveError, 'Library key is invalid'): self.runtime.start()
        link = self.home / 'link'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ArchiveError) as caught: app_runtime.Runtime(link / 'Comic Archiver').start()
        self.assertIn('link', str(caught.exception)); self.assertIn('symlink', str(caught.exception))

    def test_a_library_whose_own_folders_lead_elsewhere_is_never_opened_but_one_reached_through_a_junction_is(self):
        outside = self.home / 'outside'
        for name in ('.comic-session/worker/requests', 'Webtoon', 'Library/.archive-state/cloud', '.comic-shared'):
            root = self.home / ('library-' + str(len(name)))
            folder = junction(self, root / name, outside / name)
            with self.assertRaises(ArchiveError) as caught:
                app_runtime.Library(root).start()
            self.assertEqual(str(caught.exception), app_runtime.REDIRECTED.format(root=root, folder=folder))
            self.assertEqual(list(folder.iterdir()), [])  # Nothing was made, read or written in it.
        # The library folder itself, and those around it, are the person's own: a drive mounted in a folder, say.
        mounted = junction(self, self.home / 'mounted', self.home / 'drive')
        runtime = app_runtime.Runtime(mounted / 'Comic Archiver'); self.addCleanup(runtime.stop, 0)
        runtime.start()
        self.assertEqual(runtime.folder, mounted / 'Comic Archiver/Tapas/Library')

    def test_a_provider_replaced_by_a_junction_after_preflight_is_refused_before_private_state_is_created(self):
        root = self.home / 'changed-during-opening'
        outside = self.home / 'outside-after-preflight'
        prepare = app_runtime.site_layout.prepare
        redirected = None
        def changed(folder):
            nonlocal redirected
            prepare(folder)
            redirected = junction(self, root / 'Tapas', outside)
        runtime = app_runtime.Runtime(root); self.addCleanup(runtime.stop, 0)
        with patch.object(app_runtime.site_layout, 'prepare', side_effect=changed):
            with self.assertRaises(ArchiveError) as caught:
                runtime.start()
        self.assertEqual(str(caught.exception), app_runtime.REDIRECTED.format(root=root, folder=redirected))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(runtime.registry)

    def test_downloader_that_finished_starting_after_the_window_closed_is_asked_to_stop_at_once(self):
        self.sign_in(); self.runtime.start()
        def late(*args, **kwargs):
            self.runtime._closing.set()  # stop() gave up waiting while this was still starting
            return {'running': True}
        self.background['start'].side_effect = late
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.background['stop'].assert_called_once_with(self.root / 'Tapas/.comic-session', wait_seconds=0)
        self.background['stop'].side_effect = OSError('PRIVATE')
        self.runtime._closing.clear()
        self.assertEqual(self.runtime.start_worker(), 'closing')
        self.assertEqual(self.background['stop'].call_count, 2)

    def test_stop_closes_the_sign_ins_then_interrupts_the_downloader_without_pausing_the_account_download(self):
        self.sign_in(); self.runtime.start()
        self.assertEqual(self.runtime.start_worker(), 'running')
        registry = self.runtime.registry
        seen, session = [], self.root / 'Tapas/.comic-session'
        def drain(session, *, wait_seconds):
            seen.append(('ask', session, wait_seconds))
            return {'running': True}
        def look(session):
            seen.append(('look', session))
            return {'running': len(seen) < 6}
        self.background['stop'].side_effect = drain; self.background['status'].side_effect = look
        with patch('comic_archive.download_control.request_stop') as pause, \
                patch.object(registry.site_signin, 'close', side_effect=lambda: seen.append('site sign-in closed')), \
                patch.object(registry.cloud_signin, 'close', side_effect=lambda: seen.append('cloud sign-in closed')), \
                patch('comic_archive.account_ui.interrupt', side_effect=lambda workspace: seen.append(workspace.library)), \
                patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            pause.assert_not_called(); end.assert_not_called()
        # Asked to leave before its batch is interrupted, so it goes without reading that batch's receipt.
        self.assertEqual(seen, ['cloud sign-in closed', 'site sign-in closed', ('ask', session, 0), self.root / 'Tapas/Library',
                                ('look', session), ('look', session)])
        self.assertEqual(self.runtime.stop(), 'stopped_before')
        self.assertEqual(self.background['stop'].call_count, 1)

    def test_a_downloader_that_is_not_running_is_only_asked_once_and_one_that_will_not_go_is_ended(self):
        self.runtime.start()
        with patch('comic_archive.account_ui.interrupt') as interrupt, patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            interrupt.assert_not_called(); end.assert_not_called()
        self.background['stop'].assert_called_once_with(self.root / 'Tapas/.comic-session', wait_seconds=0)
        for ended, outcome in ((True, 'stopped'), (False, 'finishing')):
            other = app_runtime.Runtime(self.home / outcome); self.addCleanup(other.stop, 0)
            other.start()
            alive = [True]
            def end(session, ended=ended, alive=alive):
                alive[0] = not ended
                return ended
            self.background['stop'].reset_mock(); self.background['stop'].return_value = {'running': True}
            self.background['status'].side_effect = lambda session, alive=alive: {'running': alive[0]}
            with patch('comic_archive.account_ui.interrupt', side_effect=OSError('PRIVATE')) as interrupt, \
                    patch('comic_archive.app_runtime.end_downloader', side_effect=end) as ending:
                self.assertEqual(other.stop(.2), outcome)
            interrupt.assert_called_once(); ending.assert_called_once_with(self.home / outcome / 'Tapas/.comic-session')
            self.background['stop'].assert_called_once_with(self.home / outcome / 'Tapas/.comic-session', wait_seconds=0)
            self.background['status'].side_effect = None

    def test_a_downloader_too_busy_to_be_asked_is_asked_again_and_one_that_cannot_be_asked_is_still_ended(self):
        self.runtime.start()
        self.background['stop'].side_effect = [LibraryBusy('PRIVATE'), LibraryBusy('PRIVATE'), {'running': False}]
        with patch('comic_archive.app_runtime.end_downloader') as end:
            self.assertEqual(self.runtime.stop(), 'stopped')
            end.assert_not_called()
        self.assertEqual(self.background['stop'].call_count, 3)
        for ended, outcome in ((True, 'stopped'), (False, 'failed')):
            other = app_runtime.Runtime(self.home / outcome); self.addCleanup(other.stop, 0)
            other.start()
            self.background['stop'].side_effect = LibraryBusy('PRIVATE')
            self.background['status'].return_value = {'running': not ended}
            with self.assertLogs(app_runtime.LOG, 'WARNING') as logs, \
                    patch('comic_archive.app_runtime.end_downloader', return_value=ended) as end:
                self.assertEqual(other.stop(.2), outcome)
            end.assert_called_once_with(self.home / outcome / 'Tapas/.comic-session')
            self.assertIn('did not accept the stop request: LibraryBusy', logs.output[0]); self.assertNotIn('PRIVATE', ''.join(logs.output))
            self.background['status'].return_value = {'running': False, 'state': 'stopped'}

    @unittest.skipUnless(os.name == 'posix', 'POSIX process signalling required')
    def test_only_the_running_downloader_of_this_library_is_ended(self):
        session = self.sign_in(); folder = session / 'worker'; folder.mkdir()
        def child(*words):
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', *words],
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            self.addCleanup(process.wait, 5); self.addCleanup(process.kill)
            return process
        mine = child('--session-dir', str(session), 'browser-worker')
        other = child('--session-dir', str(self.home / 'elsewhere/.comic-session'), 'browser-worker')
        def claims(pid):
            (folder / 'status.json').write_text(json.dumps({'pid': pid, 'state': 'busy'}))
        self.assertFalse(app_runtime.end_downloader(session))  # Nothing recorded.
        with patch('comic_archive.app_runtime.background.running', return_value=False):
            claims(mine.pid); self.assertFalse(app_runtime.end_downloader(session))
        with patch('comic_archive.app_runtime.background.running', return_value=True):
            for pid in (other.pid, 1, 'PRIVATE', 2 ** 22 + 7):
                claims(pid); self.assertFalse(app_runtime.end_downloader(session))
            self.assertIsNone(mine.poll()); self.assertIsNone(other.poll())
            claims(mine.pid); self.assertTrue(app_runtime.end_downloader(session))
        self.assertEqual(mine.wait(5), -15)
        self.assertIsNone(other.poll())

    def test_stop_waits_for_a_launching_downloader_only_within_its_budget_and_reports_failures(self):
        self.sign_in(); self.runtime.start()
        self.background['stop'].return_value = self.background['status'].return_value = {'running': True}
        with self.runtime._launch:
            self.assertEqual(self.runtime.stop(.05), 'finishing')
        self.assertEqual(self.background['stop'].call_args.kwargs, {'wait_seconds': 0})
        self.assertTrue(self.runtime._launch.acquire(blocking=False)); self.runtime._launch.release()
        for failure in (ArchiveError('PRIVATE'), OSError('PRIVATE')):
            other = app_runtime.Runtime(self.home / type(failure).__name__); self.addCleanup(other.stop, 0)
            other.start()
            self.background['stop'].side_effect = failure
            self.assertEqual(other.stop(2), 'failed')
            self.assertTrue(other._launch.acquire(blocking=False)); other._launch.release()
            self.assertEqual(self.background['stop'].call_args.kwargs, {'wait_seconds': 0})


    def test_a_folder_the_system_refuses_to_look_at_is_neither_a_library_nor_present(self):
        unreadable = self.home / 'unreadable'
        with patch.object(Path, 'is_dir', side_effect=PermissionError('PRIVATE')):  # What some Python versions do for a folder under an unreadable parent.
            self.assertFalse(app_runtime.is_library(unreadable))
            self.assertFalse(app_runtime.present(unreadable))

    def test_a_start_sets_the_saved_download_to_wait_for_go_and_a_failure_doing_so_does_not_keep_the_app_from_starting(self):
        with patch('comic_archive.account_ui.reopen', return_value=True) as reopen, self.assertLogs(app_runtime.LOG, 'INFO') as logs:
            self.runtime.start()
        self.assertEqual((reopen.call_args.args[0].library, reopen.call_args.kwargs), (self.root / 'Tapas/Library', {'idle': True}))
        self.assertIn('waits for Go', ''.join(logs.output))
        # A downloader that outlived the app, or one whose state cannot be told, may be on a task: none is ended for it.
        for number, running in enumerate((True, None)):
            other = app_runtime.Runtime(self.home / f'other-{number}'); self.addCleanup(other.stop, 0)
            self.background['status'].return_value = {'running': running}
            with patch('comic_archive.account_ui.reopen', return_value=False) as reopen:
                other.start()
            self.assertEqual(reopen.call_args.kwargs, {'idle': False})
        failing = app_runtime.Runtime(self.home / 'failing'); self.addCleanup(failing.stop, 0)
        with patch('comic_archive.account_ui.reopen', side_effect=sqlite3.OperationalError('PRIVATE')) as reopen, \
                self.assertLogs(app_runtime.LOG, 'WARNING') as logs:
            failing.start()
        reopen.assert_called_once()
        self.assertEqual(self.library(failing), self.home / 'failing/Tapas/Library')
        self.assertNotIn('PRIVATE', ''.join(logs.output))

    def test_a_downloader_that_cannot_be_ended_is_reported_as_failed_and_the_launch_lock_is_released(self):
        self.runtime.start()
        self.background['stop'].return_value = self.background['status'].return_value = {'running': True}
        with patch('comic_archive.account_ui.interrupt'), \
                patch('comic_archive.app_runtime.end_downloader', side_effect=OSError('PRIVATE')), \
                self.assertLogs(app_runtime.LOG, 'WARNING') as logs:
            self.assertEqual(self.runtime.stop(.05), 'failed')
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.assertTrue(self.runtime._launch.acquire(blocking=False)); self.runtime._launch.release()

    @unittest.skipUnless(os.name == 'posix', 'POSIX process signalling required')
    def test_a_downloader_in_the_process_group_of_the_app_is_ended_alone(self):
        session = self.sign_in(); folder = session / 'worker'; folder.mkdir()
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--session-dir', str(session), 'browser-worker'],
                                   stdin=subprocess.DEVNULL)  # No session of its own: ending its group would end this process too.
        self.addCleanup(process.wait, 5); self.addCleanup(process.kill)
        self.assertEqual(os.getpgid(process.pid), os.getpgid(0))
        (folder / 'status.json').write_text(json.dumps({'pid': process.pid, 'state': 'busy'}))
        with patch('comic_archive.app_runtime.background.running', return_value=True):
            self.assertTrue(app_runtime.end_downloader(session))
        self.assertEqual(process.wait(5), -15)

    def test_changing_to_an_empty_folder_from_a_library_never_signed_in_creates_no_sign_in(self):
        library = self.sites()
        library.start()
        empty = self.home / 'empty'
        fresh, problem = app_runtime.move(library, empty); self.addCleanup(fresh.stop, 0)
        self.assertEqual((problem, fresh.root, app_runtime.saved_root()), (None, empty, empty))
        self.assertFalse((empty / '.comic-session/state.json').exists())
        self.background['start'].assert_not_called()

    def test_state_folder_is_private_per_user_and_ignores_relative_overrides(self):
        self.assertEqual(app_runtime.state_dir(), self.state / 'comic-archiver')
        self.assertEqual(stat.S_IMODE((self.state / 'comic-archiver').stat().st_mode), PRIVATE_FOLDER)
        with patch.dict(os.environ, {STATE_HOME: 'relative/state'}):
            self.assertEqual(app_runtime.state_dir(), self.home / STATE_DEFAULT / 'comic-archiver')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.home / 'AppData/Local')}), patch('comic_archive.app_runtime.WINDOWS', True):
            self.assertEqual(app_runtime.state_dir(), self.home / 'AppData/Local/comic-archiver')
        (self.home / 'blocked').write_text('not a folder')
        with patch.dict(os.environ, {STATE_HOME: str(self.home / 'blocked')}):
            with self.assertRaises(ArchiveError): app_runtime.state_dir()
            self.assertIsNone(app_runtime.configure_logging())
            app_runtime.LOG.error('nowhere to write')

    def test_log_file_is_private_replaced_not_duplicated_and_never_follows_a_symlink(self):
        path = app_runtime.configure_logging()
        self.assertEqual(path, self.state / 'comic-archiver/desktop.log')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), PRIVATE_FILE)
        self.assertEqual(app_runtime.configure_logging(), path)
        handler, = app_runtime.LOG.handlers
        self.assertEqual((handler.maxBytes, handler.backupCount), (256 * 1024, 2))
        app_runtime.LOG.info('written once')
        self.assertEqual(path.read_text().count('written once'), 1)
        # The download's steps taken in the window go to the same log, and errors nothing caught and crashes too.
        self.assertEqual(logging.getLogger(support_log.DOWNLOAD_LOG).handlers, [handler])
        logging.getLogger(support_log.DOWNLOAD_LOG).info('download: stopped at lists -> running at lists (Go)')
        self.assertEqual(path.read_text().count('(Go)'), 1)
        support_log.catch_unexpected.assert_called_with('comic_archive.desktop')
        support_log.catch_crashes.assert_called_with(path.with_name('crash.log'))
        self.reset_logging(); path.unlink()
        path.symlink_to(self.home / 'elsewhere')
        self.assertIsNone(app_runtime.configure_logging())
        app_runtime.LOG.error('dropped')
        self.assertFalse((self.home / 'elsewhere').exists())

    def test_failure_descriptions_name_the_class_and_code_location_but_never_the_text(self):
        try:
            raise ArchiveError('PRIVATE_COOKIE https://tapas.io/?token=PRIVATE')
        except ArchiveError as error:
            text = app_runtime.describe(error)
        self.assertRegex(text, r'^ArchiveError at test_app_runtime\.py:[0-9]+$')
        self.assertEqual(app_runtime.describe(ValueError('PRIVATE')), 'ValueError')


if __name__ == '__main__':
    unittest.main()
