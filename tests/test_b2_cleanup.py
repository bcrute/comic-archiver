import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from comic_archive import b2_cleanup, cloud
from comic_archive.cloud_errors import CloudTransferError
from comic_archive.models import ArchiveError


class B2CleanupTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.path = 'Comic-Archive/00000000-0000-0000-0000-000000000001/tests/00000000-0000-0000-0000-000000000002/00000000-0000-0000-0000-000000000003/episode.cbz'
        self.config = self.root/'b2.conf'
        self.config.write_text('[b2]\ntype=b2\narchive_bucket=comic-library-fixture\naccount=fixtureKeyID12\nkey=PrivateFixtureKey123\n'); self.config.chmod(0o600)
        self.transfer = object.__new__(cloud.Transfer)
        self.transfer.provider='b2'; self.transfer.config=self.config
        self.transfer.bucket='comic-library-fixture'; self.transfer.target={'remote':'b2'}
        self.transfer.guard=Mock()
        self.bucket = {'id':'fixtureBucketID', 'name':self.transfer.bucket}
        self.account = {'authorizationToken':'PrivateFixtureToken123', 'apiInfo':{'storageApi':{
            'apiUrl':'https://api001.backblazeb2.com', 'allowed':{'buckets':[self.bucket]}}}}
        self.files = [{'bucketId':self.bucket['id'], 'fileName':self.path, 'fileId':key, 'action':action}
                      for key, action in [('partial1','start'),('older1','upload'),('current1','upload'),('hidden1','hide')]]
        self.files += [{'bucketId':self.bucket['id'], 'fileName':self.path+'.keep', 'fileId':'unrelated', 'action':'upload'}]
        self.requests=[]; self.removed=[]
        self.factory = httpx.Client
        self.transport = httpx.MockTransport(self.request)
        patch('comic_archive.b2_cleanup.httpx.Client', side_effect=lambda **kw:self.factory(transport=self.transport, **kw)).start()
        self.addCleanup(patch.stopall)

    def request(self, req):
        self.requests.append(req)
        operation = req.url.path.rsplit('/',1)[-1]
        if operation == 'b2_authorize_account':return httpx.Response(200,json=self.account)
        self.assertEqual(req.url.host,'api001.backblazeb2.com')
        values = json.loads(req.content)
        if operation.startswith('b2_list_'):
            self.assertEqual(values['bucketId'],self.bucket['id'])
            partial = operation == 'b2_list_unfinished_large_files'
            self.assertEqual(values['namePrefix' if partial else 'prefix'],self.path)
            return httpx.Response(200,json={'files':[v for v in self.files if (v['action']=='start') == partial], 'nextFileId':None, 'nextFileName':None})
        key=values['fileId'];item=next(v for v in self.files if v['fileId']==key)
        self.assertEqual(item['fileName'],self.path)
        if operation=='b2_cancel_large_file':self.assertEqual(item['action'],'start')
        else:
            self.assertEqual(operation,'b2_delete_file_version')
            self.assertEqual(values['fileName'],self.path)
        self.removed.append(key);self.files.remove(item)
        return httpx.Response(200,json={'fileId':key})

    def test_completed_hidden_versions_and_unfinished_parts_are_deleted_by_exact_identity(self):
        self.transfer.delete_test_file(self.path)
        self.assertEqual(set(self.removed),{'partial1','older1','current1','hidden1'})
        self.assertEqual([item['fileId'] for item in self.files],['unrelated'])
        self.assertFalse(any('/b2_list_buckets' in str(req.url) for req in self.requests))
        # Retrying a completed cleanup is harmless and touches no neighboring file.
        self.transfer.delete_test_file(self.path)
        self.assertEqual(len(self.removed),4)

    def test_a_file_outside_the_apps_own_folder_is_never_deleted(self):
        self.path = self.path.replace('Comic-Archive/', 'Other-Archive/', 1)
        for item in self.files:
            item['fileName'] = item['fileName'].replace('Comic-Archive/', 'Other-Archive/', 1)
        with self.assertRaises(ArchiveError): self.transfer.delete_test_file(self.path)
        self.assertEqual(self.removed, [])

    def test_interrupted_upload_files_are_deleted_only_at_their_recorded_library_path(self):
        version = 'a'*64
        tests = self.path
        self.path = ('Comic-Archive/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000004/'
                     '00000000-0000-0000-0000-000000000003/' + version + '/episode.cbz')
        for item in self.files:
            item['fileName'] = item['fileName'].replace(tests, self.path)
        self.transfer.delete_upload(self.path)
        self.assertEqual(set(self.removed), {'partial1', 'older1', 'current1', 'hidden1'})
        self.assertEqual([item['fileId'] for item in self.files], ['unrelated'])
        asked = len(self.requests)
        for path in (self.path.replace(version, 'b'*63), self.path.replace('episode.cbz', 'other.cbz'),
                     self.path.replace('/00000000-0000-0000-0000-000000000004/', '/series/'), tests):
            with self.assertRaises(ArchiveError): self.transfer.delete_upload(path)
        with self.assertRaises(ArchiveError): self.transfer.delete_test_file(self.path)
        self.assertEqual(len(self.requests), asked)

    def test_invalid_or_general_archive_paths_are_rejected_before_network(self):
        for path in ('', 'Comic-Archive', 'unrelated/episode.cbz', self.path.replace('/tests/','/archives/'),
                     self.path.replace('episode.cbz','../../elsewhere'), self.path.replace('episode.cbz','other.cbz')):
            with self.assertRaises(ArchiveError):self.transfer.delete_test_file(path)
        self.assertFalse(self.requests)

    def test_a_different_connection_or_endpoint_never_receives_removal_requests(self):
        for change in ('bucket','endpoint'):
            account=copy.deepcopy(self.account)
            if change=='bucket':account['apiInfo']['storageApi']['allowed']['buckets'][0]['name']='other-bucket'
            else:account['apiInfo']['storageApi']['apiUrl']='https://elsewhere.test'
            with patch('comic_archive.b2_cleanup._request',return_value=account) as call:
                with self.assertRaises(CloudTransferError):self.transfer.delete_test_file(self.path)
                self.assertEqual(call.call_count,1)
        self.assertFalse(self.removed)

    def test_foreign_bucket_file_id_is_rejected_before_mutation(self):
        self.files[0]['bucketId']='foreign-bucket'
        with self.assertRaises(CloudTransferError):self.transfer.delete_test_file(self.path)
        self.assertFalse(self.removed)

    def test_retained_version_is_a_failure_so_cleanup_receipt_can_retry(self):
        real = self.request
        def request(req):
            if req.url.path.endswith('b2_delete_file_version'):
                return httpx.Response(200,json={})
            return real(req)
        self.transport = httpx.MockTransport(request)
        with self.assertRaises(CloudTransferError) as error:self.transfer.delete_test_file(self.path)
        self.assertEqual(error.exception.cloud_code,'cloud_verification')
        self.assertTrue(any(item['fileName']==self.path for item in self.files))

    def test_exact_staging_file_cleanup_has_the_same_restrictions(self):
        self.path = 'Comic-Archive/00000000-0000-0000-0000-000000000001/.staging/00000000-0000-0000-0000-000000000002/episode.cbz'
        self.files = [{'bucketId':self.bucket['id'],'fileName':self.path,'fileId':'partial1','action':'start'}]
        self.transfer.delete_test_file(self.path)
        self.assertFalse(self.files)

    def test_failed_cleanup_propagates_a_safe_retry_error(self):
        self.transport = httpx.MockTransport(lambda req:httpx.Response(503,text='PrivateFixtureToken123'))
        with self.assertRaises(CloudTransferError) as error:self.transfer.delete_test_file(self.path)
        self.assertNotIn('Private',str(error.exception))
        self.assertFalse(self.removed)

    def test_version_pagination_preserves_exact_prefix_and_name_id_cursor(self):
        real = self.request
        cursors = []
        def request(req):
            if req.url.path.endswith('b2_list_file_versions'):
                values=json.loads(req.content)
                self.assertEqual(values['prefix'],self.path)
                self.assertEqual(values['bucketId'],self.bucket['id'])
                cursors.append(values)
                versions=[item for item in self.files if item['action']!='start' and item['fileName']==self.path]
                if values.get('startFileId'):
                    self.assertEqual(values['startFileName'],self.path)
                    self.assertEqual(values['startFileId'],versions[0]['fileId'])
                return httpx.Response(200,json={'files':versions[:1],
                    'nextFileId':versions[1]['fileId'] if len(versions)>1 else None,
                    'nextFileName':self.path if len(versions)>1 else None})
            return real(req)
        self.transport=httpx.MockTransport(request)
        self.transfer.delete_test_file(self.path)
        self.assertEqual(set(self.removed),{'partial1','older1','current1','hidden1'})
        self.assertTrue(any('startFileId' in values for values in cursors))
