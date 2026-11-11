"""Provider layout migrations rename directories and preserve admission and stored state."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from comic_archive import app_runtime, site_layout, sites
from comic_archive.errors import LibraryBusy, LibraryMoved
from comic_archive.locking import LibraryLock


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        # A real legacy directory structure, with opaque bytes standing for the index and sign-in.
        for name, filename in (('Library/.archive-state', 'inventory.sqlite3'),
                               ('.comic-session', 'state.json'), ('.comic-web', 'secret.key')):
            path = self.root / name; path.mkdir(parents=True)
            (path / filename).write_bytes(b'preserved')
        (self.root / 'Library/episode.cbz').write_bytes(b'archive')

    def test_each_site_uses_a_sibling_folder_and_legacy_files_are_renamed_in_place(self):
        identities = {name: (self.root / name).stat().st_ino for name in site_layout.ENTRIES}
        shared = self.root / '.comic-shared'; shared.mkdir(); (shared / 'kept').write_bytes(b'shared')
        other = self.root / 'Webtoon/Library'; other.mkdir(parents=True); (other / 'kept.cbz').write_bytes(b'other')
        site_layout.prepare(self.root)
        for name, inode in identities.items():
            self.assertFalse((self.root / name).exists())
            self.assertEqual((self.root / 'Tapas' / name).stat().st_ino, inode)
        self.assertEqual((self.root / 'Tapas/Library/episode.cbz').read_bytes(), b'archive')
        self.assertEqual((self.root / 'Tapas/.comic-session/state.json').read_bytes(), b'preserved')
        self.assertEqual((shared / 'kept').read_bytes(), b'shared')
        self.assertEqual((other / 'kept.cbz').read_bytes(), b'other')
        self.assertTrue(site_layout.known(self.root))
        self.assertFalse((shared / site_layout.PENDING).exists())
        for site in sites.SITES:
            self.assertEqual(sites.folder(self.root, site), self.root / site.name)
        with patch('comic_archive.site_layout.os.rename') as rename:
            site_layout.prepare(self.root)
        rename.assert_not_called()
        for old in (self.root / 'Library', self.root / '.comic-session/worker/.runtime'):
            with self.assertRaises(LibraryMoved), LibraryLock(old):
                pass
        from comic_archive.inventory import Inventory
        from comic_archive.local_library import LocalWorkspaceRegistry
        for open_old in (lambda: Inventory(self.root / 'Library'), lambda: LocalWorkspaceRegistry(self.root)):
            with self.assertRaises(LibraryMoved):
                open_old()
        self.assertFalse((self.root / 'Library').exists())
        self.assertFalse((self.root / '.comic-web').exists())
        for chosen in (self.root, self.root / 'Tapas', self.root / 'Tapas/Library', other, other.parent):
            self.assertEqual(app_runtime.library_root(chosen), self.root)

    def test_an_empty_normalized_root_is_still_the_selected_library(self):
        root = self.root / 'empty'; root.mkdir()
        site_layout.prepare(root)
        self.assertTrue(app_runtime.is_library(root))
        self.assertEqual(app_runtime.library_root(root), root)
        self.assertFalse((root / 'Library').exists())
        self.assertFalse((root / 'Tapas').exists())

    def test_an_active_worker_prevents_any_move(self):
        with patch('comic_archive.background.status', return_value={'running':True}), self.assertRaises(LibraryBusy):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
        self.assertFalse((self.root / 'Tapas').exists())

    def test_a_busy_writer_prevents_any_move(self):
        held, release = threading.Event(), threading.Event()
        def writer():
            with LibraryLock(self.root / 'Library'):
                held.set(); release.wait(5)
        thread = threading.Thread(target=writer); thread.start()
        try:
            self.assertTrue(held.wait(5))
            with self.assertRaises(LibraryBusy):
                site_layout.prepare(self.root)
            self.assertFalse((self.root / 'Tapas').exists())
        finally:
            release.set(); thread.join(5)

    def test_interrupted_move_resumes_and_blocks_new_writers_and_workers(self):
        rename = os.rename
        def interrupted(source, destination):
            if Path(source).name == '.comic-session':
                raise OSError('interrupted')
            rename(source, destination)
        with patch('comic_archive.site_layout.os.rename', side_effect=interrupted), self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Tapas/Library/episode.cbz').is_file())
        self.assertTrue((self.root / '.comic-session/state.json').is_file())
        for root in (self.root / 'Library', self.root / 'Tapas/Library',
                     self.root / '.comic-session/worker/.runtime', self.root / 'Tapas/.comic-session/worker/.runtime'):
            with self.assertRaises(LibraryBusy), LibraryLock(root):
                pass
        site_layout.prepare(self.root)
        self.assertTrue(site_layout.known(self.root))
        self.assertTrue((self.root / 'Tapas/.comic-session/state.json').is_file())
        self.assertFalse((self.root / '.comic-session').exists())
        with LibraryLock(self.root / 'Tapas/Library'):
            pass

    def test_an_interrupted_move_resumes_after_a_restart_gave_the_drive_another_number(self):
        with patch('comic_archive.site_layout.os.rename', side_effect=OSError('interrupted')), \
                self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        journal = self.root / '.comic-shared' / site_layout.PENDING
        record = json.loads(journal.read_text())
        restarted = {'schema_version': 1, 'target': [record['target'][0] + 7, record['target'][1]],
                     'entries': {name: [drive + 7, folder] for name, (drive, folder) in record['entries'].items()}}
        # Another folder standing where a recorded one was is still refused.
        journal.write_text(json.dumps(restarted | {'target': [restarted['target'][0], restarted['target'][1] + 1]}))
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
        journal.write_text(json.dumps(restarted))
        site_layout.prepare(self.root)
        self.assertTrue(site_layout.known(self.root))
        self.assertTrue((self.root / 'Tapas/Library/episode.cbz').is_file())
        self.assertFalse(journal.exists())

    def own_connection(self, library):
        """A library that keeps its own cloud connection, as an earlier version set it up."""
        cloud = library / '.archive-state/cloud'; cloud.mkdir(parents=True)
        (cloud / 'b2.conf').write_text('[b2]\ntype = b2\n'); (cloud / 'b2.conf').chmod(0o600)
        elsewhere = self.root / 'chosen.conf'; elsewhere.write_text('[b2]\n'); elsewhere.chmod(0o600)
        targets = {'b2': {'config': str(self.root / 'Library/.archive-state/cloud/b2.conf'), 'remote': 'b2'},
                   'chosen': {'config': str(elsewhere), 'remote': 'b2'}}
        (cloud / 'targets.json').write_text(json.dumps({'schema_version': 1, 'library_id': 'x', 'targets': targets}))
        (cloud / 'tests').mkdir()
        (cloud / 'tests/one.json').write_text(json.dumps({'state': 'uploaded', 'connection': targets['b2']}))
        return cloud, elsewhere

    def test_a_moved_librarys_own_cloud_connection_names_its_file_where_it_is_now(self):
        _, elsewhere = self.own_connection(self.root / 'Library')
        site_layout.prepare(self.root)
        cloud = self.root / 'Tapas/Library/.archive-state/cloud'
        targets = json.loads((cloud / 'targets.json').read_text())['targets']
        self.assertEqual(targets['b2']['config'], str(cloud / 'b2.conf'))
        self.assertEqual(targets['chosen']['config'], str(elsewhere))  # A file the person named stays as named.
        self.assertEqual(json.loads((cloud / 'tests/one.json').read_text())['connection']['config'], str(cloud / 'b2.conf'))

    def test_a_library_an_earlier_version_moved_has_its_cloud_connection_pointed_at_its_file_when_next_opened(self):
        site_layout.prepare(self.root)
        cloud, _ = self.own_connection(self.root / 'Tapas/Library')
        held, done = threading.Event(), threading.Event()
        def download():
            with LibraryLock(self.root / 'Tapas/Library'):
                held.set(); done.wait(30)
        running = threading.Thread(target=download); running.start()
        self.addCleanup(running.join); self.addCleanup(done.set)
        self.assertTrue(held.wait(30))
        site_layout.prepare(self.root)  # A download is running: left for the next time.
        done.set(); running.join()
        targets = json.loads((cloud / 'targets.json').read_text())['targets']
        self.assertEqual(targets['b2']['config'], str(self.root / 'Library/.archive-state/cloud/b2.conf'))
        site_layout.prepare(self.root)
        targets = json.loads((cloud / 'targets.json').read_text())['targets']
        self.assertEqual(targets['b2']['config'], str(cloud / 'b2.conf'))
        # A record that cannot be read never keeps the library from opening.
        (cloud / 'targets.json').write_text('{')
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            site_layout.prepare(self.root)

    def test_destination_conflicts_and_cross_filesystem_moves_never_copy_or_overwrite(self):
        target = self.root / 'Tapas'; target.mkdir(); kept = target / 'keep'; kept.write_bytes(b'keep')
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertEqual(kept.read_bytes(), b'keep'); kept.unlink()
        with patch('comic_archive.site_layout.os.rename', side_effect=OSError(errno.EXDEV, 'different filesystem')):
            with self.assertRaises(site_layout.LayoutError):
                site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())
        self.assertFalse((target / 'Library').exists())

    def test_malformed_journals_and_symlinked_sources_are_refused(self):
        shared = self.root / '.comic-shared'; shared.mkdir()
        journal = shared / site_layout.PENDING; journal.write_text(json.dumps({'schema_version':1,'entries':{}}))
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        journal.unlink()
        path = self.root / '.comic-web'; path.rename(self.root / 'outside'); path.symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaises(site_layout.LayoutError):
            site_layout.prepare(self.root)
        self.assertTrue((self.root / 'Library/episode.cbz').is_file())


# Run each interruption in a separate interpreter: os._exit leaves no Python
# cleanup, so the next process must recover from the actual on-disk state.
_CRASH_CHILD = r'''
import os, sys
from pathlib import Path
from comic_archive import archive, site_layout
root, stage = Path(sys.argv[1]), sys.argv[2]
shared = root / '.comic-shared'
original_json, original_replace = archive.atomic_json, archive.replace_file
original_rename, original_unlink = os.rename, Path.unlink
original_sync = site_layout.sync_directory
renames = 0

def crash(point):
    if point == stage:
        os._exit(91)

def publish(path, value):
    if path.name == site_layout.PENDING:
        crash('before-journal')
    original_json(path, value)
    if path.name == site_layout.PENDING:
        crash('journal-published')
    elif path.name == site_layout.MARKER:
        crash('marker-published')

def replace(source, destination):
    if destination.name == site_layout.PENDING:
        crash('journal-temp')
    elif destination.name == site_layout.MARKER:
        crash('marker-temp')
    original_replace(source, destination)

def rename(source, destination):
    global renames
    renames += 1
    crash(f'before-rename-{renames}')
    original_rename(source, destination)
    crash(f'after-rename-{renames}')

def sync(path):
    original_sync(path)
    if path == shared:
        if (shared / site_layout.MARKER).exists():
            crash('marker-durable')
        elif (shared / site_layout.PENDING).exists():
            crash('journal-durable')

def unlink(path, *args, **kwargs):
    if path == shared / site_layout.PENDING:
        crash('before-journal-remove')
    original_unlink(path, *args, **kwargs)
    if path == shared / site_layout.PENDING:
        crash('after-journal-remove')

archive.atomic_json, archive.replace_file = publish, replace
site_layout.os.rename, site_layout.sync_directory = rename, sync
Path.unlink = unlink
site_layout.prepare(root)
'''


class MoveDurabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def fixture(self, name):
        root = self.base / name
        files = {
            'Library/Series A/001.cbz': b'original comic\x00\xff',
            'Library/Series A/001.json': b'{"source_episode_id":"81"}',
            'Library/Series B/002.epub': b'original novel\x01',
            'Library/.archive-state/inventory.sqlite3': b'opaque index',
            'Library/.archive-state/inventory.sqlite3-wal': b'opaque WAL',
            '.comic-session/state.json': b'{"cookies":[]}',
            '.comic-web/secret.key': bytes(range(32)),
            '.comic-shared/keep.json': b'shared settings',
            'Webtoon/Library/keep.cbz': b'other provider',
            'Tappytoon/Library/keep.epub': b'another provider',
        }
        fingerprints = {}
        for relative, content in files.items():
            path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            info = path.stat()
            fingerprints[relative] = (content, info.st_dev, info.st_ino, info.st_mtime_ns)
        return root, fingerprints

    def preserved(self, root, fingerprints, complete=False):
        for relative, expected in fingerprints.items():
            old = root / relative
            candidates = [old]
            if Path(relative).parts[0] in site_layout.ENTRIES:
                candidates.append(root / 'Tapas' / relative)
            present = [p for p in candidates if p.is_file()]
            self.assertEqual(len(present), 1, relative)
            path, = present
            info = path.stat()
            self.assertEqual((path.read_bytes(), info.st_dev, info.st_ino, info.st_mtime_ns), expected, relative)
            if complete and len(candidates) > 1:
                self.assertEqual(path, candidates[1])

    def test_process_death_at_every_publication_and_rename_boundary_preserves_all_files(self):
        import subprocess
        import sys
        stages = ['before-journal', 'journal-temp', 'journal-published', 'journal-durable',
                  *(f'{when}-rename-{n}' for n in (1, 2, 3) for when in ('before', 'after')),
                  'marker-temp', 'marker-published', 'marker-durable', 'before-journal-remove', 'after-journal-remove']
        for stage in stages:
            with self.subTest(stage=stage):
                root, fingerprints = self.fixture(stage)
                result = subprocess.run([sys.executable, '-c', _CRASH_CHILD, str(root), stage],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 91, result.stderr)
                self.preserved(root, fingerprints)
                site_layout.prepare(root)
                self.preserved(root, fingerprints, complete=True)
                self.assertTrue(site_layout.known(root))
                self.assertFalse((root / '.comic-shared' / site_layout.PENDING).exists())

    def test_flush_failure_at_each_step_can_be_retried_without_losing_or_copying_files(self):
        original = site_layout.sync_directory
        for fail_at in range(1, 11):
            with self.subTest(fail_at=fail_at):
                root, fingerprints = self.fixture(f'flush-{fail_at}')
                calls = 0
                def flush(path):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError(errno.EIO, 'injected flush failure')
                    original(path)
                with patch('comic_archive.site_layout.sync_directory', side_effect=flush), self.assertRaises(site_layout.LayoutError):
                    site_layout.prepare(root)
                self.preserved(root, fingerprints)
                site_layout.prepare(root)
                self.preserved(root, fingerprints, complete=True)

    def test_journal_and_renames_are_durable_before_the_journal_is_removed(self):
        root, fingerprints = self.fixture('ordering')
        events = []
        original_sync, original_rename, original_unlink = site_layout.sync_directory, os.rename, Path.unlink
        def sync(path):
            original_sync(path); events.append(('sync', Path(path)))
        def rename(source, destination):
            self.assertIn(('sync', root / '.comic-shared'), events)
            original_rename(source, destination); events.append(('rename', Path(destination)))
        def unlink(path, *args, **kwargs):
            if path == root / '.comic-shared' / site_layout.PENDING:
                for index, event in enumerate(events):
                    if event[0] == 'rename':
                        self.assertIn(('sync', root / 'Tapas'), events[index + 1:])
                        self.assertIn(('sync', root), events[index + 1:])
                self.assertEqual(events[-1], ('sync', root / '.comic-shared'))
            return original_unlink(path, *args, **kwargs)
        with patch('comic_archive.site_layout.sync_directory', side_effect=sync), \
                patch('comic_archive.site_layout.os.rename', side_effect=rename), patch.object(Path, 'unlink', unlink):
            site_layout.prepare(root)
        self.preserved(root, fingerprints, complete=True)

    def test_real_inventory_identity_and_attempt_history_survive_the_move_unchanged(self):
        from comic_archive.inventory import Inventory
        root = self.base / 'database'; root.mkdir()
        library = root / 'Library'
        with Inventory(library) as inventory:
            inventory.bind_account('fixture-account')
            inventory.observe('42', 'Series', [{'id':'81','title':'Episode','order':1,'access_state':'tapas_unlocked'}], 'sync')
            inventory.mark('81', 'failed', 'job_failed')
            before = [dict(row) for row in inventory.db.execute('SELECT * FROM episodes')]
        database = library / '.archive-state/inventory.sqlite3'
        content, inode = database.read_bytes(), database.stat().st_ino
        site_layout.prepare(root)
        moved = root / 'Tapas/Library/.archive-state/inventory.sqlite3'
        self.assertEqual(moved.read_bytes(), content)
        self.assertEqual(moved.stat().st_ino, inode)
        with Inventory(root / 'Tapas/Library', read_only=True) as inventory:
            self.assertEqual([dict(row) for row in inventory.db.execute('SELECT * FROM episodes')], before)
            self.assertEqual(inventory.account_binding_state('fixture-account'), 'matches')
