from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from unittest.mock import Mock, patch

from support import helper_program, posix_signals
from comic_archive import cloud
from comic_archive.cloud import Transfer as RealTransfer
from comic_archive.archive import episode_archives, digest
from comic_archive.cloud_errors import CloudTransferError, safe_code
from comic_archive.cloud_stream import PipeUpload, StreamingEpisode, _stage, cleanup_stage, held_cutoff, promote_stream
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.errors import RunLimit
from comic_archive.network import NetworkPolicyHold
from comic_archive.publication import PublicationStore
from test_archive import MemoryAdapter, png
from test_cloud import FakeTransfer
from test_storage import photo


class RecordingPipe:
    def __init__(self, owner, destination):
        self.owner, self.destination = owner, destination
        self.data = bytearray(); self.closed = False
    def write(self, data):
        self.data.extend(data); self.owner.started.set()
    def finish(self): self.close()
    def close(self):
        if self.closed: return
        self.owner.files[(self.owner.provider,self.destination)] = bytes(self.data)
        self.closed = True


class FailingPipe(RecordingPipe):
    def __init__(self, owner, destination, failure):
        super().__init__(owner, destination); self.failure = failure
    def write(self, data):
        if self.failure == 'write-error': raise CloudTransferError('cloud_connection')
        if self.failure == 'write-hold': raise NetworkPolicyHold('held')
        super().write(data)
    def finish(self):
        if self.failure == 'finish-error': raise CloudTransferError('cloud_connection')
        if self.failure == 'finish-hold': raise NetworkPolicyHold('held')
        super().finish()


class StreamingTransfer(FakeTransfer):
    started = threading.Event()
    copies = []
    fail_stream = False
    pipe_failure = None
    def start_stream(self, destination):
        if self.fail_stream: raise ArchiveError('synthetic stream unavailable')
        if self.pipe_failure == 'start-hold': raise NetworkPolicyHold('held')
        if self.pipe_failure: return FailingPipe(self, destination, self.pipe_failure)
        return RecordingPipe(self, destination)
    def verify(self, destination, expected, **kwargs):
        data = self.files.get((self.provider,destination))
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            raise ArchiveError('synthetic missing or changed cloud bytes')
    def upload(self, source, destination, expected):
        self.copies.append(Path(source).suffix)
        super().upload(source, destination, expected)
    def run(self, command, path, *args, **kwargs):
        if command == 'moveto':
            self.files[(self.provider,args[0])] = self.files.pop((self.provider,path)); return b''
        return super().run(command,path,*args,**kwargs)


class CloudStreamTests(unittest.TestCase):
    def test_stream_smoke_failure_cleans_recorded_files_and_keeps_original(self):
        original = next(self.root.glob('*/*.cbz')); before = original.read_bytes()
        with patch.object(StreamingTransfer, 'upload_bytes', side_effect=CloudTransferError('cloud_connection')):
            with self.assertRaises(ArchiveError): cloud.smoke_test(self.root, 'b2', episodes=1, stream=True)
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(StreamingTransfer.files)
        self.assertFalse(cloud.status(self.root)['pending_test_cleanup'])
        self.assertEqual(cloud.smoke_test(self.root, 'b2', episodes=1, stream=True)['streamed_episodes'], 1)

    def test_stream_smoke_rejects_unbounded_or_wrong_provider_options_before_network(self):
        with patch('comic_archive.cloud.transfer_for') as connect:
            for provider, options in [('google', {'stream': True}), ('proton', {'stream': True}),
                                      ('b2', {'stream': 1}), ('b2', {'stream': True, 'episodes': 4})]:
                with self.assertRaises(ArchiveError): cloud.smoke_test(self.root, provider, **options)
            connect.assert_not_called()

    def setUp(self):
        self.connect()
        # These follow a comic streamed to B2 while it is also saved here. A new comic goes to B2
        # straight from memory instead, which B2DirectTests covers.
        direct = patch.object(cloud,'direct_target',return_value=None);direct.start();self.addCleanup(direct.stop)
        self.streams = self.root/'.archive-state/cloud/streams/b2'

    def connect(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name); self.root = self.base/'Library'
        self.config = self.base/'config'; self.config.write_text('[b2]\ntype=b2\n'); self.config.chmod(0o600)
        StreamingTransfer.files = {}; StreamingTransfer.fail_provider = None; StreamingTransfer.fail_delete = False
        StreamingTransfer.fail_upload = False; StreamingTransfer.fail_stream = False; StreamingTransfer.copies = []
        StreamingTransfer.pipe_failure = None
        StreamingTransfer.started = threading.Event()
        self.transfer_patch = patch.object(cloud,'Transfer',StreamingTransfer); self.transfer_patch.start(); self.addCleanup(self.transfer_patch.stop)
        self.series = Series('42','Synthetic',source='tapas')
        seed = Episode('80','42','Seed',1,(Page(1,'https://cdn.test/seed',False),),'tapas_free')
        self.publish((seed,), MemoryAdapter())
        cloud.configure_target(self.root,'b2',config=self.config)
        cloud.smoke_test(self.root,'b2',episodes=1,stream=True); cloud.enable(self.root,'b2',True)
        StreamingTransfer.copies = []; StreamingTransfer.started.clear()
        self.pages = tuple(Page(i,f'https://cdn.test/{i}',False) for i in (1,2))
        self.episode = Episode('81','42','New',2,self.pages,'tapas_free')

    def publish(self, episodes, adapter):
        with closing(PublicationStore(self.root)) as store:
            return store.archive(Manifest(self.series,episodes), adapter, progress=lambda _:None)

    def test_download_and_upload_overlap_order_hashes_and_no_second_archive_upload(self):
        data = photo(); event = StreamingTransfer.started
        class Source:
            def retrieve(owner,page):
                if page.order == 2:
                    if not event.wait(5): raise AssertionError('upload did not overlap source retrieval')
                return data if page.order == 1 else png(2)
        folder = self.publish((self.episode,), Source())
        self.assertEqual(len(episode_archives(folder)), 1)  # Only the seed stays local.
        uploads = [v for (provider,path),v in StreamingTransfer.files.items() if path.endswith('episode.cbz')]
        self.assertEqual(len(uploads), 1)
        self.assertNotIn('.cbz', StreamingTransfer.copies)
        self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))
        receipts = list(self.streams.glob('*.json')); self.assertEqual(receipts, [])
        metadata = next(json.loads(v) for (_,p),v in StreamingTransfer.files.items() if p.endswith('episode.json'))
        from comic_archive.archive import verify_episode_contents
        import io
        self.assertEqual([p['file'] for p in metadata['content_hashes']], ['0001.png','0002.png'])
        self.assertEqual(verify_episode_contents(io.BytesIO(uploads[0]), metadata), [])
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'], 1)

    def test_cloud_failure_uses_verified_local_episode_fallback(self):
        StreamingTransfer.fail_stream = True
        self.publish((self.episode,), MemoryAdapter())
        self.assertIn('.cbz', StreamingTransfer.copies)
        self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'], 1)

    def test_streaming_disk_pressure_holds_and_retry_reuses_verified_source_pages(self):
        import shutil
        disk_usage=shutil.disk_usage
        def available(path):
            return Mock(free=0) if threading.current_thread().name == 'cloud-episode' else disk_usage(path)
        with patch('comic_archive.cloud_stream.shutil.disk_usage',side_effect=available),self.assertRaises(RunLimit):
            self.publish((self.episode,),MemoryAdapter())
        self.assertTrue((self.root/'.archive-cache'/hashlib.sha256(png(1)).hexdigest()).is_file())
        receipt=next(self.streams.glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['state'],'streaming')
        adapter=MemoryAdapter();adapter.retrieve=Mock(return_value=png(2))
        self.publish((self.episode,),adapter)
        self.assertLessEqual(adapter.retrieve.call_count,1)
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],1)
        self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))

    def test_source_failure_keeps_resume_pages_and_cleans_only_own_stage_on_retry(self):
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=[png(1),ArchiveError('source held')])
        with self.assertRaises(ArchiveError): self.publish((self.episode,), adapter)
        receipt = next(self.streams.glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['state'], 'streaming')
        self.assertTrue((self.root/'.archive-cache'/hashlib.sha256(png(1)).hexdigest()).exists())
        StreamingTransfer.files[('b2','unrelated/keep')] = b'keep'
        adapter = MemoryAdapter(); adapter.retrieve = Mock(return_value=png(2))
        self.publish((self.episode,), adapter)
        self.assertEqual(adapter.retrieve.call_count, 1)
        self.assertEqual(StreamingTransfer.files[('b2','unrelated/keep')], b'keep')
        self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))

    def test_multi_episode_upload_finishes_before_next_source_episode(self):
        next_episode = Episode('82','42','Next',3,(Page(1,'https://cdn.test/next',False),),'tapas_free')
        def retrieve(page):
            if page.location.endswith('/next'):
                self.assertEqual(cloud.status(self.root)['targets'][0]['verified'], 1)
                self.assertEqual(len(episode_archives(next(self.root.glob('Synthetic*')))), 1)
            return png(1)
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=retrieve)
        self.publish((self.episode,next_episode),adapter)
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'], 2)

    def test_modified_staging_scope_is_refused_without_remote_deletion(self):
        with closing(PublicationStore(self.root)) as store:
            folder = store.series_folder(self.series)
            stream = StreamingEpisode.create(store,self.series,self.episode,store.episode_path(self.series,self.episode,folder))
            stream.close()
            receipt = json.loads(stream.receipt.read_text()); receipt['remote_path'] = 'unrelated/keep'
            stream.receipt.write_text(json.dumps(receipt))
            with self.assertRaises(ArchiveError): _stage(self.root,receipt['episode_id'],'b2')
            # A record must name the provider it was staged with, and only B2 stages.
            receipt = json.loads(stream.receipt.read_text()); self.assertEqual(receipt.pop('provider'),'b2')
            stream.receipt.write_text(json.dumps(receipt))
            with self.assertRaises(ArchiveError): _stage(self.root,receipt['episode_id'],'b2')
            with self.assertRaises(ArchiveError): _stage(self.root,receipt['episode_id'],'google')
        self.assertEqual(stream.receipt.parent,self.streams)
        with self.assertRaises(ArchiveError): _stage(self.root,'../../keep','b2')

    def test_real_bounded_pipe_child_receives_exact_bytes_and_uses_small_buffers(self):
        self.transfer_patch.stop()
        script = helper_program(self.base/'rclone-fixture', 'import sys,pathlib,json\np=pathlib.Path(sys.argv[sys.argv.index("--config")+1]).parent/"received"\np.write_bytes(sys.stdin.buffer.read())\np.with_suffix(".args").write_text(json.dumps(sys.argv))\n')
        transfer = Mock(binary=str(script),config=self.config,provider='b2'); transfer.guard=Mock(); transfer.remote=lambda p:'b2:'+p
        data = b'content'*50000
        pipe = PipeUpload(transfer,'Comic-Archive/test/episode.cbz')
        pipe.write(data); pipe.finish(); pipe.close()
        self.assertEqual((self.base/'received').read_bytes(), data)
        arguments = json.loads((self.base/'received.args').read_text())
        for flag, value in [('--b2-chunk-size', '5Mi'), ('--b2-upload-concurrency', '1'), ('--streaming-upload-cutoff', '256Ki')]:
            self.assertEqual(arguments[arguments.index(flag) + 1], value)
        self.assertIn('--b2-hard-delete', arguments)
        self.assertNotIn('--size', arguments)
        self.assertTrue(transfer.guard.check.called)

    def test_policy_switch_stops_pipe_and_retains_source_recovery(self):
        transfer = Mock(); transfer.guard.check.side_effect = NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold): PipeUpload(transfer,'own/test')

    def test_real_pipe_failure_projects_only_fixed_reason_and_reaps_owned_child(self):
        from comic_archive.cloud_errors import CloudTransferError, safe_code
        script = helper_program(self.base/'rclone-failure-fixture', 'import sys\nsys.stdin.buffer.read()\nsys.stderr.write("PRIVATE_PATH PRIVATE_TOKEN storageQuotaExceeded\\n")\nsys.exit(7)\n')
        transfer = Mock(binary=str(script),config=self.config);transfer.guard=Mock();transfer.remote=lambda path:'b2:'+path
        pipe=PipeUpload(transfer,'own/episode.cbz')
        try:
            pipe.write(b'episode bytes')
            with self.assertRaises(CloudTransferError) as caught:pipe.finish()
            self.assertEqual(safe_code(caught.exception),'cloud_space')
            self.assertNotIn('PRIVATE',str(caught.exception))
            self.assertEqual(pipe.process.poll(),7)
        finally:pipe.close()

    def test_pipe_that_exits_before_write_uses_safe_authentication_hint(self):
        from comic_archive.cloud_errors import CloudTransferError, safe_code
        script = helper_program(self.base/'rclone-auth-fixture', 'import sys\nsys.stderr.write("PRIVATE_TOKEN invalid_grant\\n")\nsys.exit(7)\n')
        transfer = Mock(binary=str(script),config=self.config);transfer.guard=Mock();transfer.remote=lambda path:'b2:'+path
        pipe=PipeUpload(transfer,'own/episode.cbz')
        try:
            pipe.process.wait(timeout=5)
            with self.assertRaises(CloudTransferError) as caught:pipe.write(b'episode bytes')
            self.assertEqual(safe_code(caught.exception),'cloud_login_required')
            self.assertNotIn('PRIVATE',str(caught.exception))
        finally:pipe.close()

    def fixture(self, name, body):
        # Own the interpreter directly: terminating a Windows batch launcher
        # would leave its Python child running with the fixture file open.
        script = self.base / (name + '.py')
        script.write_text(body)
        real = subprocess.Popen
        def spawn(command, *args, **kwargs):
            if command[0] == str(script): command = [sys.executable, *command]
            return real(command, *args, **kwargs)
        wrapper = patch('comic_archive.cloud_stream.subprocess.Popen', side_effect=spawn)
        wrapper.start(); self.addCleanup(wrapper.stop)
        transfer = Mock(binary=str(script),config=self.config);transfer.guard=Mock();transfer.remote=lambda path:'b2:'+path
        return transfer

    def test_upload_that_cannot_start_leaves_no_child_or_open_input_behind(self):
        transfer = self.fixture('rclone-idle','import time\ntime.sleep(60)\n')
        children = []; real = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real(*args, **kwargs); children.append(child); return child
        with patch('comic_archive.cloud_stream.subprocess.Popen',side_effect=spawn), \
                patch('comic_archive.cloud_stream.os.set_blocking',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError): PipeUpload(transfer,'own/episode.cbz')
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdin.closed)
        missing = Mock(binary=str(self.base/'absent'),config=self.config);missing.guard=Mock();missing.remote=lambda path:path
        with self.assertRaises(OSError): PipeUpload(missing,'own/episode.cbz')

    def test_policy_change_seen_by_the_watcher_stops_the_child_and_fails_the_upload(self):
        transfer = self.fixture('rclone-reader','import sys\nsys.stdin.buffer.read()\n')
        def check():
            if threading.current_thread().name == 'cloud-network-policy': raise NetworkPolicyHold('held')
        transfer.guard.check.side_effect = check
        pipe = PipeUpload(transfer,'own/episode.cbz')
        try:
            pipe.watcher.join(timeout=10)
            self.assertFalse(pipe.watcher.is_alive())
            self.assertIsInstance(pipe.error,NetworkPolicyHold)
            self.assertIsNotNone(pipe.process.poll())
            with self.assertRaises(NetworkPolicyHold): pipe.write(b'episode bytes')
            with self.assertRaises(NetworkPolicyHold): pipe.finish()
        finally: pipe.close()

    @posix_signals
    def test_closing_an_upload_stops_a_child_even_when_it_ignores_termination(self):
        ready = self.base/'ready'; os.mkfifo(ready)
        transfer = self.fixture('rclone-stubborn','import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
            +f'open({str(ready)!r},"w").write("r")\ntime.sleep(60)\n')
        descriptor = os.open(ready,os.O_RDONLY|os.O_NONBLOCK)
        try:
            pipe = PipeUpload(transfer,'own/episode.cbz')
            try:
                self.assertTrue(select.select([descriptor],[],[],30)[0])
                self.assertEqual(os.read(descriptor,1),b'r')
            finally: pipe.close()
        finally: os.close(descriptor)
        self.assertEqual(pipe.process.poll(),-signal.SIGKILL)

    def test_upload_that_stops_accepting_data_fails_with_a_connection_code(self):
        transfer = self.fixture('rclone-stalled','import time\ntime.sleep(60)\n')
        clock = iter([0]*6)
        pipe = PipeUpload(transfer,'own/episode.cbz')
        try:
            with patch('comic_archive.cloud_stream.time',Mock(monotonic=lambda: next(clock,1000))):
                with self.assertRaises(CloudTransferError) as caught: pipe.write(b'x'*(1<<20))
            self.assertEqual(safe_code(caught.exception),'cloud_connection')
        finally: pipe.close()

    def stream_for(self, store, episode):
        folder = store.series_folder(self.series)
        archive = store.episode_path(self.series,episode,folder)
        return archive, StreamingEpisode.create(store,self.series,episode,archive)

    def test_stage_from_a_previous_connection_is_neither_cleaned_nor_promoted(self):
        with closing(PublicationStore(self.root)) as store:
            _, stream = self.stream_for(store,self.episode)
            stream.close()
        remote = stream.record['remote_path']; episode_id = stream.record['episode_id']
        StreamingTransfer.files[('b2',remote)] = b'staged'
        cloud.configure_target(self.root,'b2',config=self.config)  # a new connection identity
        transfer = StreamingTransfer('b2',{})
        with self.assertRaises(ArchiveError): cleanup_stage(self.root,transfer,episode_id)
        bundle = {'episode_id':episode_id,'files':[{'path':'Synthetic/episode.cbz','sha256':'a'*64}]}
        self.assertFalse(promote_stream(self.root,transfer,bundle,'dest/episode.cbz'))
        self.assertTrue(stream.receipt.is_file())
        self.assertEqual(StreamingTransfer.files[('b2',remote)],b'staged')

    def test_promotion_never_adopts_changed_staging_bytes_and_never_hides_a_network_hold(self):
        value = cloud.settings(self.root); target = value['targets']['b2']
        episode_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        sha = hashlib.sha256(b'episode').hexdigest()
        remote = f"Comic-Archive/{value['library_id']}/.staging/{run_id}/episode.cbz"
        receipt = self.streams/(episode_id+'.json')
        cloud._write(receipt,{'schema_version':1,'library_id':value['library_id'],'target_id':target['target_id'],
            'episode_id':episode_id,'run_id':run_id,'archive':'Synthetic/episode.cbz','remote_path':remote,
            'state':'uploaded','sha256':sha,'provider':'b2'})
        bundle = {'episode_id':episode_id,'files':[{'path':'Synthetic/episode.cbz','sha256':sha}]}
        transfer = StreamingTransfer('b2',target)
        with patch.object(StreamingTransfer,'verify',side_effect=NetworkPolicyHold('held')):
            with self.assertRaises(NetworkPolicyHold): promote_stream(self.root,transfer,bundle,'dest/episode.cbz')
        self.assertTrue(receipt.is_file())
        StreamingTransfer.files[('b2',remote)] = b'someone else'
        self.assertFalse(promote_stream(self.root,transfer,bundle,'dest/episode.cbz'))
        self.assertFalse(receipt.exists())
        self.assertNotIn(('b2',remote),StreamingTransfer.files)
        self.assertNotIn(('b2','dest/episode.cbz'),StreamingTransfer.files)

    def test_streaming_starts_only_for_an_enabled_target_and_a_missing_comic_archive(self):
        streams = self.streams
        with closing(PublicationStore(self.root)) as store:
            folder = store.series_folder(self.series)
            archive = store.episode_path(self.series,self.episode,folder)
            self.assertIsNone(StreamingEpisode.create(store,self.series,replace(self.episode,content_type='novel',pages=(),text_html='<p>Synthetic text.</p>'),archive))
            archive.parent.mkdir(parents=True,exist_ok=True); archive.write_bytes(b'existing')
            self.assertIsNone(StreamingEpisode.create(store,self.series,self.episode,archive))
            self.assertEqual(archive.read_bytes(),b'existing')
        cloud.enable(self.root,'b2',False)
        archive.unlink()
        with closing(PublicationStore(self.root)) as store:
            self.assertIsNone(StreamingEpisode.create(store,self.series,self.episode,archive))
        self.assertEqual(list(streams.glob('*.json')),[])

    def test_cancelled_assembly_leaves_no_archive_and_refuses_the_episode(self):
        with closing(PublicationStore(self.root)) as store:
            archive, stream = self.stream_for(store,self.episode)
            stream.close()
            self.assertFalse(stream.thread.is_alive())
            with self.assertRaises(ArchiveError): stream.add_page(self.pages[0],png(1))
            with self.assertRaises(ArchiveError): stream.finish({},None)
            self.assertFalse(archive.exists())
            self.assertEqual(json.loads(stream.receipt.read_text())['state'],'streaming')

    def test_cached_page_that_changed_after_download_holds_publication(self):
        one_page = replace(self.episode,pages=(self.pages[0],))
        with closing(PublicationStore(self.root)) as store:
            archive, stream = self.stream_for(store,one_page)
            try:
                (store.cache/digest(png(1))).write_bytes(b'tampered')
                stream.add_page(self.pages[0],png(1))
                with self.assertRaisesRegex(ArchiveError,'Cached streaming page changed'): stream.finish({},None)
            finally: stream.close()
            self.assertFalse(any(b'tampered' in value for value in StreamingTransfer.files.values()))
            self.assertFalse(archive.exists())
            self.assertEqual(json.loads(stream.receipt.read_text())['state'],'streaming')

    def test_assembled_archive_that_disagrees_with_its_metadata_is_never_published_or_marked_uploaded(self):
        one_page = replace(self.episode,pages=(self.pages[0],))
        wrong = {'format':'cbz','page_count':1,'content_hashes':[{'file':'0001.png','size':1,'sha256':'0'*64}]}
        with closing(PublicationStore(self.root)) as store:
            archive, stream = self.stream_for(store,one_page)
            try:
                (store.cache/digest(png(1))).write_bytes(png(1))
                stream.add_page(self.pages[0],png(1))
                with self.assertRaises(ArchiveError): stream.finish(wrong,None)
            finally: stream.close()
            self.assertFalse(archive.exists())
            self.assertEqual(json.loads(stream.receipt.read_text())['state'],'streaming')

    def test_planted_temporary_symlink_is_refused_without_writing_through_it(self):
        outside = self.base/'outside'; outside.write_bytes(b'keep')
        with closing(PublicationStore(self.root)) as store:
            folder = store.series_folder(self.series)
            archive = store.episode_path(self.series,self.episode,folder)
            archive.parent.mkdir(parents=True,exist_ok=True)
            archive.with_name(archive.name+'.tmp').symlink_to(outside)
            with self.assertRaises(ArchiveError): StreamingEpisode.create(store,self.series,self.episode,archive)
        self.assertEqual(outside.read_bytes(),b'keep')

    def test_midstream_upload_errors_fall_back_to_the_verified_local_copy(self):
        for index, mode in enumerate(('write-error','finish-error')):
            with self.subTest(mode=mode):
                StreamingTransfer.pipe_failure = mode
                episode = Episode(str(90+index),'42','New '+mode,3+index,self.pages,'tapas_free')
                self.publish((episode,),MemoryAdapter())
                self.assertEqual(StreamingTransfer.copies.count('.cbz'),index+1)
                self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))
                self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],index+1)
                self.assertEqual(list(self.streams.glob('*.json')),[])

    def test_network_policy_hold_while_streaming_stops_publication_and_the_retry_recovers(self):
        streams = self.streams
        for mode in ('start-hold','write-hold','finish-hold'):
            with self.subTest(mode=mode):
                StreamingTransfer.pipe_failure = mode
                with self.assertRaises(NetworkPolicyHold): self.publish((self.episode,),MemoryAdapter())
                self.assertEqual([json.loads(p.read_text())['state'] for p in streams.glob('*.json')],['streaming'])
                self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],0)
        StreamingTransfer.pipe_failure = None
        self.publish((self.episode,),MemoryAdapter())
        self.assertEqual(cloud.status(self.root)['targets'][0]['verified'],1)
        self.assertNotIn('.cbz',StreamingTransfer.copies)
        self.assertFalse(any('/.staging/' in p for _,p in StreamingTransfer.files))
        self.assertEqual(list(streams.glob('*.json')),[])


class B2StreamTests(unittest.TestCase):
    """B2's trial and its staging cleanup. New episodes go to B2 from memory instead (B2DirectTests)."""
    publish = CloudStreamTests.publish
    setUp = CloudStreamTests.connect

    def target(self):
        return next(t for t in cloud.status(self.root)['targets'] if t['provider'] == 'b2')

    def test_changed_connection_preserves_old_stage_without_deleting_remote_bytes(self):
        with closing(PublicationStore(self.root)) as store:
            folder = store.series_folder(self.series)
            stream = StreamingEpisode.create(store, self.series, self.episode, store.episode_path(self.series, self.episode, folder))
            stream.close(); record = json.loads(stream.receipt.read_text())
        StreamingTransfer.files[('b2', record['remote_path'])] = b'old account'
        replacement = self.base/'replacement.conf'; replacement.write_text('[b2]\ntype=b2\n'); replacement.chmod(0o600)
        cloud.configure_target(self.root, 'b2', config=replacement)
        transfer = StreamingTransfer('b2', {})
        with self.assertRaises(ArchiveError): cleanup_stage(self.root, transfer, record['episode_id'])
        self.assertEqual(StreamingTransfer.files[('b2', record['remote_path'])], b'old account')
        self.assertTrue(stream.receipt.exists())

    def test_two_episode_stream_pilot_deletes_exact_test_copies_and_keeps_originals(self):
        cloud.enable(self.root, 'b2', False)
        self.publish((self.episode,), MemoryAdapter())
        originals = {p: p.read_bytes() for p in self.root.glob('*/*.cbz')}
        StreamingTransfer.files[('b2', 'unrelated/keep')] = b'keep'
        StreamingTransfer.copies = []
        result = cloud.smoke_test(self.root, 'b2', episodes=2, stream=True)
        self.assertEqual(result['streamed_episodes'], 2)
        self.assertTrue(result['test_files_deleted'])
        self.assertNotIn('.cbz', StreamingTransfer.copies)
        self.assertEqual(StreamingTransfer.files, {('b2', 'unrelated/keep'): b'keep'})
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(self.target()['enabled'])

    def test_novel_only_library_can_complete_the_same_bounded_connection_pilot(self):
        root = self.base/'novel-library'
        series = Series('43', 'Novel', source='tapas')
        episodes = tuple(Episode(str(i), '43', 'Chapter', i, (), 'tapas_free',
                                 content_type='novel', text_html='<p>Synthetic chapter.</p>') for i in (82, 83))
        with closing(PublicationStore(root)) as store:
            store.archive(Manifest(series, episodes), MemoryAdapter(), progress=lambda _: None)
        originals = {p: p.read_bytes() for p in root.glob('*/*.epub')}
        self.assertEqual(len(originals), 2)
        cloud.configure_target(root, 'b2', config=self.config)
        StreamingTransfer.copies = []
        result = cloud.smoke_test(root, 'b2', episodes=2, stream=True)
        self.assertEqual(result['streamed_episodes'], 2)
        self.assertTrue(result['test_files_deleted'])
        self.assertNotIn('.epub', StreamingTransfer.copies)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(StreamingTransfer.files)


class Crash(BaseException):
    """The app closing in the middle of an upload."""


class B2DirectTests(unittest.TestCase):
    provider = 'b2'
    publish = CloudStreamTests.publish
    setUp = CloudStreamTests.connect
    target = B2StreamTests.target

    def b2(self):
        return {path: data for (provider, path), data in StreamingTransfer.files.items() if provider == 'b2'}

    def intents(self):
        return list((self.root/'.archive-state/cloud/direct/b2').glob('*.json'))

    def test_wrong_remote_checksums_set_episodes_aside_for_retry_until_one_run_has_too_many(self):
        episodes = tuple(replace(self.episode, id=str(84 + i), order=4 + i) for i in range(3))
        with patch.object(StreamingTransfer, 'sha1s', return_value={'episode.cbz':'0'*40}):
            with self.assertRaises(CloudTransferError) as caught:
                self.publish(episodes, MemoryAdapter())
        self.assertEqual(safe_code(caught.exception), 'cloud_verification')
        # Two were saved here and the run went on; the third, a pattern rather than one episode, paused it.
        self.assertEqual((self.target()['verified'], self.target()['failed']), (0, 3))
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))), 4)
        self.assertTrue(self.intents())
        cloud.upload_pending(self.root)
        self.assertEqual((self.target()['verified'], self.target()['failed']), (3, 0))
        self.assertFalse(self.intents())

    def test_one_unconfirmed_episode_is_saved_here_and_the_next_still_goes_to_b2(self):
        real, calls = StreamingTransfer.sha1s, []
        def first_wrong(transfer, path):
            calls.append(path)
            listed = real(transfer, path)
            return listed | {'episode.cbz':'0'*40} if len(calls) == 1 else listed
        episodes = tuple(replace(self.episode, id=str(84 + i), order=4 + i) for i in range(2))
        with patch.object(StreamingTransfer, 'sha1s', first_wrong):
            self.publish(episodes, MemoryAdapter())
        self.assertEqual((self.target()['verified'], self.target()['failed']), (1, 1))
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))), 2)

    def test_a_file_b2_lists_but_was_not_sent_fails_the_check(self):
        real = StreamingTransfer.sha1s
        with patch.object(StreamingTransfer, 'sha1s', lambda transfer, path: real(transfer, path) | {'stray.bin':'0'*40}):
            self.publish((self.episode,), MemoryAdapter())
        self.assertEqual((self.target()['verified'], self.target()['failed']), (0, 1))

    def test_an_upload_b2_lists_no_sha1_for_is_read_back_instead(self):
        with patch.object(StreamingTransfer, 'sha1s', return_value={}):
            self.publish((self.episode,), MemoryAdapter())
        self.assertEqual((self.target()['verified'], self.target()['failed']), (1, 0))
        self.assertFalse(self.intents())
        changed = replace(self.episode, id='84', order=4)
        with patch.object(StreamingTransfer, 'sha1s', return_value={}), \
                patch.object(StreamingTransfer, 'verify', side_effect=CloudTransferError('cloud_verification')):
            self.publish((changed,), MemoryAdapter())
        self.assertEqual((self.target()['verified'], self.target()['failed']), (1, 1))

    def test_only_an_unconfirmed_episode_saved_here_lets_the_run_go_on(self):
        # Anything else stops every upload alike, and one not saved here must be downloaded again.
        for index, (failure, keep) in enumerate(((CloudTransferError('cloud_cap'), None),
                                                 (CloudTransferError('cloud_verification'), OSError('disk full'))), 1):
            with self.subTest(failure=failure.cloud_code):
                episode = replace(self.episode, id=str(83 + index), order=3 + index)
                with patch.object(StreamingTransfer, 'upload_bytes', side_effect=failure), \
                        patch('comic_archive.cloud.keep_here', side_effect=keep):
                    with self.assertRaises(CloudTransferError) as caught:
                        self.publish((episode,), MemoryAdapter())
                self.assertEqual(safe_code(caught.exception), failure.cloud_code)

    def test_a_failed_local_fallback_keeps_the_recovery_intent_without_claiming_a_saved_copy(self):
        from comic_archive.archive import sidecar_path
        with patch.object(StreamingTransfer, 'upload_bytes', side_effect=CloudTransferError('cloud_cap')):
            with patch('comic_archive.cloud.keep_here', side_effect=OSError('disk full')):
                with self.assertRaises(CloudTransferError) as caught:
                    self.publish((self.episode,), MemoryAdapter())
        self.assertEqual(safe_code(caught.exception), 'cloud_cap')
        self.assertEqual(self.target()['verified'], 0)
        self.assertTrue(self.intents())
        for path in self.root.glob('*/*.json'):
            if path.name != 'series.json' and json.loads(path.read_text()).get('episode_number') == self.episode.order:
                self.assertFalse(cloud.offloaded(self.root, sidecar_path(path).with_suffix('.cbz')))
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=lambda page: png(page.order))
        self.publish((self.episode,), adapter)
        self.assertEqual(self.target()['verified'], 1)
        self.assertFalse(self.intents())

    def test_a_direct_upload_network_hold_retains_its_type_after_saving_the_local_fallback(self):
        with patch.object(StreamingTransfer, 'upload_bytes', side_effect=NetworkPolicyHold('held')):
            with self.assertRaises(NetworkPolicyHold):
                self.publish((self.episode,), MemoryAdapter())
        self.assertEqual((self.target()['verified'], self.target()['failed']), (0, 1))
        self.assertEqual(len(list(self.root.glob('*/*.cbz'))), 2)
        self.assertTrue(self.intents())
        cloud.upload_pending(self.root)
        self.assertEqual(self.target()['verified'], 1)

    def test_reader_publication_follows_direct_receipt_history_and_intent_retirement(self):
        from comic_archive import reader_catalogue
        actual = reader_catalogue.publish
        observations = []
        def observe(root, transfer, record, library, target, **options):
            self.assertFalse(self.intents())
            self.assertEqual(record['state'], 'verified')
            self.assertTrue(list((root / '.archive-state/cloud/history/b2').glob('*/*.json')))
            observations.append(actual(root, transfer, record, library, target, **options))
            return observations[-1]
        with patch.object(reader_catalogue, 'publish', observe):
            self.publish((self.episode,), MemoryAdapter())
        self.assertEqual(observations, [True])
        self.assertTrue(any('/.reader/' in name for name in self.b2()))
        self.assertEqual(self.target()['verified'], 1)

    def test_new_episode_goes_to_b2_from_memory_and_is_never_saved_here(self):
        from comic_archive.archive import verify_episode_contents
        import io
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=lambda page: png(page.order + 2))
        folder = self.publish((self.episode,), adapter)
        self.assertEqual(len(episode_archives(folder)), 1)  # Only the seed is here.
        self.assertFalse(list(folder.glob('*.tmp')))
        for order in (3, 4):
            self.assertFalse((self.root/'.archive-cache'/hashlib.sha256(png(order)).hexdigest()).exists())
        self.assertFalse(StreamingTransfer.started.is_set())
        self.assertNotIn('.cbz', StreamingTransfer.copies)
        files = self.b2()
        cbz = [path for path in files if path.endswith('/episode.cbz')]
        self.assertEqual(len(cbz), 1)
        prefix = cbz[0].rsplit('/', 1)[0]
        self.assertEqual({path.rsplit('/', 1)[1] for path in files if path.startswith(prefix + '/')},
                         {'episode.cbz', 'episode.json', 'series.json'})
        metadata = json.loads(files[prefix + '/episode.json'])
        self.assertEqual(verify_episode_contents(io.BytesIO(files[cbz[0]]), metadata), [])
        self.assertEqual(self.target()['verified'], 1)
        sidecar = next(path for path in folder.glob('*.json') if path.name != 'series.json'
                       and json.loads(path.read_text())['export_id'] == metadata['export_id'])
        self.assertTrue(cloud.offloaded(self.root, sidecar.with_suffix('.cbz')))
        self.assertFalse(self.intents())
        # Kept in B2, it is not downloaded again.
        again = MemoryAdapter(); again.retrieve = Mock()
        self.publish((self.episode,), again)
        again.retrieve.assert_not_called()

    def test_each_page_is_let_go_once_its_last_use_is_in_the_cbz(self):
        from comic_archive import publication
        from comic_archive.archive import verify_episode_contents
        import io
        pages = tuple(Page(order, f'https://cdn.test/repeat{order}', False) for order in (1, 2, 3))
        repeat = Episode('83', '42', 'Repeat', 4, pages, 'tapas_free')  # Its first and last pages are the same image.
        held, episodes, real = [], [], publication.cbz_bytes
        class Held(publication.DirectEpisode):
            def __init__(self, archive):
                super().__init__(archive); episodes.append(self)
        def build(metadata, open_page, check=None):
            def counted(page):
                source = open_page(page); held.append(len(episodes[-1].pages)); return source
            return real(metadata, counted, check)
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=lambda page: png(1 if page.order == 3 else page.order))
        with patch.object(publication, 'DirectEpisode', Held), patch.object(publication, 'cbz_bytes', build):
            self.publish((repeat,), adapter)
        self.assertEqual(held, [2, 1, 0])
        files = self.b2()
        cbz = next(path for path in files if path.endswith('/episode.cbz'))
        metadata = json.loads(files[cbz.rsplit('/', 1)[0] + '/episode.json'])
        self.assertEqual(metadata['page_count'], 3)
        self.assertEqual(verify_episode_contents(io.BytesIO(files[cbz]), metadata), [])

    def test_failed_upload_keeps_the_episode_here_for_the_retry_button(self):
        StreamingTransfer.fail_upload = True
        with self.assertRaises(ArchiveError): self.publish((self.episode,), MemoryAdapter())
        folder = next(self.root.glob('Synthetic*'))
        self.assertEqual(len(episode_archives(folder)), 2)
        self.assertEqual(self.target()['failed'], 1)
        StreamingTransfer.fail_upload = False
        cloud.upload_pending(self.root)
        self.assertIn('.cbz', StreamingTransfer.copies)
        self.assertEqual(len(episode_archives(folder)), 1)
        self.assertEqual(self.target()['verified'], 1)
        self.assertFalse(self.intents())

    def crash_after_the_cbz(self):
        real = StreamingTransfer.upload_bytes
        def upload(transfer, data, destination):
            if not destination.endswith('/episode.cbz'): raise Crash()
            real(transfer, data, destination)
        with patch.object(StreamingTransfer, 'upload_bytes', upload), self.assertRaises(Crash):
            self.publish((self.episode,), MemoryAdapter())

    def test_next_episode_downloads_while_the_previous_one_uploads(self):
        following = Episode('82', '42', 'Next', 3, (Page(1, 'https://cdn.test/next', False),), 'tapas_free')
        downloading = threading.Event()
        real = StreamingTransfer.upload_bytes
        def upload(transfer, data, destination):
            if destination.endswith('/episode.cbz') and not downloading.is_set():
                self.assertTrue(downloading.wait(5))  # The first upload is still going.
            real(transfer, data, destination)
        def retrieve(page):
            if page.location.endswith('/next'): downloading.set()
            return png(page.order)
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=retrieve)
        with patch.object(StreamingTransfer, 'upload_bytes', upload):
            folder = self.publish((self.episode, following), adapter)
        self.assertEqual(self.target()['verified'], 2)
        self.assertEqual(len(episode_archives(folder)), 1)
        self.assertFalse(self.intents())

    def test_failed_upload_saves_the_episode_waiting_behind_it_too(self):
        following = Episode('82', '42', 'Next', 3, (Page(1, 'https://cdn.test/next', False),), 'tapas_free')
        real = StreamingTransfer.upload_bytes
        sent = []
        def upload(transfer, data, destination):
            if destination.endswith('/episode.cbz') and not sent:
                sent.append(destination); raise CloudTransferError('cloud_cap')
            real(transfer, data, destination)
        with patch.object(StreamingTransfer, 'upload_bytes', upload), self.assertRaises(CloudTransferError) as caught:
            self.publish((self.episode, following), MemoryAdapter())
        self.assertEqual(safe_code(caught.exception), 'cloud_cap')
        folder = next(self.root.glob('Synthetic*'))
        self.assertEqual(len(episode_archives(folder)), 3)
        self.assertEqual((self.target()['failed'], self.target()['pending']), (1, 1))
        cloud.upload_pending(self.root)
        self.assertEqual(self.target()['verified'], 2)
        self.assertEqual(len(episode_archives(folder)), 1)

    def test_interrupted_upload_is_downloaded_again_and_its_leftovers_deleted(self):
        self.crash_after_the_cbz()
        left = self.b2()
        self.assertTrue(any(path.endswith('/episode.cbz') for path in left))
        self.assertEqual(len(self.intents()), 1)
        StreamingTransfer.files[('b2', 'unrelated/keep')] = b'keep'
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=lambda page: png(page.order))
        self.publish((self.episode,), adapter)
        self.assertEqual(adapter.retrieve.call_count, 2)  # Nothing of it was saved here.
        files = self.b2()
        self.assertFalse(set(left) & set(files))
        self.assertEqual(files['unrelated/keep'], b'keep')
        self.assertEqual(self.target()['verified'], 1)
        self.assertFalse(self.intents())

    def sent_archive(self):
        """The episode B2 alone keeps, by the sidecar left here."""
        files = self.b2()
        cbz = next(path for path in files if path.endswith('/episode.cbz'))
        export_id = json.loads(files[cbz.rsplit('/', 1)[0] + '/episode.json'])['export_id']
        sidecar = next(path for path in self.root.glob('*/*.json') if path.name != 'series.json'
                       and json.loads(path.read_text())['export_id'] == export_id)
        return sidecar.with_suffix('.cbz')

    def test_an_upload_recorded_in_either_record_alone_is_never_taken_for_leftovers(self):
        # Stopped after the outbox was written, before the history was.
        with patch('comic_archive.cloud_history.retain', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.publish((self.episode,), MemoryAdapter())
        sent = self.b2(); archive = self.sent_archive()
        self.assertEqual(len(self.intents()), 1)
        cloud.cleanup_direct(self.root)
        self.assertEqual(self.b2(), sent); self.assertFalse(self.intents())
        self.assertTrue(cloud.offloaded(self.root, archive))
        # The history alone: an upload stopped before its outbox record was written, as earlier versions wrote them.
        cloud.upload_pending(self.root)  # The history is written as the episode's upload is confirmed.
        history = json.loads(next((self.root/'.archive-state/cloud/history/b2').glob('*/*.json')).read_text())
        intent = self.root/'.archive-state/cloud/direct/b2'/(history['episode_id'] + '.json')
        cloud._write(intent, {'schema_version': 1, 'library_id': history['library_id'], 'target_id': history['target_id'],
                              'episode_id': history['episode_id'], 'names': [item['name'] for item in history['files']],
                              'prefix': f"Comic-Archive/{history['library_id']}/{history['series_id']}/{history['episode_id']}/{history['version']}"})
        (self.root/'.archive-state/cloud/outbox/b2'/(history['episode_id'] + '.json')).unlink()
        cloud.cleanup_direct(self.root)
        self.assertEqual(self.b2(), sent); self.assertFalse(self.intents())
        self.assertTrue(cloud.offloaded(self.root, archive))

    def test_a_damaged_recovery_record_is_set_aside_and_never_stops_later_uploads(self):
        folder = self.root/'.archive-state/cloud/direct/b2'; folder.mkdir(parents=True, exist_ok=True)
        damaged = folder/(str(uuid.uuid4()) + '.json'); damaged.write_text('{broken'); damaged.chmod(0o600)
        StreamingTransfer.files[('b2', 'unrelated/keep')] = b'keep'
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=lambda page: png(page.order))
        folder_of_episodes = self.publish((self.episode,), adapter)
        self.assertEqual((self.target()['verified'], self.target()['failed']), (1, 0))
        self.assertEqual(len(episode_archives(folder_of_episodes)), 1)  # Only the seed is here.
        self.assertFalse(self.intents()); self.assertFalse(damaged.exists())
        self.assertEqual([path.read_text() for path in folder.glob(damaged.stem + '.damaged-*')], ['{broken'])
        self.assertEqual(self.b2()['unrelated/keep'], b'keep')

    def test_records_for_other_connections_never_crowd_out_this_ones_leftovers(self):
        self.crash_after_the_cbz()
        left = self.b2(); ours = self.intents()
        value = cloud.settings(self.root); folder = self.root/'.archive-state/cloud/direct/b2'
        for number in range(25):
            episode = f'00000000-0000-0000-0000-{number:012d}'
            cloud._write(folder/(episode + '.json'), {'schema_version': 1, 'library_id': value['library_id'],
                         'target_id': str(uuid.uuid4()), 'episode_id': episode, 'names': ['episode.cbz'],
                         'prefix': f"Comic-Archive/{value['library_id']}/{uuid.uuid4()}/{episode}/{'0'*64}"})
        cloud.cleanup_direct(self.root)
        self.assertFalse(set(left) & set(self.b2()))
        self.assertEqual(len(self.intents()), 25); self.assertFalse(any(path.exists() for path in ours))

    def test_leftovers_recorded_for_an_earlier_connection_are_kept(self):
        self.crash_after_the_cbz()
        left = self.b2()
        replacement = self.base/'replacement.conf'; replacement.write_text('[b2]\ntype=b2\n'); replacement.chmod(0o600)
        cloud.configure_target(self.root, 'b2', config=replacement)
        cloud.upload_pending(self.root)
        self.assertEqual(self.b2(), left)
        self.assertEqual(len(self.intents()), 1)

    def test_paused_episode_keeps_its_pages_and_continues_without_downloading_them_again(self):
        from comic_archive.cancellation import RunCancelled
        def pause_at_the_second_page(page):
            if page.order == 2: raise RunCancelled('Paused.')
            return png(page.order + 2)  # Images no other episode here has.
        adapter = MemoryAdapter(); adapter.retrieve = Mock(side_effect=pause_at_the_second_page)
        with self.assertRaises(RunCancelled): self.publish((self.episode,), adapter)
        first = self.root/'.archive-cache'/hashlib.sha256(png(3)).hexdigest()
        self.assertTrue(first.is_file())  # Kept here only because the episode stopped partway.
        self.assertFalse([path for path in self.b2() if path.endswith('/episode.cbz')])
        again = MemoryAdapter(); again.retrieve = Mock(side_effect=lambda page: png(page.order + 2))
        folder = self.publish((self.episode,), again)
        self.assertEqual([call.args[0].order for call in again.retrieve.call_args_list], [2])
        self.assertEqual(len([path for path in self.b2() if path.endswith('/episode.cbz')]), 1)
        self.assertEqual(self.target()['verified'], 1)
        self.assertEqual(len(episode_archives(folder)), 1)  # Only the seed is here.
        self.assertFalse(first.exists())  # Let go once the episode is in B2.

    def test_rented_episode_is_saved_here_before_it_goes_to_b2(self):
        rented = replace(self.episode, access_expires_at='2026-12-31T00:00:00Z')
        saved = []
        real = cloud.queue_episode
        def queued(root, archive):
            saved.append(archive.is_file()); return real(root, archive)
        with patch.object(cloud, 'queue_episode', queued):
            folder = self.publish((rented,), MemoryAdapter())
        self.assertTrue(saved and all(saved))
        self.assertIn('.cbz', StreamingTransfer.copies)  # Uploaded from the saved file, not from memory.
        self.assertEqual(self.target()['verified'], 1)
        self.assertEqual(len(episode_archives(folder)), 1)  # Removed once the upload is checked.
        self.assertFalse(self.intents())

    def test_episode_too_big_to_hold_in_memory_is_saved_here_then_uploaded(self):
        with patch('comic_archive.publication.DIRECT_MEMORY', 1):
            folder = self.publish((self.episode,), MemoryAdapter())
        self.assertIn('.cbz', StreamingTransfer.copies)
        self.assertEqual(self.target()['verified'], 1)
        self.assertEqual(len(episode_archives(folder)), 1)
        self.assertFalse(self.intents())

    def test_b2_compares_its_own_sha1_and_downloads_only_when_it_has_none(self):
        source = self.base/'episode.cbz'; source.write_bytes(b'episode bytes')
        sha256 = hashlib.sha256(b'episode bytes').hexdigest(); sha1 = hashlib.sha1(b'episode bytes').hexdigest()
        transfer = object.__new__(RealTransfer); transfer.provider = 'b2'
        transfer.target = {'remote': 'b2'}; transfer.bucket = 'bucket'
        transfer.run = Mock(return_value=sha1.encode() + b'  episode.cbz\n')
        transfer.verify('own/episode.cbz', sha256, source=source)
        transfer.run.assert_called_once()
        self.assertNotIn('--download', transfer.run.call_args.args)
        transfer.run = Mock(return_value=b'b'*40 + b'  episode.cbz\n')
        with self.assertRaises(ArchiveError): transfer.verify('own/episode.cbz', sha256, source=source)
        # A file sent in parts may have no SHA-1 at B2: its bytes are read back instead.
        transfer.run = Mock(side_effect=[b' '*40 + b'  episode.cbz\n', sha256.encode() + b'  episode.cbz'])
        transfer.verify('own/episode.cbz', sha256, source=source)
        self.assertIn('--download', transfer.run.call_args.args)
        transfer.full_readback = True; transfer.run = Mock(return_value=sha256.encode())
        transfer.verify('own/episode.cbz', sha256, source=source)
        self.assertIn('--download', transfer.run.call_args.args)

    def test_known_size_cutoff_survives_rclone_keeping_three_decimals_of_it(self):
        # rclone 1.75 read 10685131B back as 10.190Mi, 10684989 bytes, and sent that episode in parts.
        for size in (0, 1, 1023, 1024**2 - 1, 1024**2, 10685130, 256 * 1024**2):
            with self.subTest(size=size):
                whole = int(held_cutoff(size).removesuffix('Mi'))
                self.assertEqual(held_cutoff(size), f'{whole}Mi')
                self.assertLess(size, whole * 1024**2)
                self.assertLessEqual(whole * 1024**2, size + 1024**2)

    def test_known_size_pipe_lets_rclone_hold_the_whole_file_for_its_sha1(self):
        self.transfer_patch.stop()
        script = helper_program(self.base/'rclone-fixture', 'import sys,pathlib,json\np=pathlib.Path(sys.argv[sys.argv.index("--config")+1]).parent/"received"\np.write_bytes(sys.stdin.buffer.read())\np.with_suffix(".args").write_text(json.dumps(sys.argv))\n')
        transfer = Mock(binary=str(script), config=self.config, provider='b2'); transfer.guard = Mock()
        transfer.remote = lambda path: 'b2:' + path
        data = b'content' * 400000
        RealTransfer.upload_bytes(transfer, data, 'Comic-Archive/test/episode.cbz')
        self.assertEqual((self.base/'received').read_bytes(), data)
        arguments = json.loads((self.base/'received.args').read_text())
        self.assertEqual(arguments[arguments.index('--streaming-upload-cutoff') + 1], held_cutoff(len(data)))
        self.assertEqual(arguments[-2:], ['rcat', 'b2:Comic-Archive/test/episode.cbz'])
