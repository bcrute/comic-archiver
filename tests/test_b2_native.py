"""Closed B2 wire journeys through HTTPX and the real native/shared cloud code.

The provider is in memory. No key, bucket, billing operation or source-site request
is real. It checks uploaded bytes, tokens and object identity rather than accepting
arbitrary transfer method calls.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import unquote
import uuid
from unittest.mock import patch

import httpx

from comic_archive import b2_native, cloud, cloud_runtime
from comic_archive.b2_native import NativeB2Transfer, PART
from comic_archive.cloud_auth import SignIn
from comic_archive.cloud_errors import CloudTransferError
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore
from contextlib import closing
from test_archive import MemoryAdapter


class B2Wire:
    def __init__(self):
        self.bucket = {'id': 'fixtureBucket', 'name': 'fixture-library'}
        self.account = {'accountId': 'fixtureAccount', 'authorizationToken': 'FIXTURE_TOKEN',
            'apiInfo': {'storageApi': {'apiUrl': 'https://api001.backblazeb2.com',
                'downloadUrl': 'https://f001.backblazeb2.com', 'allowed': {'buckets': [self.bucket],
                'capabilities': ['listBuckets', 'listFiles', 'readFiles', 'writeFiles', 'deleteFiles'], 'namePrefix': None}}}}
        self.files = {}
        self.large = {}
        self.operations = []
        self.uploads = 0
        self.canceled = []
        self.endpoint = 'https://pod-fixture.backblaze.com/b2api/v4/b2_upload_file/fixture'
        self.destination_bucket = self.bucket['id']
        self.destination_file = None
        self.corrupt_digest = False
        self.on_upload = None
        self.on_authorize = None

    def add(self, name, data, sha1=None):
        item = {'bucketId': self.bucket['id'], 'fileId': 'fixture_' + uuid.uuid4().hex,
            'fileName': name, 'action': 'upload', 'contentLength': len(data),
            'contentSha1': sha1 or hashlib.sha1(data, usedforsecurity=False).hexdigest()}
        self.files[name] = (item, bytes(data))
        return copy.deepcopy(item)

    def request(self, request):
        operation = request.url.path.split('/')[3] if request.url.path.startswith('/b2api/v4/') else ''
        self.operations.append((operation, str(request.url.host)))
        if 'cookie' in request.headers:
            raise AssertionError('B2 must not receive a site cookie.')
        if operation == 'b2_authorize_account':
            if request.url.host != 'api.backblazeb2.com' or not request.headers['Authorization'].startswith('Basic '):
                raise AssertionError('Unexpected credential destination.')
            if self.on_authorize:
                self.on_authorize()
            return httpx.Response(200, json=self.account)
        if operation in {'b2_upload_file', 'b2_upload_part'}:
            if request.url.host != 'pod-fixture.backblaze.com' or request.headers['Authorization'] != 'FIXTURE_UPLOAD':
                raise AssertionError('Unexpected upload credentials or host.')
            data = request.read()
            if int(request.headers['Content-Length']) != len(data):
                raise AssertionError('Upload length differs from its header.')
            sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            if request.headers['X-Bz-Content-Sha1'] != sha1:
                raise AssertionError('Upload bytes differ from their checksum.')
            self.uploads += 1
            if self.on_upload:
                self.on_upload()
            if operation == 'b2_upload_file':
                answer = self.add(unquote(request.headers['X-Bz-File-Name']), data)
                if self.corrupt_digest:
                    answer['contentSha1'] = '0' * 40
            else:
                file_id = request.url.path.rsplit('/', 1)[-1]
                number = int(request.headers['X-Bz-Part-Number'])
                self.large[file_id]['parts'][number] = data
                answer = {'fileId': file_id, 'partNumber': number, 'contentLength': len(data), 'contentSha1': sha1}
            return httpx.Response(200, json=answer)
        if request.headers['Authorization'] != 'FIXTURE_TOKEN':
            raise AssertionError('Wrong account token.')
        if operation == 'b2_download_file_by_id':
            if request.url.host != 'f001.backblazeb2.com':
                raise AssertionError('Unexpected download host.')
            file_id = request.url.params['fileId']
            item, data = next((v for v in self.files.values() if v[0]['fileId'] == file_id))
            return httpx.Response(200, content=data, headers={'X-Bz-File-Id': item['fileId']})
        if request.url.host != 'api001.backblazeb2.com':
            raise AssertionError('Token sent outside the authorized API host.')
        values = json.loads(request.content)
        if operation == 'b2_list_buckets':
            answer = {'buckets': [{'bucketId': self.bucket['id'], 'bucketName': self.bucket['name'], 'bucketType': 'allPrivate'}]}
        elif operation in {'b2_list_file_names', 'b2_list_file_versions'}:
            prefix = values.get('prefix', '')
            files = [v[0] for k, v in sorted(self.files.items()) if k.startswith(prefix)]
            answer = {'files': files, 'nextFileName': None, 'nextFileId': None}
        elif operation == 'b2_list_unfinished_large_files':
            files = [{'fileId': key, 'fileName': v['name'], 'bucketId': self.bucket['id'], 'action': 'start'}
                     for key, v in self.large.items() if v['name'].startswith(values['namePrefix'])]
            answer = {'files': files, 'nextFileId': None}
        elif operation == 'b2_get_upload_url':
            answer = {'bucketId': self.destination_bucket, 'uploadUrl': self.endpoint, 'authorizationToken': 'FIXTURE_UPLOAD'}
        elif operation == 'b2_start_large_file':
            file_id = 'fixture_' + uuid.uuid4().hex
            self.large[file_id] = {'name': values['fileName'], 'parts': {}}
            answer = {'fileId': file_id, 'fileName': values['fileName'], 'bucketId': self.bucket['id']}
        elif operation == 'b2_get_upload_part_url':
            file_id = values['fileId']
            answer = {'fileId': self.destination_file or file_id, 'authorizationToken': 'FIXTURE_UPLOAD',
                'uploadUrl': 'https://pod-fixture.backblaze.com/b2api/v4/b2_upload_part/' + file_id}
        elif operation == 'b2_finish_large_file':
            value = self.large.pop(values['fileId'])
            parts = [value['parts'][n] for n in range(1, len(values['partSha1Array']) + 1)]
            if [hashlib.sha1(v, usedforsecurity=False).hexdigest() for v in parts] != values['partSha1Array']:
                raise AssertionError('Multipart completion did not name the received parts.')
            answer = self.add(value['name'], b''.join(parts), sha1='none')
            answer['fileId'] = values['fileId']; self.files[value['name']][0]['fileId'] = values['fileId']
        elif operation == 'b2_cancel_large_file':
            self.large.pop(values['fileId'], None); self.canceled.append(values['fileId']); answer = {}
        elif operation == 'b2_copy_file':
            _, data = next(v for v in self.files.values() if v[0]['fileId'] == values['sourceFileId'])
            answer = self.add(values['fileName'], data)
        elif operation == 'b2_delete_file_version':
            item, _ = self.files[values['fileName']]
            if item['fileId'] != values['fileId']:
                raise AssertionError('Deletion selected another version.')
            del self.files[values['fileName']]; answer = {}
        else:
            raise AssertionError('Unexpected B2 operation: ' + operation)
        return httpx.Response(200, json=answer)


class NativeB2Tests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name) / 'Library'; self.root.mkdir()
        self.server = B2Wire()
        self.real_client = httpx.Client
        self.mock = httpx.MockTransport(self.server.request)
        self.client = lambda **kw: self.real_client(transport=self.mock, **kw)
        self.addCleanup(patch.stopall)
        patch('comic_archive.network.probe', return_value={'connection': 'wifi', 'metered': False}).start()
        self.config = self.root / '.archive-state/cloud/connection-fixture.conf'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('[b2]\ntype = b2\naccount = fixtureKey123\nkey = FIXTURE_KEY123\narchive_bucket = fixture-library\n')
        self.config.chmod(0o600)
        self.target = {'remote': 'b2', 'config': str(self.config), 'target_id': str(uuid.uuid4()), 'enabled': False, 'tested': False}
        self.library_id, self.series_id, self.episode_id = (str(uuid.uuid4()) for _ in range(3))
        self.path = f'Comic-Archive/{self.library_id}/{self.series_id}/{self.episode_id}/{"a" * 64}/episode.cbz'
        self.transfer = NativeB2Transfer(self.root, 'b2', self.target, client_factory=self.client)

    def test_upload_uses_real_wire_checksum_and_full_readback_without_a_process(self):
        data = b'synthetic completed archive'
        source = self.root / 'episode.cbz'; source.write_bytes(data)
        self.transfer.full_readback = True
        with patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            self.transfer.upload(source, self.path, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.server.files[self.path][1], data)
        self.assertIn(('b2_download_file_by_id', 'f001.backblazeb2.com'), self.server.operations)
        self.assertEqual(source.read_bytes(), data)
        popen.assert_not_called(); run.assert_not_called()

    def test_immutable_retry_verifies_existing_bytes_without_new_upload_or_overwrite(self):
        self.transfer.upload_bytes(b'old', self.path)
        self.transfer.upload_bytes(b'old', self.path)
        self.assertEqual(self.server.uploads, 1)
        with self.assertRaises(CloudTransferError): self.transfer.upload_bytes(b'new', self.path)
        self.assertEqual(self.server.files[self.path][1], b'old')
        self.assertEqual(self.server.uploads, 1)

    def test_forged_upload_host_or_bucket_is_refused_before_sending_bytes(self):
        for url in ('https://elsewhere.test/b2api/v4/b2_upload_file/x',
                    'http://pod-fixture.backblaze.com/b2api/v4/b2_upload_file/x',
                    'https://pod-fixture.backblaze.com@elsewhere.test/b2api/v4/b2_upload_file/x'):
            with self.subTest(url=url):
                self.server.endpoint = url
                with self.assertRaises(CloudTransferError): self.transfer.upload_bytes(b'archive', self.path)
        self.server.endpoint = 'https://pod-fixture.backblaze.com/b2api/v4/b2_upload_file/x'
        self.server.destination_bucket = 'not-the-selected-bucket'
        with self.assertRaises(CloudTransferError): self.transfer.upload_bytes(b'archive', self.path)
        self.assertEqual(self.server.uploads, 0)

    def test_upload_digest_mismatch_never_verifies_and_local_original_survives(self):
        source = self.root / 'episode.cbz'; source.write_bytes(b'archive')
        self.server.corrupt_digest = True
        with self.assertRaises(CloudTransferError) as error:
            self.transfer.upload(source, self.path, hashlib.sha256(b'archive').hexdigest())
        self.assertEqual(error.exception.cloud_code, 'cloud_verification')
        self.assertEqual(source.read_bytes(), b'archive')

    def test_stream_has_bounded_parts_verified_completion_and_readback(self):
        data = b'x' * PART + b'last part'
        pipe = self.transfer.start_stream(self.path)
        try:
            pipe.write(data)
            self.assertEqual(len(pipe.buffer), len(b'last part'))
            self.assertEqual(len(pipe.parts), 1)
            pipe.finish(); pipe.finish()
        finally: pipe.close()
        self.assertEqual(self.server.files[self.path][1], data)
        self.transfer.verify(self.path, hashlib.sha256(data).hexdigest())
        self.assertFalse(self.server.large)
        self.assertFalse(self.server.canceled)

    def test_interrupted_stream_cancels_only_its_owned_large_file(self):
        pipe = self.transfer.start_stream(self.path)
        own = pipe.file_id
        other = 'unrelated_large_file'; self.server.large[other] = {'name': self.path + '-other', 'parts': {}}
        pipe.write(b'pending')
        pipe.close(); pipe.close()
        self.assertEqual(self.server.canceled, [own])
        self.assertIn(other, self.server.large)
        self.assertNotIn(self.path, self.server.files)

    def test_wrong_part_destination_is_rejected_before_part_bytes(self):
        pipe = self.transfer.start_stream(self.path)
        self.server.destination_file = 'foreign_file'
        try:
            with self.assertRaises(CloudTransferError): pipe.write(b'x' * PART)
        finally: pipe.close()
        self.assertEqual(self.server.uploads, 0)

    def test_policy_hold_during_upload_does_not_become_success(self):
        self.server.on_upload = lambda: setattr(self.transfer.guard, 'check', lambda: (_ for _ in ()).throw(NetworkPolicyHold('held')))
        with self.assertRaises(NetworkPolicyHold): self.transfer.upload_bytes(b'archive', self.path)

    def test_native_signin_uses_same_private_config_without_downloading_helper(self):
        patch.object(cloud_runtime, '_android', True).start()
        patch('httpx.Client', side_effect=self.client).start()
        signin = SignIn(self.root); self.addCleanup(signin.close)
        with patch('comic_archive.cloud_tools.ensure', side_effect=AssertionError('desktop helper must not run')):
            signin.start('b2', {'key_id': 'fixtureKey123', 'application_key': 'FIXTURE_KEY123'})
            signin._thread.join(5)
        self.assertFalse(signin._thread.is_alive())
        self.assertEqual(signin.status()['status'], 'connected')
        target = cloud.settings(self.root)['targets']['b2']
        self.assertFalse(target['enabled']); self.assertFalse(target['tested'])
        self.assertEqual(Path(target['config']).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('FIXTURE_KEY123', json.dumps(signin.status()))
        self.assertIsInstance(cloud.transfer_for(self.root, 'b2', target), NativeB2Transfer)

    def test_cancelled_native_signin_cannot_install_a_late_authorization(self):
        patch.object(cloud_runtime, '_android', True).start()
        patch('httpx.Client', side_effect=self.client).start()
        entered, release = threading.Event(), threading.Event()
        def wait_for_cancel():
            entered.set()
            if not release.wait(5):
                raise AssertionError('Sign-in fixture was not released.')
        self.server.on_authorize = wait_for_cancel
        signin = SignIn(self.root); self.addCleanup(signin.close)
        signin.start('b2', {'key_id': 'fixtureKey123', 'application_key': 'FIXTURE_KEY123'})
        try:
            self.assertTrue(entered.wait(5))
            signin.cancel()
        finally: release.set()
        signin._thread.join(5)
        self.assertFalse(signin._thread.is_alive())
        self.assertEqual(signin.status()['status'], 'cancelled')
        self.assertIsNone(cloud.settings(self.root))

    def test_stream_part_limit_keeps_an_unverified_intent_instead_of_publishing(self):
        pipe = self.transfer.start_stream(self.path)
        try:
            with patch.object(b2_native, 'MAX_PARTS', 1):
                pipe.write(b'a' * PART)
                with self.assertRaises(CloudTransferError) as error: pipe.write(b'b' * PART)
            self.assertEqual(error.exception.cloud_code, 'cloud_transfer_limit')
            self.assertNotIn(self.path, self.server.files)
        finally: pipe.close()

    def test_cleanup_deletes_exact_recorded_staging_file_and_preserves_siblings(self):
        stage = f'Comic-Archive/{self.library_id}/.staging/{uuid.uuid4()}/episode.cbz'
        self.transfer.upload_bytes(b'staging', stage)
        sibling = stage + '-other'; self.server.add(sibling, b'unrelated')
        patch('httpx.Client', side_effect=self.client).start()
        self.transfer.delete_test_file(stage)
        self.assertNotIn(stage, self.server.files)
        self.assertEqual(self.server.files[sibling][1], b'unrelated')

    def test_stage_promotion_copies_verifies_then_deletes_only_staging(self):
        stage = f'Comic-Archive/{self.library_id}/.staging/{uuid.uuid4()}/episode.cbz'
        self.transfer.upload_bytes(b'staging', stage)
        patch('httpx.Client', side_effect=self.client).start()
        self.transfer.run('moveto', self.transfer.remote(stage), self.transfer.remote(self.path), '--immutable')
        self.assertNotIn(stage, self.server.files)
        self.assertEqual(self.server.files[self.path][1], b'staging')
        operations = [v[0] for v in self.server.operations]
        self.assertLess(operations.index('b2_copy_file'), operations.index('b2_delete_file_version'))

    def test_fixed_remote_facade_refuses_other_remote_and_general_commands(self):
        for args in [('purge', self.transfer.remote(self.path)), ('lsf', 'other:bucket/path', '--files-only')]:
            with self.subTest(args=args), self.assertRaises(ArchiveError): self.transfer.run(*args)
        self.assertFalse(self.server.operations)

    def test_repeated_listing_cursor_is_bounded_without_making_a_thousand_calls(self):
        original = self.server.request
        calls = []
        def request(req):
            if req.url.path.endswith('b2_list_file_names'):
                calls.append(req)
                return httpx.Response(200, json={'files': [], 'nextFileName': self.path + '-cursor'})
            return original(req)
        self.mock = httpx.MockTransport(request)
        with self.assertRaises(CloudTransferError): self.transfer.sha1s(self.path)
        self.assertEqual(len(calls), 2)

    def test_download_authority_is_checked_before_any_transfer_token_is_sent(self):
        self.server.account['apiInfo']['storageApi']['downloadUrl'] = 'https://elsewhere.test'
        with self.assertRaises(CloudTransferError): self.transfer.upload_bytes(b'data', self.path)
        self.assertEqual(self.server.uploads, 0)
        self.assertTrue(all(host in {'api.backblazeb2.com', 'api001.backblazeb2.com'}
                            for _, host in self.server.operations))

    def test_large_copy_uses_contiguous_server_ranges_without_a_large_local_buffer(self):
        # Symbolic large-object metadata exercises the >5GB protocol, not a claim
        # that this test transferred or verified gigabytes of archive content.
        original = self.server.add(self.path, b'symbolic')
        original['contentLength'] = 5_000_000_007
        destination = self.path.rsplit('/', 1)[0] + '/episode.json'
        ranges = []
        handler = self.server.request
        def request(req):
            if req.url.path.endswith('b2_copy_part'):
                value = json.loads(req.content)
                ranges.append(value['range'])
                self.assertEqual(value['sourceFileId'], original['fileId'])
                first, last = map(int, value['range'].removeprefix('bytes=').split('-'))
                size = last - first + 1
                self.server.large[value['largeFileId']]['parts'][value['partNumber']] = size
                return httpx.Response(200, json={'fileId': value['largeFileId'], 'partNumber': value['partNumber'],
                    'contentLength': size, 'contentSha1': 'a' * 40})
            if req.url.path.endswith('b2_finish_large_file'):
                value = json.loads(req.content); large = self.server.large.pop(value['fileId'])
                answer = self.server.add(large['name'], b'', sha1='none')
                answer.update(fileId=value['fileId'], contentLength=sum(large['parts'].values()))
                return httpx.Response(200, json=answer)
            return handler(req)
        self.mock = httpx.MockTransport(request)
        with patch.object(self.transfer, '_chunks', side_effect=AssertionError('No archive buffer for server copy')):
            self.transfer._copy(original, destination)
        self.assertEqual(ranges, ['bytes=0-4999999999', 'bytes=5000000000-5000000006'])
        self.assertFalse(self.server.canceled)
        self.assertFalse(self.server.large)

    def publish(self, key='81', novel=False):
        series = Series('42', 'Synthetic', source='tapas')
        episode = Episode(key, '42', 'Chapter ' + key, int(key),
            () if novel else (Page(1, 'https://cdn.test/a', False),), 'tapas_free',
            content_type='novel' if novel else 'comic', text_html='<p>Synthetic text.</p>' if novel else None)
        with closing(PublicationStore(self.root)) as store:
            folder = store.archive(Manifest(series, (episode,)), MemoryAdapter(), progress=lambda _: None)
            return store.episode_path(series, episode, folder)

    def test_real_core_smoke_checks_cbz_epub_readback_and_exact_cleanup_before_enabling(self):
        first, second = self.publish(), self.publish('82', novel=True)
        originals = {p: p.read_bytes() for p in (first, second)}
        patch.object(cloud_runtime, '_android', True).start()
        patch('httpx.Client', side_effect=self.client).start()
        cloud.configure_target(self.root, 'b2', config=self.config)
        with self.assertRaises(ArchiveError): cloud.enable(self.root, 'b2', True)
        unrelated = 'unrelated/private-file'; self.server.add(unrelated, b'keep')
        result = cloud.smoke_test(self.root, 'b2', episodes=2, stream=True)
        self.assertEqual(result['verified_episodes'], 2)
        self.assertTrue(result['test_files_deleted'])
        self.assertEqual(set(self.server.files), {unrelated})
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertTrue(cloud.settings(self.root)['targets']['b2']['tested'])
        self.assertFalse(cloud.settings(self.root)['targets']['b2']['enabled'])
        cloud.enable(self.root, 'b2', True)
        cloud.queue_episode(self.root, first)
        report = cloud.upload_pending(self.root)
        self.assertEqual(report['uploaded_episodes'], 1)
        self.assertEqual(report['local_archives_deleted'], 1)
        self.assertFalse(first.exists())
        records = list((self.root / '.archive-state/cloud/outbox/b2').glob('*.json'))
        self.assertEqual(len(records), 1)
        receipt = json.loads(records[0].read_text())
        self.assertEqual(receipt['state'], 'verified')
        self.assertTrue(receipt['eviction_requested'])
        archive = receipt['files'][0]
        remote = f'Comic-Archive/{receipt["library_id"]}/{receipt["series_id"]}/{receipt["episode_id"]}/{receipt["version"]}/{archive["name"]}'
        self.assertEqual(self.server.files[remote][1], originals[first])

    def test_real_core_failed_upload_retains_original_and_failed_retry_receipt(self):
        first = self.publish(); original = first.read_bytes()
        patch.object(cloud_runtime, '_android', True).start()
        patch('httpx.Client', side_effect=self.client).start()
        cloud.configure_target(self.root, 'b2', config=self.config)
        cloud.smoke_test(self.root, 'b2', episodes=1)
        cloud.enable(self.root, 'b2', True)
        cloud.queue_episode(self.root, first)
        self.server.corrupt_digest = True
        with self.assertRaises(CloudTransferError): cloud.upload_pending(self.root)
        self.assertEqual(first.read_bytes(), original)
        record = json.loads(next((self.root / '.archive-state/cloud/outbox/b2').glob('*.json')).read_text())
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['error_code'], 'cloud_verification')
