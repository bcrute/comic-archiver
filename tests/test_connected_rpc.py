"""Actual index/file/RPC controls, with explicitly fake account/lease policy.

No listener, authenticated peer, native runtime or network fixture is fabricated.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
import zipfile

from comic_archive.connected.files import Index, SharedRoot, SourceError
from comic_archive.connected.rpc import Session, parse_control
from comic_archive.connected.wire import decode_frame, WINDOW_MAX


def identifier():
    return str(uuid.uuid4())


@unittest.skipUnless(os.name == 'posix', 'Initial selected file boundary is POSIX only')
class ConnectedRpcTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.library = self.base / 'share'
        self.library.mkdir()
        self.root = SharedRoot(self.library)
        self.addCleanup(self.root.close)
        self.index = Index(self.base / 'private/index.sqlite', self.root)
        self.addCleanup(self.index.close)
        self.now, self.allowed, self.shared = 0, True, True
        self.challenge = identifier()
        self.session = Session({self.index.share_id: self.index},
                               lambda share: self.allowed and (share is None or self.shared),
                               self.challenge, clock=lambda: self.now)
        self.addCleanup(self.session.close)
        self.record = self.book('one', b'synthetic bytes' * 3000)
        self.hello()

    def book(self, name, data):
        target = self.library / f'{name}.cbz'
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('0001.png', data)
        target.with_suffix('.json').write_text(json.dumps(dict(schema_version=2, export_id=identifier(),
            title=name, format='cbz', content_type='comic', page_count=1,
            content_hashes=[dict(file='0001.png', size=len(data), sha256=hashlib.sha256(data).hexdigest())])))
        return self.index.register_completed(f'{name}.cbz', 'Series')

    def request(self, op, **args):
        return json.dumps(dict(v=1, id=identifier(), op=op, args=args), separators=(',', ':'))

    def call(self, op, **args):
        return json.loads(self.session.handle(self.request(op, **args)))

    def hello(self):
        result = self.call('hello', supported_majors=[1], limits={'archive_bytes':64 * 1024**2},
                           session_challenge=self.challenge)
        self.assertEqual(result['ok']['catalog_revisions'], {self.index.share_id:self.index.revision})

    def args(self):
        return dict(share_id=self.index.share_id, book_id=self.record['book_id'], content_version=self.record['content_version'])

    def open(self):
        return self.call('read.open', **self.args(), offset=0, length=self.record['byte_length'])['ok']['read_id']

    def test_real_index_catalog_and_range_rpc_preserve_original_and_zero_credit(self):
        page = self.call('catalog.list', share_id=self.index.share_id, cursor=None, limit=50)['ok']
        self.assertEqual(page['entries'], [self.record])
        self.assertIsNone(page['next'])
        self.assertNotIn(str(self.library), json.dumps(page))
        read_id = self.open()
        stream = self.session.streams[read_id][0]
        self.assertIsNone(self.session.next_frame(read_id))
        self.assertIn('ok', self.call('read.credit', read_id=read_id, bytes=self.record['byte_length']))
        received = bytearray()
        self.assertIsNone(self.session.next_frame(read_id, buffered_amount=WINDOW_MAX))
        while read_id in self.session.streams:
            _, offset, _, payload, _ = decode_frame(self.session.next_frame(read_id))
            self.assertEqual(offset, len(received))
            received.extend(payload)
        self.assertEqual(received, (self.library / 'one.cbz').read_bytes())
        self.assertEqual(hashlib.sha256(received).hexdigest(), self.record['sha256'])
        self.assertIsNone(stream.source.fd)

    def test_duplicate_credit_and_evicted_request_never_mutate_again(self):
        read_id = self.open()
        text = self.request('read.credit', read_id=read_id, bytes=1)
        reply = self.session.handle(text)
        self.assertEqual(self.session.handle(text), reply)
        self.assertEqual(self.session.streams[read_id][0].credit, 1)
        for _ in range(130):
            self.call('ping', session_challenge=self.challenge)
        self.assertEqual(json.loads(self.session.handle(text))['error']['code'], 'busy')
        self.assertEqual(self.session.streams[read_id][0].credit, 1)
        changed = json.loads(text)
        changed['args']['bytes'] = 2
        with self.assertRaises(SourceError):
            self.session.handle(json.dumps(changed))
        self.assertTrue(self.session.closed)

    def test_cursor_binds_revision_share_limit_and_expiry(self):
        self.book('two', b'second synthetic page')
        first = self.call('catalog.list', share_id=self.index.share_id, cursor=None, limit=1)['ok']
        token = first['next']
        self.assertTrue(token)
        mismatch = self.call('catalog.list', share_id=self.index.share_id, cursor=token, limit=2)
        self.assertEqual(mismatch['error']['code'], 'catalog_changed')
        self.now = 60
        expired = self.call('catalog.list', share_id=self.index.share_id, cursor=token, limit=1)
        self.assertEqual(expired['error']['code'], 'catalog_changed')
        fresh = self.call('catalog.list', share_id=self.index.share_id, cursor=None, limit=1)['ok']
        self.book('three', b'third synthetic page')
        changed = self.call('catalog.list', share_id=self.index.share_id, cursor=fresh['next'], limit=1)
        self.assertEqual(changed['error']['code'], 'catalog_changed')

    def test_completed_catalogue_does_not_hold_all_snapshot_slots(self):
        for _ in range(4):
            self.assertIn('ok', self.call('catalog.list', share_id=self.index.share_id, cursor=None, limit=50))
        self.assertEqual(len(self.session.snapshots), 0)

    def test_revoked_share_cannot_replay_cached_catalogue_and_poll_closes_real_fd(self):
        text = self.request('catalog.list', share_id=self.index.share_id, cursor=None, limit=50)
        self.session.handle(text)
        read_id = self.open()
        stream = self.session.streams[read_id][0]
        self.shared = False
        with self.assertRaises(SourceError) as caught:
            self.session.handle(text)
        self.assertEqual(caught.exception.code, 'share_removed')
        with self.assertRaises(SourceError):
            self.session.next_frame(read_id)
        self.assertIsNone(stream.source.fd)
        event = json.loads(self.session.read_error(read_id, SourceError('share_removed')))
        self.assertEqual(event['error'], {'code':'share_removed', 'retryable':False})

    def test_session_revoke_cancel_and_idle_poll_close_actual_read_handles(self):
        read_id = self.open()
        stream = self.session.streams[read_id][0]
        self.assertIn('ok', self.call('read.cancel', read_id=read_id))
        self.assertIn('ok', self.call('read.cancel', read_id=read_id))
        self.assertIsNone(stream.source.fd)
        read_id = self.open()
        stream = self.session.streams[read_id][0]
        self.now = 30
        with self.assertRaises(SourceError):
            self.session.next_frame(read_id, WINDOW_MAX)
        self.assertIsNone(stream.source.fd)
        self.now = 31
        read_id = self.open()
        stream = self.session.streams[read_id][0]
        self.allowed = False
        with self.assertRaises(SourceError):
            self.session.next_frame(read_id)
        self.assertIsNone(stream.source.fd)
        self.assertTrue(self.session.closed)

    def test_bad_control_and_wrong_challenge_do_not_grant_negotiation(self):
        valid = self.request('ping', session_challenge=self.challenge)
        with self.assertRaises(SourceError):
            parse_control(valid[:-1] + ',"v":1}')
        for extra in ['"\\ud800"', '1e999', '9007199254740992', '[' * 17 + '0' + ']' * 17]:
            with self.assertRaises(SourceError):
                parse_control('{"v":1,"id":"' + identifier() + '","op":"ping","args":{"x":' + extra + '}}')
        self.assertEqual(self.call('ping', session_challenge=identifier())['error']['code'], 'auth_required')

    def test_replacement_and_unknown_operation_never_serve_a_new_original(self):
        old = self.record
        self.record = self.book('one', b'replacement synthetic content')
        self.assertNotEqual(old['content_version'], self.record['content_version'])
        stale = self.call('read.open', share_id=self.index.share_id, book_id=old['book_id'],
                          content_version=old['content_version'], offset=0, length=1)
        self.assertEqual(stale['error']['code'], 'version_changed')
        self.assertEqual(self.call('management.fetch', url='https://private.invalid')['error']['code'], 'unsupported_format')
        self.assertEqual(self.session.streams, {})
