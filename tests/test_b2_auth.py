import configparser
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from support import PRIVATE_FILE, helper_program
from comic_archive import b2_auth, cloud
from comic_archive.cloud_auth import SignIn, validate
from comic_archive.cloud_errors import CloudTransferError
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold


class B2ConnectionTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name) / 'Library'; self.root.mkdir()
        self.credentials = {'key_id':'fixtureKeyID12', 'application_key':'PrivateFixtureKey123'}
        self.cancel = threading.Event()
        self.bucket = {'id':'fixtureBucketID', 'name':'comic-library-fixture'}
        self.allowed = {'buckets':[self.bucket], 'capabilities':['listBuckets','listFiles','readFiles','writeFiles','deleteFiles'], 'namePrefix':None}
        self.account = {'accountId':'fixtureAccountID', 'authorizationToken':'PrivateFixtureToken123',
                        'apiInfo':{'storageApi':{'apiUrl':'https://api001.backblazeb2.com', 'allowed':self.allowed}}}
        self.listing = {'buckets':[{'bucketId':self.bucket['id'], 'bucketName':self.bucket['name'], 'bucketType':'allPrivate'}]}
        self.requests = []
        def request(req):
            self.requests.append(req)
            if req.url.path.endswith('b2_authorize_account'):
                self.assertEqual(req.url.host, 'api.backblazeb2.com')
                self.assertTrue(req.headers['Authorization'].startswith('Basic '))
                return httpx.Response(200, json=self.account)
            self.assertEqual(req.url.host, 'api001.backblazeb2.com')
            self.assertEqual(req.method, 'POST')
            self.assertEqual(json.loads(req.content), {'accountId':'fixtureAccountID', 'bucketName':self.bucket['name']})
            self.assertEqual(req.headers['Authorization'], 'PrivateFixtureToken123')
            return httpx.Response(200, json=self.listing)
        self.factory = httpx.Client
        self.transport = httpx.MockTransport(request)
        patch('comic_archive.b2_auth.httpx.Client', side_effect=lambda **kw:self.factory(transport=self.transport, **kw)).start()
        self.guard = patch('comic_archive.b2_auth.NetworkGuard').start()
        self.addCleanup(patch.stopall)

    def connect(self):
        return b2_auth.connection(self.root, self.credentials, self.cancel)

    def test_discovers_one_private_bucket_without_password_region_or_bucket_input(self):
        result = self.connect()
        self.assertEqual(result['archive_bucket'], self.bucket['name'])
        self.assertEqual(result['type'], 'b2')
        self.assertEqual(result['hard_delete'], 'true')
        self.assertEqual(result['chunk_size'], '5Mi')
        self.assertEqual(result['upload_concurrency'], '1')
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.guard.return_value.check.called)

    def test_rejects_unrestricted_multiple_bucket_prefix_or_incomplete_permission_keys(self):
        for changes in ({'buckets':None}, {'buckets':[]}, {'buckets':[self.bucket,self.bucket]},
                        {'namePrefix':'other/'}, {'capabilities':['writeFiles']},
                        {'buckets':[{'id':'fixtureBucketID','name':'../../elsewhere'}]}):
            with self.subTest(changes=changes):
                self.allowed.clear(); self.allowed.update(copy.deepcopy({'buckets':[self.bucket], 'capabilities':['listBuckets','listFiles','readFiles','writeFiles','deleteFiles'], 'namePrefix':None}))
                self.allowed.update(changes)
                with self.assertRaises(CloudTransferError) as error:self.connect()
                self.assertEqual(error.exception.cloud_code, 'b2_key_permissions')
        self.assertTrue(all(req.url.path.endswith('b2_authorize_account') for req in self.requests))

    def test_public_bucket_is_rejected_without_any_change_or_upload(self):
        self.listing['buckets'][0]['bucketType'] = 'allPublic'
        with self.assertRaises(CloudTransferError) as error:self.connect()
        self.assertEqual(error.exception.cloud_code, 'b2_private_bucket')
        self.assertEqual(len(self.requests), 2)

    def test_provider_endpoint_cannot_redirect_credentials_elsewhere(self):
        for url in ('http://api001.backblazeb2.com', 'https://example.test',
                    'https://api001.backblazeb2.com@elsewhere.test', 'https://api001.backblazeb2.com/path'):
            self.account['apiInfo']['storageApi']['apiUrl'] = url
            with self.assertRaises(CloudTransferError):self.connect()
        self.assertEqual(len(self.requests), 4)

    def test_cancellation_and_mobile_data_hold_make_no_requests(self):
        self.cancel.set()
        with self.assertRaises(ArchiveError):self.connect()
        self.cancel.clear(); self.guard.return_value.check.side_effect = NetworkPolicyHold('fixture')
        with self.assertRaises(NetworkPolicyHold):self.connect()
        self.assertFalse(self.requests)

    def test_errors_and_oversized_or_invalid_responses_do_not_expose_credentials(self):
        for response, code in ((httpx.Response(401, text='PrivateFixtureKey123'),'b2_key_permissions'),
                               (httpx.Response(429),'cloud_rate_limited'), (httpx.Response(503),'cloud_connection'),
                               (httpx.Response(200, text='x'*65537),'cloud_connection'),
                               (httpx.Response(200, json=[]),'cloud_connection')):
            transport = httpx.MockTransport(lambda req:response)
            with patch('comic_archive.b2_auth.httpx.Client', side_effect=lambda **kw:self.factory(transport=transport, **kw)):
                with self.assertRaises(CloudTransferError) as error:self.connect()
                self.assertEqual(error.exception.cloud_code, code)
                self.assertNotIn('Private', str(error.exception))

    def test_keys_are_validated_before_starting_helper_or_network(self):
        validate('b2', self.credentials | {'application_key':'K005Fixture_A+Key/123='})
        for credentials in ({}, {'key_id':'fixtureKeyID12'}, self.credentials | {'bucket':'other'},
                            self.credentials | {'application_key':'secret\nnew=setting'},
                            self.credentials | {'key_id':123}):
            with self.assertRaises(ArchiveError):validate('b2', credentials)
        self.assertFalse(self.requests)

    def test_signin_persists_private_credentials_without_subprocess_args_or_status(self):
        signin = SignIn(self.root); self.addCleanup(signin.close)
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.Transfer'), patch.object(signin, '_command') as command:
            signin.start('b2', self.credentials); signin._thread.join(5)
            self.assertEqual(signin.status()['status'], 'connected')
            command.assert_not_called()
        value = cloud.settings(self.root); target = value['targets']['b2']
        config = Path(target['config'])
        self.assertEqual(config.stat().st_mode & 0o777, PRIVATE_FILE)
        parser = configparser.ConfigParser(interpolation=None); parser.read(config)
        self.assertEqual(parser['b2']['key'], self.credentials['application_key'])
        self.assertEqual(parser['b2']['archive_bucket'], self.bucket['name'])
        self.assertFalse(target['enabled']); self.assertFalse(target['tested'])
        self.assertNotIn('Private', json.dumps(signin.status()))
        self.assertNotIn('Private', json.dumps(cloud.status(self.root)))
        # A fresh process can use the durable target without a new browser login.
        with patch('comic_archive.cloud.Transfer') as transfer:
            cloud.transfer_for(self.root, 'b2', target)
            self.assertEqual(transfer.call_args.args, ('b2', target))

    def test_failed_or_cancelled_repair_preserves_existing_connection(self):
        with patch('comic_archive.cloud.Transfer'):
            old = self.root/'old.conf'; old.write_text('[b2]\ntype=b2\n'); old.chmod(0o600)
            cloud.configure_target(self.root, 'b2', config=old)
        before = cloud.settings(self.root)
        for cancel in (False, True):
            signin = SignIn(self.root); self.addCleanup(signin.close)
            def connect(*args):
                if cancel:signin.cancel()
                raise CloudTransferError('b2_private_bucket')
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.b2_auth.connection', side_effect=connect):
                signin.start('b2', self.credentials); signin._thread.join(5)
                self.assertEqual(signin.status()['status'], 'cancelled' if cancel else 'error')
            self.assertEqual(cloud.settings(self.root), before)
            self.assertTrue(old.is_file())
            self.assertFalse(list((self.root/'.archive-state/cloud').glob('signin-*')))

    def test_transfer_scopes_paths_and_permanently_deletes_only_the_requested_file(self):
        config = self.root/'b2.conf'
        config.write_text('[b2]\ntype=b2\narchive_bucket=comic-library-fixture\n'); config.chmod(0o600)
        commands = []
        def run(args, **kw):
            commands.append(args)
            kw['stdout'].write(b'b2: b2\n')
            return subprocess.CompletedProcess(args, 0)
        with patch('comic_archive.cloud._binary', return_value='/fixture/rclone'), patch('comic_archive.cloud.subprocess.run', side_effect=run):
            transfer = cloud.Transfer('b2', {'config':str(config), 'remote':'b2'})
            transfer.run('deletefile', transfer.remote('Comic-Archive/fixture/tests/fixture/episode.cbz'))
        self.assertIn('--b2-hard-delete', commands[-1])
        self.assertEqual(commands[-1][-2:], ['deletefile','b2:comic-library-fixture/Comic-Archive/fixture/tests/fixture/episode.cbz'])
        self.assertFalse(any('purge' in args or 'cleanup' in args for args in commands))

    def test_b2_verification_reads_back_sha256_before_local_files_can_be_deleted(self):
        transfer = object.__new__(cloud.Transfer); transfer.provider='b2'; transfer.bucket='comic-library-fixture'
        transfer.target={'remote':'b2'}; transfer.run=Mock(return_value=b'a'*64 + b'  episode.cbz')
        transfer.verify('own/episode.cbz', 'a'*64)
        self.assertEqual(transfer.run.call_args.args, ('hashsum','SHA256','b2:comic-library-fixture/own/episode.cbz','--download'))
        transfer.run.return_value = b'b'*64
        with self.assertRaises(CloudTransferError):transfer.verify('own/episode.cbz', 'a'*64)

    def test_real_stream_child_receives_unknown_size_bytes_with_bounded_b2_buffers(self):
        from comic_archive.cloud_stream import PipeUpload
        script = helper_program(self.root/'helper-fixture', 'import sys,pathlib,json\np=pathlib.Path(sys.argv[sys.argv.index("--config")+1]).parent/"received"\np.write_bytes(sys.stdin.buffer.read())\np.with_suffix(".args").write_text(json.dumps(sys.argv))\n')
        transfer = Mock(provider='b2', binary=str(script), config=self.root/'fixture.conf')
        transfer.remote = lambda p:'b2:comic-library-fixture/' + p
        data = b'fixture' * 100000
        pipe = PipeUpload(transfer, 'Comic-Archive/fixture/episode.cbz')
        try:
            pipe.write(data); pipe.finish()
        finally:
            pipe.close()
        self.assertEqual((self.root/'received').read_bytes(), data)
        args = json.loads((self.root/'received.args').read_text())
        self.assertEqual(args[args.index('--b2-chunk-size') + 1], '5Mi')
        self.assertEqual(args[args.index('--b2-upload-concurrency') + 1], '1')
        self.assertIn('--b2-hard-delete', args)
        self.assertNotIn('--size', args)
