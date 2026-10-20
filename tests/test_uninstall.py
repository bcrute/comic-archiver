import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import home_environment
from comic_archive import app_runtime, sites, uninstall
from comic_archive.models import ArchiveError
from comic_archive.session import store_session


def sign_in(place, site=sites.DEFAULT):
    session = place / '.comic-session'
    store_session({'cookies': [{'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': site.cookie_domains[0]}],
                   'origins': []}, session, site)
    (session / 'state.tmp').write_text('PRIVATE_HALF_SAVED')
    return session


def connection(folder, name='connection-1.conf'):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text('[b2]\naccount = PRIVATE_KEY_ID\nkey = PRIVATE_KEY\n')
    (folder / 'targets.json').write_text(json.dumps({'schema_version': 1, 'library_id': 'kept',
                                                     'targets': {'b2': {'config': str(folder / name)}}}))


def episode(place):
    path = place / 'Library/Series/001.cbz'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'saved')
    return path


class UninstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / 'state'
        wrapper = patch.dict(os.environ, home_environment(self.home, self.state))
        wrapper.start(); self.addCleanup(wrapper.stop)
        for name in ('XDG_CACHE_HOME', 'XDG_DATA_HOME'):
            os.environ.pop(name, None)
        self.root = self.home / 'Comic Archiver'

    def test_every_sign_in_and_cloud_key_goes_from_every_library_folder_and_saved_episodes_stay(self):
        chosen, opened, never = self.home / 'drive/Comic Archiver', self.home / 'old/Comic Archiver', self.home / 'other'
        sessions = [sign_in(self.root), sign_in(self.root / 'Webtoon', sites.SITES[1]), sign_in(chosen / 'Tappytoon', sites.SITES[2]),
                    sign_in(opened)]
        untouched = sign_in(never)
        episodes = [episode(self.root), episode(self.root / 'Webtoon'), episode(opened)]
        own = self.root / 'Webtoon/Library/.archive-state/cloud'
        clouds = [self.root / '.comic-shared/cloud', own, chosen / '.comic-shared/cloud']
        for folder in clouds:
            connection(folder)
        (own / 'tests').mkdir(); (own / 'tests/run.json').write_text('{}')  # Records of cloud tests stay.
        app_runtime.remember_root(chosen)
        for root in (self.root, opened, self.root):
            app_runtime.remember_library(root)
        self.assertEqual(uninstall.libraries(), [self.root, chosen, opened])
        self.assertTrue(uninstall.forget_all())
        for session in sessions:
            self.assertEqual(sorted(path.name for path in session.iterdir()), ['site'])
        self.assertTrue((untouched / 'state.json').is_file())
        for folder in clouds:
            self.assertEqual([path.name for path in folder.iterdir() if path.suffix == '.conf'], [])
            self.assertEqual(json.loads((folder / 'targets.json').read_text()),
                             {'schema_version': 1, 'library_id': 'kept', 'targets': {}})
        self.assertTrue((own / 'tests/run.json').is_file())
        self.assertEqual([path.read_bytes() for path in episodes], [b'saved'] * 3)

    def test_a_library_never_signed_in_and_an_unreadable_connection_record_are_cleared_too(self):
        self.root.mkdir()
        cloud = self.root / '.comic-shared/cloud'
        connection(cloud, 'rclone.conf')
        (cloud / 'targets.json').write_text('not json')
        self.assertTrue(uninstall.forget_all())
        self.assertEqual(list(cloud.iterdir()), [])
        self.assertEqual(list(self.root.iterdir()), [self.root / '.comic-shared'])  # Nothing is made in its place.

    def test_a_library_linked_to_shared_settings_elsewhere_has_those_connections_forgotten(self):
        place = self.root / 'Webtoon'
        sign_in(place, sites.SITES[1])
        moved = self.home / 'moved/.comic-shared'
        connection(moved / 'cloud')
        link = place / 'Library/.archive-state/shared.json'
        link.parent.mkdir(parents=True)
        link.write_text(json.dumps({'schema_version': 1, 'folder': str(moved), 'cloud': 'shared'}))
        self.assertTrue(uninstall.forget_all())
        self.assertFalse((moved / 'cloud/connection-1.conf').exists())
        link.write_text('unreadable')
        self.assertTrue(uninstall.forget_all())  # Its own sign-in is still forgotten.

    @unittest.skipIf(os.name == 'nt', 'Making a symlink on Windows needs a privilege.')
    def test_a_symlinked_folder_is_never_followed(self):
        outside = self.home / 'outside'
        connection(outside)
        sign_in(outside / 'place')
        connection(outside / 'own')
        (self.root / 'Library/.archive-state').mkdir(parents=True)
        (self.root / 'Library/.archive-state/cloud').symlink_to(outside / 'own')
        (self.root / '.comic-shared').symlink_to(outside)
        (self.root / 'Webtoon').symlink_to(outside / 'place')
        self.assertTrue(uninstall.forget_all())
        self.assertTrue((outside / 'connection-1.conf').is_file())
        self.assertTrue((outside / 'own/connection-1.conf').is_file())
        self.assertTrue((outside / 'place/.comic-session/state.json').is_file())
        app_runtime.remember_library(outside / 'link')
        (outside / 'link').symlink_to(outside / 'place')
        (self.root / 'Webtoon').unlink()
        self.assertTrue(uninstall.forget_all())
        self.assertTrue((outside / 'place/.comic-session/state.json').is_file())

    def test_a_downloader_still_running_is_asked_to_stop_then_ended_and_the_sign_in_goes_regardless(self):
        session = sign_in(self.root)
        answers = {'running': True}
        status = Mock(side_effect=lambda folder: dict(answers))
        stop = Mock(side_effect=lambda folder: answers.update(running=False))
        with patch('comic_archive.uninstall.background.status', status), patch('comic_archive.uninstall.background.stop', stop):
            self.assertTrue(uninstall.forget(self.root))
            stop.assert_called_once_with(session)
            stop.side_effect, answers['running'] = None, True
            ended = Mock(side_effect=lambda folder: answers.update(running=None))  # A status file the lock no longer backs.
            with patch('comic_archive.uninstall.STOP_SECONDS', .2), patch('comic_archive.uninstall.app_runtime.end_downloader', ended):
                self.assertTrue(uninstall.forget(self.root))
                ended.assert_called_once_with(session)
                answers['running'], ended.side_effect = True, None
                stop.side_effect = ArchiveError('This older worker cannot stop through background controls.')
                with patch('comic_archive.uninstall.app_runtime.END_WAIT_SECONDS', .2), self.assertLogs('comic_archive.desktop', 'WARNING'):
                    self.assertFalse(uninstall.forget(self.root))
                self.assertEqual(ended.call_count, 2)
            status.side_effect = ArchiveError('Worker paths must not be symlinked.')
            sign_in(self.root)
            with self.assertLogs('comic_archive.desktop', 'WARNING'):
                self.assertFalse(uninstall.forget_all())
        self.assertFalse((session / 'state.json').exists())

    def test_a_library_folder_that_cannot_be_read_is_reported_and_the_others_are_still_cleared(self):
        session = sign_in(self.root)
        with patch('comic_archive.uninstall.libraries', return_value=[self.home / 'broken', self.root]), \
                patch('comic_archive.uninstall.forget', side_effect=[OSError('PRIVATE'), True]) as forget, \
                self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.assertFalse(uninstall.forget_all())
        self.assertEqual(forget.call_count, 2); self.assertNotIn('PRIVATE', logs.output[0])
        self.assertTrue((session / 'state.json').is_file())

    def test_the_apps_own_folders_go_and_nothing_beside_them(self):
        folders = uninstall.app_folders()
        if os.name == 'nt':
            self.assertEqual(folders, [self.state / 'comic-archiver', self.state / 'Comic Archiver'])
        else:
            self.assertEqual(folders, [self.state / 'comic-archiver', self.home / '.local/share/comic-archive',
                                       self.home / '.cache/Comic Archiver', self.home / '.local/share/Comic Archiver'])
            with patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.home / 'c'), 'XDG_DATA_HOME': str(self.home / 'd'),
                                         'XDG_STATE_HOME': 'relative'}):
                self.assertEqual(uninstall.app_folders()[::2], [self.home / '.local/state/comic-archiver', self.home / 'c/Comic Archiver'])
                self.assertEqual(uninstall.app_folders()[3], self.home / 'd/Comic Archiver')
        for folder in folders:
            (folder / 'deep').mkdir(parents=True)
            (folder / 'deep/file').write_text('x')
        (self.state / 'neighbour').write_text('kept')
        self.assertTrue(uninstall.remove_app_folders())
        self.assertFalse(any(folder.exists() for folder in folders))
        self.assertEqual((self.state / 'neighbour').read_text(), 'kept')
        self.assertTrue(uninstall.remove_app_folders())  # Already gone.
        folders[0].mkdir()
        with patch('comic_archive.uninstall.shutil.rmtree'):
            self.assertFalse(uninstall.remove_app_folders())

    @unittest.skipIf(os.name == 'nt', 'Making a symlink on Windows needs a privilege.')
    def test_a_symlinked_app_folder_is_removed_as_a_link_only(self):
        outside = self.home / 'outside'; outside.mkdir(); (outside / 'file').write_text('kept')
        self.state.mkdir()
        (self.state / 'comic-archiver').symlink_to(outside)
        self.assertTrue(uninstall.remove_app_folders())
        self.assertFalse((self.state / 'comic-archiver').is_symlink())
        self.assertEqual((outside / 'file').read_text(), 'kept')

    def test_the_linux_menu_entry_goes_only_when_it_starts_this_copy(self):
        app, other = self.home / 'App', self.home / 'Other'
        entry = self.home / '.local/share/applications/comic-archiver.desktop'
        entry.parent.mkdir(parents=True)
        entry.write_text(f'[Desktop Entry]\nExec="{other}/comic-archiver"\n')
        uninstall.remove_menu_entry(app)
        self.assertTrue(entry.exists())
        entry.write_text(f'[Desktop Entry]\nExec="{app}/comic-archiver"\n')
        uninstall.remove_menu_entry(app)
        self.assertFalse(entry.exists())
        uninstall.remove_menu_entry(app)  # Already gone.
        moved = self.home / 'data/applications/comic-archiver.desktop'
        moved.parent.mkdir(parents=True)
        moved.write_text(f'Exec="{app}/comic-archiver"\n')
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.home / 'data')}):
            uninstall.remove_menu_entry(app)
        self.assertFalse(moved.exists())

    def test_the_unpacked_linux_app_is_found_beside_its_python_and_windows_has_none(self):
        app = self.home / 'App'
        (app / 'python').mkdir(parents=True)
        (app / 'comic-archiver').write_text('launcher')
        with patch.object(uninstall.sys, 'prefix', str(app / 'python')), patch('comic_archive.uninstall.WINDOWS', False):
            self.assertIsNone(uninstall.app_folder())  # Run from its source: no VERSION.
            (app / 'VERSION').write_text('0.2.2\n')
            self.assertEqual(uninstall.app_folder(), app)
            (app / 'comic-archiver').unlink()
            self.assertIsNone(uninstall.app_folder())
            (app / 'comic-archiver').write_text('launcher')
            with patch('comic_archive.uninstall.WINDOWS', True):
                self.assertIsNone(uninstall.app_folder())


def app_copy(folder, version, launcher='comic-archiver', installed=False):
    (folder / 'python').mkdir(parents=True)
    (folder / launcher).write_text('launcher')
    (folder / 'VERSION').write_text(version + '\n')
    if installed:
        (folder / 'unins000.exe').write_bytes(b'uninstaller')
    return folder


class OlderCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.record = self.home / 'state/comic-archiver/app'
        self.here = app_copy(self.home / 'new', '0.2.10')
        for wrapper in (patch.dict(os.environ, home_environment(self.home, self.home / 'state')),
                        patch.object(uninstall.sys, 'prefix', str(self.here / 'python'))):
            wrapper.start(); self.addCleanup(wrapper.stop)
        os.environ.pop(uninstall.PREVIOUS, None)

    def previous(self, folder):
        return patch.dict(os.environ, {uninstall.PREVIOUS: str(folder)})

    def test_versions_are_compared_as_numbers_and_anything_else_is_no_version(self):
        self.assertEqual(uninstall.copy_version(self.here), (0, 2, 10))
        self.assertGreater(uninstall.copy_version(self.here), uninstall.copy_version(app_copy(self.home / 'a', '0.2.9')))
        for text in ('', 'abc', '1.2.3.4.5', '1..2', '0.2.1-beta', '1234567'):
            (self.here / 'VERSION').write_text(text)
            self.assertIsNone(uninstall.copy_version(self.here))
        self.assertIsNone(uninstall.copy_version(self.home / 'missing'))

    def test_the_copy_the_menu_started_before_and_the_one_noted_last_are_offered_when_not_newer(self):
        old, same, windows = app_copy(self.home / 'old', '0.2.1'), app_copy(self.home / 'same', '0.2.10'), \
            app_copy(self.home / 'zip', '0.1.9', 'Comic Archiver.cmd')
        self.assertEqual(uninstall.older_copies(), [])
        self.assertEqual(self.record.read_text(), f'{self.here}\n')
        with self.previous(old):
            self.assertEqual(uninstall.older_copies(), [old])
        for folder in (same, windows):
            self.record.write_text(f'{folder}\n')
            self.assertEqual(uninstall.older_copies(), [folder])
            self.assertEqual(self.record.read_text(), f'{self.here}\n')  # Asked once: this copy is noted now.
        self.record.write_text(f'{old}\n')
        with self.previous(old):
            self.assertEqual(uninstall.older_copies(), [old])
        self.record.write_text(f'{windows}\n')
        with self.previous(old):
            self.assertEqual(uninstall.older_copies(), [old, windows])

    def test_a_newer_copy_is_never_offered_and_stays_the_one_noted(self):
        newer = app_copy(self.home / 'newer', '0.3.0')
        self.record.parent.mkdir(parents=True); self.record.write_text(f'{newer}\n')
        with self.previous(newer):
            self.assertEqual(uninstall.older_copies(), [])
        self.assertEqual(self.record.read_text(), f'{newer}\n')
        self.assertTrue((newer / 'VERSION').exists())

    def test_an_installed_copy_one_holding_a_library_or_this_one_is_never_offered(self):
        installed = app_copy(self.home / 'installed', '0.2.1', 'Comic Archiver.cmd', installed=True)
        holding, around = app_copy(self.home / 'holding', '0.2.1'), app_copy(self.home / 'around', '0.2.1')
        inner = app_copy(self.here / 'inner', '0.2.1')
        app_runtime.remember_library(holding / 'Comic Archiver')
        for folder in (installed, holding, inner, self.here, self.home / 'gone', self.home / 'notacopy'):
            with self.previous(folder):
                self.assertEqual(uninstall.older_copies(), [], folder)
        self.record.unlink()  # This copy, noted by now: the copy around 'here' below may be offered nothing else.
        with patch.object(uninstall.sys, 'prefix', str(around / 'here/python')):
            app_copy(around / 'here', '0.2.10')
            with self.previous(around):
                self.assertEqual(uninstall.older_copies(), [])
        self.record.unlink()
        (self.home / 'notacopy').mkdir()
        app_copy(self.home / 'relative', '0.2.1')
        folder = os.getcwd(); os.chdir(self.home); self.addCleanup(os.chdir, folder)
        for text in ('relative', ''):
            with patch.dict(os.environ, {uninstall.PREVIOUS: text}):
                self.assertEqual(uninstall.older_copies(), [])

    @unittest.skipIf(os.name == 'nt', 'Making a symlink on Windows needs a privilege.')
    def test_a_symlink_to_a_copy_is_never_offered(self):
        old = app_copy(self.home / 'old', '0.2.1')
        (self.home / 'link').symlink_to(old)
        with self.previous(self.home / 'link'):
            self.assertEqual(uninstall.older_copies(), [])

    def test_running_from_the_source_offers_nothing_and_notes_nothing(self):
        (self.here / 'VERSION').unlink()
        with self.previous(app_copy(self.home / 'old', '0.2.1')):
            self.assertEqual(uninstall.older_copies(), [])
        self.assertFalse(self.record.exists())

    def test_an_older_copy_is_deleted_whole(self):
        old = app_copy(self.home / 'old', '0.2.1')
        (old / 'python/lib').mkdir(); (old / 'python/lib/module.py').write_text('x')
        self.assertTrue(uninstall.remove_copy(old))
        self.assertFalse(old.exists())
        old = app_copy(self.home / 'old', '0.2.1')
        with patch('comic_archive.uninstall.shutil.rmtree'):
            self.assertFalse(uninstall.remove_copy(old))


class LibraryRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        wrapper = patch.dict(os.environ, home_environment(self.home, self.home / 'state'))
        wrapper.start(); self.addCleanup(wrapper.stop)

    def test_each_library_folder_opened_is_noted_once_and_a_bad_record_is_ignored(self):
        record = self.home / 'state/comic-archiver/libraries'
        self.assertEqual(app_runtime.opened_libraries(), [])
        first, second = self.home / 'a', self.home / 'b b'
        for root in (first, second, first):
            app_runtime.remember_library(root)
        self.assertEqual((app_runtime.opened_libraries(), record.read_text()), ([first, second], f'{first}\n{second}\n'))
        self.assertEqual(sorted(path.name for path in record.parent.iterdir()), ['libraries'])
        record.write_text(f'relative\n\n{first}\n/nul\0l\n')
        self.assertEqual(app_runtime.opened_libraries(), [first])
        with patch('comic_archive.app_runtime.state_dir', side_effect=ArchiveError('blocked')), \
                self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.assertEqual(app_runtime.opened_libraries(), [])
            app_runtime.remember_library(second)
        self.assertEqual(app_runtime.opened_libraries(), [first])


if __name__ == '__main__':
    unittest.main()
