import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE
from comic_archive import sites
from comic_archive.app_actions import Actions, Refused
from comic_archive.inventory import Inventory
from comic_archive.library_model import NO_OPENER
from comic_archive.locking import LibraryLock
from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry, NoSignIn, _private_key
from comic_archive.models import ArchiveError


class LocalLibraryTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry = LocalWorkspaceRegistry(self.root)
        self.workspace = self.registry.resolve(LOCAL_OWNER, self.registry.workspace_id)
        # What the window asks of the library goes through the same calls the window makes.
        self.runtime = SimpleNamespace(registry=self.registry, site=sites.DEFAULT, folder=self.workspace.library)
        self.actions = Actions(lambda: self.runtime)

    def file_to_open(self):
        folder=self.workspace.library/'portable';folder.mkdir()
        archive=folder/'saved episode.cbz';archive.write_bytes(b'owned portable file')
        archive.with_suffix('.json').write_text('{}')
        return archive,next(key for key,(_,kind) in self.registry._exports(self.workspace).items() if kind=='cbz')

    def queue(self):
        queue=self.workspace.session/'worker';(queue/'requests').mkdir(parents=True)
        (queue/'status.json').write_text('{"state":"ready"}')
        return queue/'requests'

    def test_open_existing_file_hands_only_that_file_to_the_fixed_opener_and_copies_nothing(self):
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_library.WINDOWS',False), patch('comic_archive.local_library.subprocess.Popen') as launch:
            launch.return_value.wait.return_value=0
            before=list(archive.parent.iterdir())
            # Opening a saved file does not wait for a download that holds the library.
            with LibraryLock(self.workspace.library):
                result=self.actions.open_file(identifier)
            self.assertEqual(result,{'status':'open_requested'})
            self.assertEqual(launch.call_args.args[0],['xdg-open',str(archive)])
            self.assertNotIn(str(archive),json.dumps(result))
            self.assertEqual(list(archive.parent.iterdir()),before)
            self.assertEqual(archive.read_bytes(),b'owned portable file')
            self.assertFalse(list(self.workspace.session.glob('worker/requests/*')))

    def test_open_refuses_foreign_missing_private_and_symlinked_files_before_launch(self):
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_library.WINDOWS',False), patch('comic_archive.local_library.subprocess.Popen') as launch:
            for owner,workspace,export in [('other-owner',self.workspace.id,identifier),
                    (LOCAL_OWNER,'0'*32,identifier),(LOCAL_OWNER,self.workspace.id,'0'*64),
                    (LOCAL_OWNER,self.workspace.id,'../../.comic-session/state.json'),(LOCAL_OWNER,self.workspace.id,None)]:
                with self.assertRaises(ArchiveError): self.registry.open_file_on_device(owner,workspace,export)
            archive.unlink()
            with self.assertRaises(ArchiveError): self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            private=self.workspace.session/'state.json';private.write_text('PRIVATE_COOKIES')
            archive.symlink_to(private)
            with self.assertRaises(ArchiveError): self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            launch.assert_not_called();self.assertEqual(private.read_text(),'PRIVATE_COOKIES')

    def test_missing_opener_or_default_app_is_told_in_fixed_words_and_allows_retry(self):
        _,identifier=self.file_to_open()
        with patch('comic_archive.local_library.WINDOWS',False), patch('comic_archive.local_library.subprocess.Popen') as launch:
            launch.side_effect=FileNotFoundError('/PRIVATE_PATH RAW_HELPER_ERROR')
            with self.assertRaises(Refused) as refused: self.actions.open_file(identifier)
            self.assertEqual((refused.exception.code,str(refused.exception)),('device_file_open_unavailable',NO_OPENER))
            launch.side_effect=None;launch.return_value.wait.return_value=3
            with self.assertRaises(Refused) as again: self.actions.open_file(identifier)
            self.assertEqual((again.exception.code,str(again.exception)),('device_file_open_unavailable',NO_OPENER))
            launch.return_value.wait.return_value=0
            self.assertEqual(self.actions.open_file(identifier),{'status':'open_requested'})

    def test_default_app_that_stays_attached_is_not_killed_or_kept_waiting(self):
        import subprocess
        _,identifier=self.file_to_open()
        with patch('comic_archive.local_library.WINDOWS',False), patch('comic_archive.local_library.subprocess.Popen') as launch, \
                patch('comic_archive.local_library.threading.Thread') as thread:
            launch.return_value.wait.side_effect=subprocess.TimeoutExpired('xdg-open',2)
            result=self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)
            self.assertEqual(result,{'status':'open_requested'})
            launch.return_value.kill.assert_not_called();launch.return_value.terminate.assert_not_called()
            thread.assert_called_once_with(target=launch.return_value.wait,daemon=True)
            thread.return_value.start.assert_called_once()

    def test_on_windows_the_saved_file_is_handed_to_windows_and_a_refusal_is_reported(self):
        from comic_archive.errors import DeviceFileOpenUnavailable
        archive,identifier=self.file_to_open()
        with patch('comic_archive.local_library.WINDOWS',True), patch('comic_archive.local_library.os.startfile',create=True) as opened, \
                patch('comic_archive.local_library.subprocess.Popen') as launch:
            self.assertEqual(self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier),{'status':'open_requested'})
            opened.assert_called_once_with(archive);launch.assert_not_called()
            opened.side_effect=OSError('PRIVATE')  # No program on this computer opens such a file.
            with self.assertRaises(DeviceFileOpenUnavailable):
                self.registry.open_file_on_device(LOCAL_OWNER,self.workspace.id,identifier)

    def test_saved_files_are_listed_only_for_this_library(self):
        self.assertEqual([g['id'] for g in self.actions.saved_files(summary=True)['locations']],['device'])
        with self.assertRaises(ArchiveError): self.registry.saved_files(LOCAL_OWNER,'0'*32,summary=True)
        with self.assertRaises(ArchiveError): self.registry.saved_files('someone-else',self.workspace.id,summary=True)

    def test_button_signin_takes_only_checked_credentials_which_never_enter_the_worker_queue(self):
        credentials={'key_id':'K'*12,'application_key':'PRIVATE_KEY'+'A'*20}
        with patch.object(self.registry.cloud_signin,'start',return_value={'status':'running','provider':'b2'}) as start:
            for provider,given in (([],credentials),('ftp',credentials),('google',credentials),('b2',None),
                                   ('b2',{'password':'PRIVATE'}),('b2',credentials|{'path':'/outside'})):
                with self.assertRaises(Refused) as refused: self.actions.cloud_signin(provider,given)
                self.assertEqual(refused.exception.code,'request_unavailable')
                self.assertNotIn('PRIVATE',str(refused.exception))
            start.assert_not_called()
            result=self.actions.cloud_signin('b2',credentials)
            start.assert_called_once_with('b2',credentials)
            self.assertNotIn('PRIVATE_KEY',json.dumps(result))
            with self.assertRaises(ArchiveError):
                self.registry.start_cloud_signin(LOCAL_OWNER,'0'*32,'b2',credentials)
        self.assertFalse(list(self.workspace.session.glob('worker/requests/*.json')))
        self.assertEqual(self.actions.cloud_cancel()['status'],'idle')
        self.assertEqual(self.registry.cloud_signin_status(LOCAL_OWNER,self.workspace.id),{'status':'idle'})
        with self.assertRaises(ArchiveError): self.registry.cloud_signin_status(LOCAL_OWNER,'0'*32)

    def test_cloud_test_is_queued_and_cleanup_belongs_to_this_library(self):
        requests=self.queue()
        with self.assertRaises(Refused): self.actions.cloud_test('google')
        task=self.actions.cloud_test('b2')['task_id']
        self.assertEqual(json.loads((requests/f'{task}.json').read_text()),{'operation':'cloud_test','args':{'provider':'b2'}})
        test_id='f257c685-2a39-4e16-8dc0-353338e2f7d7'
        with self.assertRaises(Refused): self.actions.cloud_cleanup('../outside')
        with patch('comic_archive.cloud.status',return_value={'pending_test_cleanup':[]}):
            with self.assertRaises(Refused) as refused: self.actions.cloud_cleanup(test_id)
        self.assertEqual(refused.exception.code,'request_unavailable')
        with patch('comic_archive.cloud.status',return_value={'pending_test_cleanup':[{'test_id':test_id,'provider':'b2'}]}):
            task=self.actions.cloud_cleanup(test_id)['task_id']
        self.assertEqual(json.loads((requests/f'{task}.json').read_text()),{'operation':'cloud_cleanup','args':{'test_id':test_id}})

    def test_network_choice_is_strict_and_changes_during_import_lock(self):
        with patch('comic_archive.network.probe',return_value={'connection':'cellular','metered':True}):
            info=self.actions.poll(cloud=False)['network']
            self.assertTrue(info['wifi_only']); self.assertFalse(info['allowed'])
            for value in ('false',0,None):
                with self.assertRaises(Refused): self.actions.wifi_only(value)
            self.assertTrue(self.registry.network_status(LOCAL_OWNER,self.workspace.id)['wifi_only'])
            with LibraryLock(self.workspace.library):
                result=self.actions.wifi_only(False)
            self.assertTrue(result['allowed'])
            with self.assertRaises(ArchiveError): self.registry.network_status(LOCAL_OWNER,'0'*32)

    def test_cloud_storage_is_turned_on_for_this_library_and_uploads_are_queued_with_nothing_else(self):
        info=self.actions.poll(network=False)['cloud']
        self.assertTrue(info['supported_on_this_interface'])
        self.assertFalse(info['targets'][0]['connected'])
        with patch('comic_archive.cloud.enable',return_value=info) as enable:
            self.actions.cloud_enable('b2',True)
            enable.assert_called_once_with(self.workspace.library,'b2',True)
        requests=self.queue()
        self.actions.cloud_upload()
        request=next(requests.glob('*.json'))
        self.assertEqual(json.loads(request.read_text()),{'operation':'cloud_upload','args':{}})

    def test_a_site_uses_the_shared_cloud_storage_or_its_own_as_asked(self):
        from comic_archive import shared
        self.assertIsNone(self.actions.poll(network=False)['cloud']['storage'])
        # A library the app has not linked has nothing to share.
        with self.assertRaises(Refused) as refused: self.actions.cloud_storage('separate')
        self.assertEqual(refused.exception.code,'request_unavailable')
        shared.link(self.workspace.library,self.root/'.comic-shared')
        for mode in ('both',['shared'],None):
            with self.assertRaises(Refused): self.actions.cloud_storage(mode)
        self.assertEqual(shared.cloud_mode(self.workspace.library),'shared')
        for mode in ('separate','shared'):
            self.assertEqual((self.actions.cloud_storage(mode)['storage'],shared.cloud_mode(self.workspace.library)),(mode,mode))

    def test_existing_library_and_session_are_reused_without_copying_or_chmoding_project(self):
        (self.workspace.session / 'owned.txt').write_text('PRIVATE')
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('42', 'Existing series', [], 'fixture')
        mode = self.root.stat().st_mode
        registry = LocalWorkspaceRegistry(self.root)
        self.assertEqual(registry.workspace_id, self.workspace.id)
        self.assertEqual(self.workspace.root, self.root)
        self.assertEqual(self.root.stat().st_mode, mode)
        self.assertEqual(registry.status(LOCAL_OWNER, self.workspace.id)['series_count'], 1)
        self.assertEqual((self.workspace.session / 'owned.txt').read_text(), 'PRIVATE')
        self.assertEqual(registry.ids(LOCAL_OWNER), [self.workspace.id])
        self.assertEqual(registry.ids('someone-else'), [])
        with self.assertRaises(ArchiveError): registry.resolve('someone-else', self.workspace.id)
        self.assertEqual((self.root / '.comic-web/registry.key').stat().st_mode & 0o777, PRIVATE_FILE)

    def test_the_library_key_is_made_once_and_a_linked_or_damaged_one_is_refused_and_kept(self):
        folder = self.root / '.comic-web'
        key = (folder / 'registry.key').read_bytes()
        self.assertEqual((len(key), _private_key(folder)), (32, key))
        other = self.root / 'other-key'; other.mkdir()
        (other / 'registry.key').write_bytes(b'short')
        with self.assertRaises(ArchiveError) as damaged: _private_key(other)
        self.assertEqual(str(damaged.exception), 'Library key is invalid; existing files are retained.')
        self.assertEqual((other / 'registry.key').read_bytes(), b'short')
        (other / 'registry.key').unlink(); (other / 'registry.key').symlink_to(folder / 'registry.key')
        with self.assertRaises(ArchiveError) as linked: _private_key(other)
        self.assertEqual(str(linked.exception), 'Refusing a symlinked library key.')

    def test_dashboard_start_does_not_take_the_writer_lock_or_reset_an_active_import(self):
        from concurrent.futures import ThreadPoolExecutor
        with Inventory(self.workspace.library) as inventory:
            inventory.observe('42', 'Existing', [{'id':'81','title':'E','order':1,'access_state':'tapas_free'}], 'subscribed')
            inventory.start_run()
            inventory.mark('81', 'downloading')
        with LibraryLock(self.workspace.library), ThreadPoolExecutor(max_workers=1) as executor:
            registry = executor.submit(LocalWorkspaceRegistry, self.root).result(timeout=5)
            self.assertEqual(registry.workspace_id, self.workspace.id)
        with Inventory(self.workspace.library, read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'], {'downloading':1})
            self.assertEqual(inventory.status()['last_run']['status'], 'running')

    def test_a_slow_saved_library_listing_does_not_hold_up_the_window_s_other_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        scanning, release = threading.Event(), threading.Event()
        real = self.registry.saved_catalog.collect

        def slow(*args):
            scanning.set(); release.wait(10)
            return real(*args)
        with patch.object(self.registry.saved_catalog, 'collect', side_effect=slow), ThreadPoolExecutor(max_workers=2) as pool, \
                patch('comic_archive.network.probe', return_value={'connection': 'wired', 'metered': False}):
            listing = pool.submit(self.actions.saved_files, summary=True, refresh=True)
            self.assertTrue(scanning.wait(5))
            try:
                # The dashboard's refresh and a change to the library answer while the scan goes on.
                self.assertEqual(pool.submit(self.actions.poll).result(timeout=5)['status']['workspace_id'], self.workspace.id)
                self.assertTrue(pool.submit(self.actions.wifi_only, True).result(timeout=5)['wifi_only'])
            finally:
                release.set()
            self.assertEqual(listing.result(timeout=10)['locations'][0]['id'], 'device')

    def test_symlinked_session_or_project_is_rejected_without_changing_files(self):
        other = self.root / 'other'
        other.mkdir()
        project = self.root / 'project'
        project.mkdir()
        (project / '.comic-session').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(project)
        link = self.root / 'linked-project'
        link.symlink_to(project, target_is_directory=True)
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(link)
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(self.root / 'missing')
        self.assertEqual(list(other.iterdir()), [])

    def test_signin_and_account_are_readable_and_a_fresh_library_has_no_account(self):
        self.assertEqual(self.actions.poll(cloud=False, network=False)['site_auth'], {'status': 'idle'})
        account = self.actions.poll(cloud=False, network=False)['account']
        self.assertTrue(account['supported']); self.assertFalse(account['configured']); self.assertFalse(account['coverage_verified'])
        for read in (self.registry.site_signin_status, self.registry.account_status):
            with self.assertRaises(ArchiveError): read(LOCAL_OWNER, '0' * 32)

    def test_until_the_window_supplies_its_own_the_site_signin_is_idle_and_cannot_start(self):
        signin = NoSignIn()
        self.assertEqual(signin.status(), {'status': 'idle'})
        with self.assertRaises(ArchiveError): signin.start()
        with self.assertRaises(ArchiveError): signin.sign_out()
        signin.close()
        self.assertIsInstance(self.registry.site_signin, NoSignIn)

    def test_browser_signin_is_refused_while_the_downloader_is_busy_and_does_not_start(self):
        worker = self.workspace.session / 'worker'; worker.mkdir(parents=True)
        (worker / 'status.json').write_text('{"state":"busy"}')
        with patch.object(self.registry.site_signin, 'start') as start:
            with self.assertRaises(Refused) as refused: self.actions.signin()
        self.assertEqual(refused.exception.code, 'library_busy')
        start.assert_not_called()
        self.assertEqual(self.registry.site_signin.status(), {'status': 'idle'})
        (worker / 'status.json').write_text('{"state":"ready"}')
        with patch.object(self.registry.site_signin, 'start', return_value={'status': 'running'}) as start:
            self.assertEqual(self.actions.signin(), {'status': 'running'})
        start.assert_called_once_with()

    def test_signing_out_is_refused_while_downloading_and_otherwise_asks_the_sign_in_to_forget_itself(self):
        worker = self.workspace.session / 'worker'; worker.mkdir(parents=True)
        (worker / 'status.json').write_text('{"state":"busy"}')
        with patch.object(self.registry.site_signin, 'sign_out', return_value={'status': 'signed_out'}) as sign_out:
            with self.assertRaises(Refused) as refused: self.actions.sign_out()
            self.assertEqual(refused.exception.code, 'library_busy')
            (worker / 'status.json').write_text('{"state":"ready"}')
            with patch.object(self.registry, 'account_status', return_value={'status': 'running'}):
                with self.assertRaises(Refused) as refused: self.actions.sign_out()
            self.assertEqual(refused.exception.code, 'library_busy')
            sign_out.assert_not_called()
            with patch.object(self.registry, 'account_status', return_value={'status': 'stopped'}):
                self.assertEqual(self.actions.sign_out(), {'status': 'signed_out'})
            sign_out.assert_called_once_with()
            with self.assertRaises(ArchiveError): self.registry.sign_out_site('someone-else', self.workspace.id)
            sign_out.assert_called_once_with()
        # Before the window supplies its sign-in there is none to sign out of.
        with self.assertRaises(Refused) as refused: self.actions.sign_out()
        self.assertEqual(refused.exception.code, 'request_unavailable')

    def test_vanished_or_symlinked_library_is_reported_unavailable_and_never_recreated(self):
        self.assertTrue(self.actions.poll(cloud=False, network=False)['status'])
        library = self.workspace.library
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        library.rename(self.root / 'moved-library'); library.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(Refused) as refused: self.actions.poll(cloud=False, network=False)
        self.assertEqual(refused.exception.code, 'request_unavailable')
        self.assertEqual(list(elsewhere.iterdir()), [])
        library.unlink()
        with self.assertRaises(ArchiveError): self.registry.status(LOCAL_OWNER, self.workspace.id)
        self.assertFalse(library.exists() or library.is_symlink())

    def test_library_that_is_a_file_is_refused_and_left_alone(self):
        project = self.root / 'project'; project.mkdir()
        (project / 'Library').write_text('NOT A FOLDER')
        with self.assertRaises(ArchiveError): LocalWorkspaceRegistry(project)
        self.assertEqual((project / 'Library').read_text(), 'NOT A FOLDER')

    def archive_one(self):
        from contextlib import closing
        from comic_archive.archive import Store
        from test_jobs import AccountAdapter
        adapter = AccountAdapter(); manifest = adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.workspace.library)) as store: store.archive(manifest, adapter, progress=lambda _: None)
        archive = next(self.workspace.library.glob('*/*.cbz'))
        return archive, archive.with_suffix('.json'), archive.parent / 'series.json'

    def listed(self):
        return {path.name: kind for path, kind in self.registry._exports(self.workspace).values()}

    def test_session_state_stray_covers_and_private_folders_are_never_offered_as_saved_files(self):
        import shutil
        from test_archive import png
        archive, sidecar, series = self.archive_one()
        (self.workspace.session / 'state.json').write_text('PRIVATE_COOKIES')
        (series.parent / 'cover.jpg').write_bytes(png(1))  # Never named by the series file.
        for name in ('.archive-state', '.archive-cache', '.other-private'):
            folder = self.workspace.library / name; folder.mkdir(exist_ok=True)
            shutil.copy2(archive, folder / 'private.cbz')
            shutil.copy2(sidecar, folder / 'private.json')
            shutil.copy2(series, folder / 'series.json')
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata', 'series.json': 'series_metadata'})

    def test_unreadable_series_file_or_unpaired_folders_never_break_or_widen_the_saved_files(self):
        from test_archive import png
        archive, sidecar, series = self.archive_one()
        orphans = self.workspace.library / 'orphans'; orphans.mkdir()
        (orphans / 'episode.cbz').write_bytes(b'NO_SIDECAR'); (orphans / 'series.json').write_text('{}')
        (series.parent / 'cover.png').write_bytes(png(1))
        value = json.loads(series.read_text())
        for content in ('not json', '[]', json.dumps(value | {'cover_file': {'malformed': 'field'}}),
                        json.dumps(value | {'cover_file': '../cover.png'})):
            series.write_text(content)
            self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata', 'series.json': 'series_metadata'}, content)
        series.write_text(json.dumps(value | {'cover_file': 'cover.png'}))
        self.assertEqual(self.listed()['cover.png'], 'cover')
        outside = self.root / 'outside.json'; outside.write_text(json.dumps({'cover_file': 'cover.png'}))
        series.unlink(); series.symlink_to(outside)
        # The link is left out, never followed; the episode beside it is still offered.
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata'})
        series.unlink()
        self.assertEqual(self.listed(), {archive.name: 'cbz', sidecar.name: 'episode_metadata'})

    def test_a_linked_file_or_linked_details_are_left_out_and_the_rest_of_the_library_is_still_offered(self):
        archive, sidecar, _ = self.archive_one()
        other = archive.with_name('0002 - Other.cbz'); other.write_bytes(archive.read_bytes())
        other.with_suffix('.json').write_bytes(sidecar.read_bytes())
        private = self.root / 'private.cbz'; private.write_bytes(b'PRIVATE_COOKIES')
        linked = archive.with_name('0003 - Linked.cbz'); linked.symlink_to(private)
        linked.with_suffix('.json').write_bytes(sidecar.read_bytes())
        lone = archive.with_name('0004 - Lone.cbz'); lone.write_bytes(archive.read_bytes())
        lone.with_suffix('.json').symlink_to(sidecar)  # Its details are a link: the episode is left out with them.
        self.assertEqual(set(self.listed()), {archive.name, sidecar.name, other.name, other.with_suffix('.json').name, 'series.json'})

    def test_status_reports_a_session_file_but_refuses_a_symlinked_one(self):
        status = self.registry.status(LOCAL_OWNER, self.workspace.id)
        self.assertFalse(status['session_configured']); self.assertFalse(status['inventory_complete'])
        self.assertNotIn(str(self.root), json.dumps(status))
        state = self.workspace.session / 'state.json'; state.write_text('PRIVATE_COOKIES')
        self.assertTrue(self.registry.status(LOCAL_OWNER, self.workspace.id)['session_configured'])
        outside = self.root / 'outside.json'; outside.write_text('PRIVATE_COOKIES')
        state.unlink(); state.symlink_to(outside)
        with self.assertRaises(ArchiveError): self.registry.status(LOCAL_OWNER, self.workspace.id)


if __name__ == '__main__':
    unittest.main()
