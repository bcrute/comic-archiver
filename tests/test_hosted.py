import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE, PRIVATE_FOLDER, replaces_open_files
from comic_archive.archive import Store, digest
from comic_archive.hosted import WorkspaceRegistry
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered
from comic_archive.locking import LibraryLock
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter
from test_archive import png


class HostedTests(unittest.TestCase):
    def test_cloud_error_projection_allows_only_fixed_codes_and_no_provider_diagnostics(self):
        from comic_archive.cloud_errors import MESSAGES
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        for code in (*MESSAGES, 'PRIVATE', ['PRIVATE'], None):
            response.write_text(json.dumps({'task_id':task,'status':'error','error_code':code,
                'error':'PRIVATE_TOKEN PRIVATE_PATH','cookie':'PRIVATE'}))
            value=self.registry.task('website-user-a',self.a.id,task)
            self.assertEqual(value['code'],code if isinstance(code,str) and code in MESSAGES else 'task_failed')
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_storage_preview_refuses_hosted_device_claims_and_projects_only_safe_metrics(self):
        with self.assertRaises(ArchiveError):
            self.registry.preview_series_storage('website-user-a',self.a.id,'https://tapas.io/series/sample','series')
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        response.write_text(json.dumps({'task_id':task,'status':'ok','result':{'storage_preview':{
            'planned_episode_count':2,'content_type':'novel','fits_estimate':False,'cookie':'PRIVATE','path':'PRIVATE'},
            'account_identity':'PRIVATE','source_series_id':'PRIVATE'}}))
        value=self.registry.task('website-user-a',self.a.id,task)['result']
        self.assertEqual(value,{'storage_preview':{'planned_episode_count':2,'content_type':'novel','fits_estimate':False}})

    def test_cloud_test_controls_refuse_hosted_and_cross_owner_workspaces(self):
        for owner,wid in [('website-user-a',self.a.id),('website-user-a',self.b.id)]:
            with self.assertRaises(ArchiveError):self.registry.test_cloud(owner,wid,'google')
            with self.assertRaises(ArchiveError):self.registry.cleanup_cloud_test(owner,wid,'f257c685-2a39-4e16-8dc0-353338e2f7d7')

    def test_cloud_test_result_projection_exposes_only_verified_counts_and_booleans(self):
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        response.write_text(json.dumps({'task_id':task,'status':'ok','result':{'verified_episodes':2,
            'test_files_deleted':True,'local_archives_retained':True,'config':'PRIVATE','test_id':'PRIVATE','prefix':'PRIVATE'}}))
        result=self.registry.task('website-user-a',self.a.id,task)['result']
        self.assertEqual(result,{'verified_episodes':2,'test_files_deleted':True,'local_archives_retained':True})
        response.write_text(json.dumps({'task_id':task,'status':'ok','result':{'verified_episodes':True,'test_files_deleted':1,'local_archives_retained':'PRIVATE'}}))
        self.assertEqual(self.registry.task('website-user-a',self.a.id,task)['result'],{})

    def test_daily_schedules_are_owner_scoped_and_keep_server_budget_caps(self):
        self.prepare_queue(self.a);self.prepare_queue(self.b)
        task=self.registry.configure_daily('website-user-a',self.a.id,run_now=True)
        request=json.loads((self.a.session/'worker/requests'/(task['task_id']+'.json')).read_text())
        self.assertEqual(request['operation'],'configure_daily')
        self.assertTrue(request['args']['options']['free_only'])
        self.assertEqual(request['args']['options']['max_seconds'],900)
        self.assertEqual(request['args']['options']['max_pages'],1000)
        for owner,wid,opts in [('website-user-a',self.b.id,{}),('website-user-a',self.a.id,{'max_seconds':901}),
                               ('website-user-a',self.a.id,{'free_only':False}),('website-user-a',self.a.id,{'output':str(self.b.library)})]:
            with self.assertRaises(ArchiveError):self.registry.configure_daily(owner,wid,options=opts)
        with self.assertRaises(ArchiveError):self.registry.daily_status('website-user-a',self.b.id)
        self.assertFalse(self.registry.daily_status('website-user-b',self.b.id)['configured'])

    def setUp(self):
        self.work=tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.registry=WorkspaceRegistry(Path(self.work.name)/'hosted')
        self.a=self.registry.create('website-user-a')
        self.b=self.registry.create('website-user-b')

    def prepare_queue(self,workspace):
        (workspace.session/'worker/requests').mkdir(parents=True)

    def seed(self,workspace):
        with Inventory(workspace.library) as inventory:
            inventory.observe('42','Series',[{'id':'81','title':'Episode','order':1,'access_state':'tapas_free'}],'test_fixture')

    def test_registry_is_private_persistent_and_owner_scoped(self):
        self.assertEqual(self.registry.ids('website-user-a'),[self.a.id])
        self.assertEqual(self.registry.ids('website-user-b'),[self.b.id])
        restored=WorkspaceRegistry(self.registry.root)
        self.assertEqual(restored.resolve('website-user-a',self.a.id),self.a)
        with self.assertRaises(ArchiveError):
            restored.resolve('website-user-b',self.a.id)
        for path,mode in [(self.registry.root,PRIVATE_FOLDER),(self.registry.database,PRIVATE_FILE),(self.registry.root/'registry.key',PRIVATE_FILE),(self.a.library,PRIVATE_FOLDER),(self.a.session,PRIVATE_FOLDER)]:
            self.assertEqual(path.stat().st_mode&0o777,mode)
        self.assertNotIn(b'website-user-a',self.registry.database.read_bytes())

    def test_untrusted_ids_and_symlinked_workspaces_are_rejected(self):
        for key in ('../'+self.a.id,'/tmp',None,self.b.id):
            with self.assertRaises(ArchiveError):
                self.registry.resolve('website-user-a',key)
        moved=self.a.session.with_name('original-session')
        self.a.session.rename(moved)
        self.a.session.symlink_to(self.b.session,target_is_directory=True)
        with self.assertRaises(ArchiveError):
            self.registry.resolve('website-user-a',self.a.id)
        self.assertEqual(self.registry.ids('website-user-b'),[self.b.id])

    def test_owner_jobs_have_separate_queues_and_no_path_overrides(self):
        self.prepare_queue(self.a);self.prepare_queue(self.b)
        a=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1,'max_seconds':5})
        b=self.registry.submit('website-user-b',self.b.id,'archive_discovered',{'limit':1})
        self.assertNotEqual(a['task_id'],b['task_id'])
        self.assertEqual(self.registry.task('website-user-a',self.a.id,a['task_id'])['status'],'queued')
        for owner,wid,task in [('website-user-a',self.b.id,b['task_id']),('website-user-a',self.a.id,b['task_id'])]:
            with self.assertRaises(ArchiveError):
                self.registry.task(owner,wid,task)
        for operation,options in [('sync',{'output':str(self.b.library)}),('shell',{'command':'invalid'}),('enrich_series',{'id':'42','refresh':'yes'})]:
            with self.assertRaises(ArchiveError):
                self.registry.submit('website-user-a',self.a.id,operation,options)
        with self.assertRaises(ArchiveError):
            self.registry.submit('website-user-a',self.b.id,'sync',{'limit':1})
        spec=self.registry.worker_spec('website-user-a',self.a.id)
        self.assertEqual(spec['cwd'],self.a.root)
        self.assertEqual(spec['argv'][-2:],('browser-worker','--stable-code'))
        self.assertNotIn(str(self.b.root),repr(spec))

    def test_task_projection_omits_paths_credentials_and_diagnostics(self):
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        response.write_text(json.dumps({'task_id':task,'status':'ok','result':{'status':'limited','downloaded_pages':2,
            'downloaded_bytes':100,'folder':'PRIVATE_PATH','cookie':'PRIVATE_COOKIE','diagnostic':{'token':'PRIVATE'},
            'archived_episode_ids':['81'],'enriched_series_ids':['42']}}))
        public=self.registry.task('website-user-a',self.a.id,task)
        self.assertNotIn('PRIVATE',json.dumps(public))
        self.assertEqual(public['result']['downloaded_pages'],2)
        status=self.registry.status('website-user-a',self.a.id)
        self.assertFalse(status['session_configured'])
        self.assertNotIn(str(self.a.root),json.dumps(status))
        self.assertFalse(status['inventory_complete'])

    def test_network_pause_projection_keeps_only_fixed_reason_and_gateway_status(self):
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        for reason,http_status in (('network_unavailable',503),('PRIVATE',503),('network_unavailable','PRIVATE'),
                                   ('network_unavailable',403)):
            response.write_text(json.dumps({'task_id':task,'status':'ok','result':{
                'status':'paused','pause_reason':reason,'source_http_status':http_status,'diagnostic':'PRIVATE'}}))
            result=self.registry.task('website-user-a',self.a.id,task)['result']
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertEqual(result.get('pause_reason'),reason if reason=='network_unavailable' else None)
            self.assertEqual(result.get('source_http_status'),503 if reason=='network_unavailable' and http_status==503 else None)

    def test_metadata_retry_projection_publishes_only_valid_counts_and_numeric_ids(self):
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir()
        for count in (1,0,-1,True,'PRIVATE'):
            response.write_text(json.dumps({'task_id':task,'status':'ok','result':{
                'status':'finished','metadata_unavailable_series_count':count,'enriched_series_ids':['42'],
                'metadata_unavailable_series':[{'series_id':'42','code':'PRIVATE_ERROR'}],
                'folder':'PRIVATE_PATH','diagnostic':'PRIVATE_COOKIE'}}))
            result=self.registry.task('website-user-a',self.a.id,task)['result']
            self.assertEqual(result.get('metadata_unavailable_series_count'),count if type(count) is int and count>=0 else None)
            self.assertEqual(result['enriched_series_ids'],['42']);self.assertNotIn('PRIVATE',json.dumps(result))

    def test_server_budget_defaults_and_caps_cannot_be_removed_by_a_request(self):
        self.prepare_queue(self.a)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'max_seconds':None})['task_id']
        request=json.loads((self.a.session/'worker/requests'/f'{task}.json').read_text())
        self.assertEqual(request['args']['max_seconds'],900)
        self.assertEqual(request['args']['max_bytes'],256*1024**2)
        for key,value in [('max_bytes',256*1024**2+1),('limit',51),('max_seconds',float('nan')),('max_pages',True),('max_bytes',10**1000),('max_seconds',10**1000)]:
            with self.assertRaises(ArchiveError):
                self.registry.submit('website-user-a',self.a.id,'sync',{key:value})

    @patch('builtins.print')
    def test_two_synthetic_account_jobs_run_concurrently_and_exports_are_isolated(self,output):
        barrier=threading.Barrier(2)
        class ConcurrentAccount(AccountAdapter):
            def capture_automatic(self,url):
                barrier.wait(timeout=5)
                return super().capture_automatic(url)
        self.seed(self.a);self.seed(self.b)
        adapters=[ConcurrentAccount(),ConcurrentAccount()];adapters[1].identity='22'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(archive_discovered,adapter,workspace.library) for adapter,workspace in zip(adapters,[self.a,self.b],strict=True)]
            results=[f.result(timeout=10) for f in futures]
        self.assertEqual([r['archived_episode_ids'] for r in results],[['81'],['81']])
        self.assertEqual(self.registry.status('website-user-a',self.a.id)['episodes_by_status'],{'archived':1})
        exports_a=self.registry.exports('website-user-a',self.a.id)
        exports_b=self.registry.exports('website-user-b',self.b.id)
        self.assertEqual({e['kind'] for e in exports_a},{'cbz','episode_metadata','series_metadata'})
        self.assertFalse({e['id'] for e in exports_a}&{e['id'] for e in exports_b})
        cbz=next(e for e in exports_a if e['kind']=='cbz')
        with self.registry.open_export('website-user-a',self.a.id,cbz['id']) as download:
            self.assertEqual(download.size,cbz['size'])
            import zipfile
            with zipfile.ZipFile(download.stream) as comic:
                self.assertEqual(comic.namelist(),['0001.png','0002.png'])
                self.assertEqual(comic.read('0001.png'),png(1))
        for owner,wid in [('website-user-b',self.a.id),('website-user-b',self.b.id)]:
            with self.assertRaises(ArchiveError):
                self.registry.open_export(owner,wid,cbz['id'])
        with self.assertRaises(ArchiveError):
            archive_discovered(adapters[1],self.a.library)

    def test_unpublished_private_files_and_corrupt_exports_cannot_be_downloaded(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:
            store.archive(manifest,adapter,progress=lambda _:None)
        (self.a.session/'state.json').write_text('PRIVATE_COOKIES')
        folder=next(self.a.library.glob('*/series.json')).parent
        (folder/'cover.jpg').write_bytes(png(1))  # Unpublished stray file must not be offered.
        exports=self.registry.exports('website-user-a',self.a.id)
        self.assertNotIn('state.json',[e['filename'] for e in exports])
        self.assertNotIn('cover.jpg',[e['filename'] for e in exports])
        cbz=next(e for e in exports if e['kind']=='cbz')
        next(folder.glob('*.cbz')).write_bytes(b'corrupt')
        with self.assertRaises(ArchiveError):
            self.registry.open_export('website-user-a',self.a.id,cbz['id'])
        with self.assertRaises(ArchiveError):
            self.registry.open_export('website-user-a',self.a.id,'../../state.json')

    def test_private_state_and_cache_folders_are_excluded_even_if_they_contain_valid_CBZ_pairs(self):
        import shutil
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:store.archive(manifest,adapter,progress=lambda _:None)
        archive=next(self.a.library.glob('*/*.cbz'))
        for name in ('.archive-state','.archive-cache','.other-private'):
            folder=self.a.library/name;folder.mkdir(exist_ok=True)
            shutil.copy2(archive,folder/'private.cbz')
            shutil.copy2(archive.with_suffix('.json'),folder/'private.json')
            shutil.copy2(archive.parent/'series.json',folder/'series.json')
        exports=self.registry.exports('website-user-a',self.a.id)
        self.assertEqual(len(exports),3)
        self.assertNotIn('private.cbz',[row['filename'] for row in exports])

    def test_a_library_writer_does_not_block_existing_exports_or_another_owner_library(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:store.archive(manifest,adapter,progress=lambda _:None)
        with LibraryLock(self.a.library):
            with LibraryLock(self.b.library):
                pass
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.registry.exports,'website-user-a',self.a.id)
                exports=future.result(timeout=5)
                cbz=next(e for e in exports if e['kind']=='cbz')
                future=pool.submit(self.registry.open_export,'website-user-a',self.a.id,cbz['id'])
                with future.result(timeout=5) as download:
                    self.assertTrue(download.stream.read().startswith(b'PK'))

    def test_optional_series_metadata_or_cover_loss_does_not_hide_CBZ_exports(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:
            store.archive(manifest,adapter,progress=lambda _:None)
        series=next(self.a.library.glob('*/series.json'))
        value=json.loads(series.read_text())
        value.update(cover_file='cover.jpg',cover_sha256='0'*64,cover_size=100)
        series.write_text(json.dumps(value))
        for case in ('missing_cover','malformed_cover','missing_series'):
            if case=='malformed_cover':
                value['cover_file']={'malformed':'field'}
                series.write_text(json.dumps(value))
            if case=='missing_series':series.unlink()
            files=self.registry.exports('website-user-a',self.a.id)
            cbz=next(e for e in files if e['kind']=='cbz')
            with self.registry.open_export('website-user-a',self.a.id,cbz['id']) as download:
                self.assertTrue(download.stream.read().startswith(b'PK'))

    @replaces_open_files
    def test_open_download_retains_verified_bytes_across_atomic_replacement(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:
            store.archive(manifest,adapter,progress=lambda _:None)
        archive=next(self.a.library.glob('*/*.cbz'));original=archive.read_bytes()
        cbz=next(e for e in self.registry.exports('website-user-a',self.a.id) if e['kind']=='cbz')
        with self.registry.open_export('website-user-a',self.a.id,cbz['id']) as download:
            replacement=archive.with_suffix('.replacement');replacement.write_bytes(b'corrupt')
            replacement.replace(archive)
            self.assertEqual(download.stream.read(),original)
        with self.assertRaises(ArchiveError):
            self.registry.open_export('website-user-a',self.a.id,cbz['id'])

    @replaces_open_files
    def test_replacement_during_verification_streams_the_pinned_CBZ_and_sidecar(self):
        from comic_archive.archive import verify_episode_contents
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:store.archive(manifest,adapter,progress=lambda _:None)
        archive=next(self.a.library.glob('*/*.cbz'));sidecar=archive.with_suffix('.json')
        original_archive=archive.read_bytes();original_metadata=sidecar.read_bytes()
        exports=self.registry.exports('website-user-a',self.a.id)
        for kind,expected in (('cbz',original_archive),('episode_metadata',original_metadata)):
            archive.write_bytes(original_archive);sidecar.write_bytes(original_metadata)
            selected=next(e for e in exports if e['kind']==kind)
            def replace_then_verify(stream,metadata):
                replacement=archive.with_suffix('.replacement');replacement.write_bytes(b'CORRUPT');replacement.replace(archive)
                replacement=sidecar.with_suffix('.replacement');replacement.write_text('{"cookie":"PRIVATE"}');replacement.replace(sidecar)
                return verify_episode_contents(stream,metadata)
            with patch('comic_archive.hosted.verify_episode_contents',side_effect=replace_then_verify):
                with self.registry.open_export('website-user-a',self.a.id,selected['id']) as download:
                    self.assertEqual(download.stream.read(),expected)
            with self.assertRaises(ArchiveError):self.registry.open_export('website-user-a',self.a.id,selected['id'])

    @replaces_open_files
    def test_cover_and_series_metadata_downloads_verify_the_same_pinned_files(self):
        from comic_archive.archive import verify_series_contents
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:
            store.archive(manifest,adapter,progress=lambda _:None)
            store.write_series_metadata(manifest.series,cover=png(1),enriched=True)
        series=next(self.a.library.glob('*/series.json'));cover=series.parent/'cover.png'
        original_series=series.read_bytes();original_cover=cover.read_bytes()
        exports=self.registry.exports('website-user-a',self.a.id)
        for kind,expected in (('cover',original_cover),('series_metadata',original_series)):
            series.write_bytes(original_series);cover.write_bytes(original_cover)
            selected=next(e for e in exports if e['kind']==kind)
            def replace_then_verify(metadata,data):
                replacement=cover.with_suffix('.replacement');replacement.write_bytes(b'CORRUPT');replacement.replace(cover)
                replacement=series.with_suffix('.replacement');replacement.write_text('{"cookie":"PRIVATE"}');replacement.replace(series)
                return verify_series_contents(metadata,data)
            with patch('comic_archive.hosted.verify_series_contents',side_effect=replace_then_verify):
                with self.registry.open_export('website-user-a',self.a.id,selected['id']) as download:
                    self.assertEqual(download.stream.read(),expected)
            with self.assertRaises(ArchiveError):self.registry.open_export('website-user-a',self.a.id,selected['id'])

    def test_publication_with_mismatched_pinned_pages_is_rejected_without_writer_lock(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:store.archive(manifest,adapter,progress=lambda _:None)
        archive=next(self.a.library.glob('*/*.cbz'));sidecar=archive.with_suffix('.json')
        value=json.loads(sidecar.read_text());value['content_hashes'][0]['sha256']='0'*64;sidecar.write_text(json.dumps(value))
        selected=next(e for e in self.registry.exports('website-user-a',self.a.id) if e['kind']=='cbz')
        with LibraryLock(self.a.library):
            with self.assertRaises(ArchiveError):self.registry.open_export('website-user-a',self.a.id,selected['id'])

    def archive_one(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:store.archive(manifest,adapter,progress=lambda _:None)
        archive=next(self.a.library.glob('*/*.cbz'))
        return archive,archive.with_suffix('.json'),archive.parent/'series.json'

    def answer(self,body):
        (self.a.session/'worker/requests').mkdir(parents=True,exist_ok=True)
        task=self.registry.submit('website-user-a',self.a.id,'sync',{'limit':1})['task_id']
        response=self.a.session/'worker/responses'/f'{task}.json';response.parent.mkdir(exist_ok=True)
        response.write_text(json.dumps({'task_id':task}|body))
        return self.registry.task('website-user-a',self.a.id,task)

    def test_server_limits_are_validated_and_a_bad_limit_object_creates_no_registry(self):
        from comic_archive.hosted import HostedJobLimits
        for bad in ({'limit':0},{'max_pages':True},{'max_bytes':1.5},{'max_seconds':0},{'max_seconds':float('inf')},{'max_seconds':'900'}):
            with self.subTest(bad=bad),self.assertRaises(ArchiveError):HostedJobLimits(**bad)
        elsewhere=Path(self.work.name)/'elsewhere'
        with self.assertRaises(ArchiveError):WorkspaceRegistry(elsewhere,limits={'limit':1})
        self.assertFalse(elsewhere.exists())

    def test_damaged_or_symlinked_registry_key_and_database_are_refused_without_being_rewritten(self):
        root=self.registry.root;key=root/'registry.key';original=key.read_bytes()
        key.write_bytes(original[:10])
        with self.assertRaises(ArchiveError):WorkspaceRegistry(root)
        self.assertEqual(key.read_bytes(),original[:10])
        key.write_bytes(original)
        moved=root/'moved.key';key.rename(moved);key.symlink_to(moved)
        with self.assertRaises(ArchiveError):WorkspaceRegistry(root)
        self.assertEqual(moved.read_bytes(),original)
        key.unlink();moved.rename(key)
        database=self.registry.database;real=root/'real.sqlite3';database.rename(real);database.symlink_to(real)
        with self.assertRaises(ArchiveError):WorkspaceRegistry(root)
        with self.assertRaises(ArchiveError):self.registry.ids('website-user-a')
        database.unlink();real.rename(database)
        self.assertEqual(self.registry.ids('website-user-a'),[self.a.id])

    def test_blank_oversized_or_non_text_owner_identity_and_bad_quota_never_reach_a_workspace(self):
        for owner in ('',None,'x'*257,b'website-user-a',7):
            for action in (lambda:self.registry.ids(owner),lambda:self.registry.create(owner),
                           lambda:self.registry.resolve(owner,self.a.id)):
                with self.subTest(owner=owner),self.assertRaises(ArchiveError):action()
        for quota in (0,101,True,'2',1.5):
            with self.subTest(quota=quota),self.assertRaises(ArchiveError):
                self.registry.create('website-user-a',max_per_owner=quota)
        self.assertEqual(self.registry.ids('website-user-a'),[self.a.id])
        self.assertEqual(len(list(self.registry.workspaces.iterdir())),2)

    def test_source_refusal_projection_keeps_only_gateway_status_and_a_sane_retry_delay(self):
        from comic_archive.retry_after import MAX_RETRY_AFTER
        for status,delay,kept_status,kept_delay in ((429,30,429,30),(403,0,403,0),(429,MAX_RETRY_AFTER,429,MAX_RETRY_AFTER),
                (429,MAX_RETRY_AFTER+1,429,None),(429,-1,429,None),(429,True,429,None),(429,'30',429,None),(500,30,None,None)):
            value=self.answer({'status':'error','error_code':'source_refused','error':'PRIVATE',
                'result':{'source_http_status':status,'retry_after_seconds':delay,'cookie':'PRIVATE'}})
            self.assertEqual(value['code'],'source_refused')
            self.assertEqual(value.get('source_http_status'),kept_status,(status,delay))
            self.assertEqual(value.get('retry_after_seconds'),kept_delay,(status,delay))
            self.assertNotIn('PRIVATE',json.dumps(value))
        self.assertEqual(self.answer({'status':'error','error_code':'source_refused','result':'PRIVATE'})['code'],'source_refused')

    def test_single_series_result_projection_keeps_only_valid_ids_flags_and_cancellation(self):
        value=self.answer({'status':'ok','result':{'status':'finished','source_series_id':'42','cover_archived':True,
            'cancelled_before_start':True,'archived_episode_ids':['81','82'],'folder':'PRIVATE'}})
        self.assertEqual(value['result'],{'status':'finished','source_series_id':'42','cover_archived':True,
            'cancelled_before_start':True,'archived_episode_ids':['81','82']})
        for series_id in ('0123','abc','1'*21,42):
            value=self.answer({'status':'ok','result':{'status':'finished','source_series_id':series_id,'cover_archived':'yes',
                'cancelled_before_start':1,'archived_episode_ids':['81','PRIVATE']}})
            self.assertEqual(value['result'],{'status':'finished'},series_id)

    def test_status_reports_a_session_file_but_refuses_a_symlinked_one(self):
        state=self.a.session/'state.json';state.write_text('PRIVATE_COOKIES')
        self.assertTrue(self.registry.status('website-user-a',self.a.id)['session_configured'])
        outside=Path(self.work.name)/'outside.json';outside.write_text('PRIVATE_COOKIES')
        state.unlink();state.symlink_to(outside)
        with self.assertRaises(ArchiveError):self.registry.status('website-user-a',self.a.id)

    def test_worker_launch_data_is_fixed_and_the_headless_choice_must_be_an_explicit_boolean(self):
        self.assertNotIn('--headless',self.registry.worker_spec('website-user-a',self.a.id,headless=False)['argv'])
        self.assertIn('--headless',self.registry.worker_spec('website-user-a',self.a.id)['argv'])
        for bad in ('yes',1,None):
            with self.assertRaises(ArchiveError):self.registry.worker_spec('website-user-a',self.a.id,headless=bad)

    def test_daily_schedule_keeps_lower_explicit_budgets_and_refuses_malformed_options(self):
        self.prepare_queue(self.a)
        budgets={'limit':5,'max_pages':20,'max_bytes':1000,'max_seconds':60}
        task=self.registry.configure_daily('website-user-a',self.a.id,options=budgets)
        request=json.loads((self.a.session/'worker/requests'/(task['task_id']+'.json')).read_text())
        self.assertEqual({key:request['args']['options'][key] for key in budgets},budgets)
        self.assertTrue(request['args']['options']['free_only'])
        for options in (['limit'],'x',{'limit':0},{'max_pages':1001},{'max_bytes':True},{'limit':2.5}):
            with self.subTest(options=options),self.assertRaises(ArchiveError):
                self.registry.configure_daily('website-user-a',self.a.id,options=options)
        self.assertEqual(len(list((self.a.session/'worker/requests').iterdir())),1)

    def test_unreadable_series_file_or_unpaired_folders_never_break_or_widen_the_export_list(self):
        archive,sidecar,series=self.archive_one()
        orphans=self.a.library/'orphans';orphans.mkdir()
        (orphans/'episode.cbz').write_bytes(b'NO_SIDECAR');(orphans/'series.json').write_text('{}')
        (series.parent/'cover.png').write_bytes(png(1))  # Never named by a readable series file.
        wanted={'cbz','episode_metadata','series_metadata'}
        for content in ('not json','[]'):
            series.write_text(content)
            exports=self.registry.exports('website-user-a',self.a.id)
            self.assertEqual({row['kind'] for row in exports},wanted,content)
            self.assertEqual({row['filename'] for row in exports},{archive.name,sidecar.name,'series.json'})
        outside=Path(self.work.name)/'outside.json';outside.write_text(json.dumps({'cover_file':'cover.png'}))
        series.unlink();series.symlink_to(outside)
        # The link is left out, never followed; the episode beside it is still offered.
        exports=self.registry.exports('website-user-a',self.a.id)
        self.assertEqual({row['filename'] for row in exports},{archive.name,sidecar.name})

    def test_a_linked_or_vanished_file_is_left_out_and_the_rest_of_the_library_is_still_offered(self):
        archive,sidecar,_=self.archive_one()
        other=archive.with_name('0002 - Other.cbz');other.write_bytes(archive.read_bytes())
        other.with_suffix('.json').write_bytes(sidecar.read_bytes())
        private=Path(self.work.name)/'private.cbz';private.write_bytes(b'PRIVATE_COOKIES')
        linked=archive.with_name('0003 - Linked.cbz');linked.symlink_to(private)
        linked.with_suffix('.json').write_bytes(sidecar.read_bytes())
        lone=archive.with_name('0004 - Lone.cbz');lone.write_bytes(archive.read_bytes())
        lone.with_suffix('.json').symlink_to(sidecar)  # Its details are a link: the episode is left out with them.
        names={row['filename'] for row in self.registry.exports('website-user-a',self.a.id)}
        self.assertEqual(names,{archive.name,sidecar.name,other.name,other.with_suffix('.json').name,'series.json'})
        # A file removed between reading the folder and measuring it is left out too.
        real=self.registry._exports
        def vanished(workspace):
            result=real(workspace);other.unlink();return result
        with patch.object(self.registry,'_exports',side_effect=vanished):
            names={row['filename'] for row in self.registry.exports('website-user-a',self.a.id)}
        self.assertEqual(names,{archive.name,sidecar.name,other.with_suffix('.json').name,'series.json'})
        selected=next(e for e in self.registry.exports('website-user-a',self.a.id) if e['filename']==archive.name)['id']
        with self.registry.open_export('website-user-a',self.a.id,selected) as download:
            self.assertTrue(download.stream.read().startswith(b'PK'))

    def test_downloads_are_refused_for_damaged_metadata_changed_format_oversized_or_escaping_cover_names(self):
        archive,sidecar,series=self.archive_one()
        original_sidecar,original_series=sidecar.read_bytes(),series.read_bytes()
        (self.a.root/'outside.png').write_bytes(png(1))
        ids={row['kind']:row['id'] for row in self.registry.exports('website-user-a',self.a.id)}
        def refused(*kinds):
            for kind in kinds:
                with self.assertRaises(ArchiveError):self.registry.open_export('website-user-a',self.a.id,ids[kind])
        for content in (b'not json',b'[]',b'"text"'):
            sidecar.write_bytes(content);refused('cbz','episode_metadata')
        value=json.loads(original_sidecar);value['format']='epub'
        sidecar.write_text(json.dumps(value));refused('cbz')
        sidecar.write_bytes(original_sidecar)
        valid=json.loads(original_series);outside=png(1)
        # Only the cover name is wrong: size and hash are those of the file outside the series folder.
        escaping=dict(valid,cover_file='../../outside.png',cover_size=len(outside),cover_sha256=digest(outside))
        opened=[];real_open=os.open
        def recording_open(path,*args,**kwargs):
            opened.append(str(path));return real_open(path,*args,**kwargs)
        with patch('comic_archive.hosted.os.open',side_effect=recording_open):
            series.write_text(json.dumps(escaping));refused('series_metadata')
        self.assertEqual([path for path in opened if path.endswith('outside.png')],[])
        # A valid document padded with blanks past the limit: a cut-off read would still parse.
        for content in (json.dumps(valid)+' '*(256*1024),'[]'):
            series.write_text(content);refused('series_metadata')
        series.write_bytes(original_series)
        with self.registry.open_export('website-user-a',self.a.id,ids['cbz']) as download:
            self.assertTrue(download.stream.read().startswith(b'PK'))
        with self.registry.open_export('website-user-a',self.a.id,ids['series_metadata']) as download:
            self.assertEqual(download.stream.read(),original_series)

    def swapped_after_listing(self,change):
        """The library changes after the file list is built but before the download is opened."""
        real=self.registry._exports
        def listed(workspace):
            result=real(workspace);change();return result
        return patch.object(self.registry,'_exports',side_effect=listed)

    def test_a_listed_file_swapped_for_a_link_or_pipe_before_it_is_opened_is_not_followed(self):
        archive,sidecar,series=self.archive_one()
        selected=next(e for e in self.registry.exports('website-user-a',self.a.id) if e['kind']=='cbz')['id']
        private=Path(self.work.name)/'private.cbz';private.write_bytes(b'PRIVATE_COOKIES')
        original=archive.read_bytes()
        def to_link():archive.unlink();archive.symlink_to(private)
        def to_pipe():archive.unlink();os.mkfifo(archive)
        for change in (to_link,to_pipe) if hasattr(os,'mkfifo') else (to_link,):  # Windows has no named pipes among its files.
            with self.swapped_after_listing(change),self.assertRaises(ArchiveError):
                self.registry.open_export('website-user-a',self.a.id,selected)
            archive.unlink();archive.write_bytes(original)
        with self.registry.open_export('website-user-a',self.a.id,selected) as download:
            self.assertEqual(download.stream.read(),original)

    def test_cover_download_is_refused_when_the_series_file_stops_naming_that_cover(self):
        adapter=AccountAdapter();manifest=adapter.capture_automatic('https://tapas.io/episode/81')
        with closing(Store(self.a.library)) as store:
            store.archive(manifest,adapter,progress=lambda _:None)
            store.write_series_metadata(manifest.series,cover=png(1),enriched=True)
        series=next(self.a.library.glob('*/series.json'));original=series.read_bytes();value=json.loads(original)
        self.assertEqual(value['cover_file'],'cover.png')
        selected=next(e for e in self.registry.exports('website-user-a',self.a.id) if e['kind']=='cover')['id']
        other_cover=dict(value,cover_file='cover.jpg')
        no_cover={key:item for key,item in value.items() if key!='cover_file'}
        for changed in (other_cover,no_cover):
            with self.swapped_after_listing(lambda:series.write_text(json.dumps(changed))),self.assertRaises(ArchiveError):
                self.registry.open_export('website-user-a',self.a.id,selected)
            series.write_bytes(original)
        with self.registry.open_export('website-user-a',self.a.id,selected) as download:
            self.assertEqual(download.stream.read(),png(1))


if __name__=='__main__':
    unittest.main()
