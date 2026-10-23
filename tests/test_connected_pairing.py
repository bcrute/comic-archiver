"""Actual private file/lock/key/bootstrap custody, not service/RTC/lease proof.

Opt-in JOSE dependency is required at the original connected-reader point. These
public synthetic local fixtures never impersonate an account or active read grant.
"""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from comic_archive.connected.files import Index, SharedRoot, SourceError

AVAILABLE = os.name == 'posix' and importlib.util.find_spec('jwcrypto') is not None
if AVAILABLE:
    from comic_archive.connected.pairing import PairingOwner
    from comic_archive.connected.signaling import Identity, SignalSession, verify_bundle_proof
    from comic_archive.connected.state import HostState, STATE
else:
    PairingOwner = Identity = SignalSession = HostState = verify_bundle_proof = STATE = None


def identifier():
    return str(uuid.uuid4())


@unittest.skipUnless(AVAILABLE, 'POSIX/connected extra required; a skip is no connected qualification')
class ConnectedPairingTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.shared = self.base / 'shared'
        self.shared.mkdir()
        self.root = SharedRoot(self.shared)
        self.addCleanup(self.root.close)
        self.index = Index(self.base / 'index/private.sqlite', self.root)
        self.addCleanup(self.index.close)
        self.path = self.base / 'host'
        self.state = HostState.open(self.path, roots=[self.root], create=True)
        self.now, self.wall = 0, 1_800_000_000
        self.owner = self.owner_for(self.state)
        self.addCleanup(self.cleanup_owner)

    def cleanup_owner(self):
        self.owner.close()

    def owner_for(self, state):
        return PairingOwner(state, {self.index.share_id:self.index}, clock=lambda: self.now, wall=lambda: self.wall)

    def reopen(self):
        self.owner.close()
        self.state = HostState.open(self.path, roots=[self.root])
        self.owner = self.owner_for(self.state)

    def candidate(self, secret_change=False):
        draft = self.owner.start()
        url = self.owner.qr_url(draft, 'https://reader.example')
        self.assertNotIn('bootstrap_secret', url.split('#')[0])
        encoded = url.split('#cl1=')[1]
        qr = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        secret = base64.urlsafe_b64decode(qr['bootstrap_secret'] + '=')
        self.assertEqual(len(secret), 32)
        reader = Identity.generate()
        # Synthetic opaque account binding is DATA. No server account/lease
        # is constructed from it; local bootstrap still owns actual secret/keys.
        account = identifier()
        proof = self.state.identity.attest(draft.challenge, 'host', int(self.wall * 1000))
        bundle = verify_bundle_proof(proof, qr['host_signing_pin'], draft.challenge, 'host', int(self.wall * 1000))
        session = SignalSession(reader, bundle, dict(pairing_id=draft.pairing_id, session_id=draft.session_id,
            account_binding=account, challenge=draft.challenge, generation=0), 'reader', lambda: True,
            clock=lambda: self.now, wall=lambda: self.wall)
        self.addCleanup(session.close)
        wire = session.seal('pair.request', dict(reader_keys=reader.public_bundle()), b'x' * 32 if secret_change else secret)
        return draft, reader, session, account, wire

    def review(self):
        draft, reader, session, account, wire = self.candidate()
        return self.owner.receive(draft, account, reader.public_bundle(), wire), session

    def test_actual_private_keys_reopen_with_same_identity_and_missing_file_never_rotates(self):
        before = self.state.identity.public_bundle()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.path / STATE).stat().st_mode & 0o777, 0o600)
        self.reopen()
        self.assertEqual(self.state.identity.public_bundle(), before)
        with self.assertRaises(SourceError):
            HostState.open(self.path, create=True)
        self.owner.close()
        (self.path / STATE).unlink()
        with self.assertRaises(SourceError):
            HostState.open(self.path)
        self.assertFalse((self.path / STATE).exists())

    def test_wrong_permissions_hardlink_foreign_format_and_shared_state_refuse_without_rewrite(self):
        self.owner.close()
        original = (self.path / STATE).read_bytes()
        os.chmod(self.path / STATE, 0o644)
        with self.assertRaises(SourceError):
            HostState.open(self.path)
        os.chmod(self.path / STATE, 0o600)
        os.link(self.path / STATE, self.base / 'linked-state')
        with self.assertRaises(SourceError):
            HostState.open(self.path)
        (self.base / 'linked-state').unlink()
        record = json.loads(original)
        record['format'] = 'foreign/1'
        (self.path / STATE).write_text(json.dumps(record))
        foreign = (self.path / STATE).read_bytes()
        with self.assertRaises(SourceError):
            HostState.open(self.path)
        self.assertEqual((self.path / STATE).read_bytes(), foreign)
        (self.path / STATE).unlink()
        os.mkfifo(self.path / STATE, 0o600)
        with self.assertRaises(SourceError):
            HostState.open(self.path)
        with self.assertRaises(SourceError):
            HostState.open(self.shared / 'must-not-create', roots=[self.root], create=True)
        self.assertFalse((self.shared / 'must-not-create').exists())

    def test_process_lock_prevents_second_owner_without_retiring_original_handles(self):
        with self.assertRaises(SourceError) as caught:
            HostState.open(self.path)
        self.assertEqual(caught.exception.code, 'busy')
        self.assertTrue(self.state.current())
        self.assertIsNotNone(self.state.identity)

    def test_real_review_cannot_be_reconstructed_or_mutated_and_requires_actual_selected_index(self):
        review, _ = self.review()
        with self.assertRaises(AttributeError):
            review.account_binding = identifier()
        changed = self.owner.describe(review)
        changed['reader_keys']['signing']['x'] = 'changed DTO'
        self.assertNotEqual(self.owner.describe(review), changed)
        for forged in [changed, copy.copy(review)]:
            with self.assertRaises(SourceError):
                self.owner.approve(forged, [self.index])
        for index in [{'share_id':self.index.share_id}, copy.copy(self.index)]:
            with self.assertRaises(SourceError):
                self.owner.approve(review, [index])
        self.assertFalse(review.used)

    def test_approval_commits_once_then_reopen_unshare_and_revoke_preserve_tombstones(self):
        review, reader = self.review()
        before = self.state.identity.public_bundle()
        acknowledgement = self.owner.approve(review, [self.index])
        self.assertEqual(reader.open(acknowledgement)['body'], dict(shares=[self.index.share_id], generation=0))
        self.assertTrue(review.used)
        with self.assertRaises(SourceError):
            self.owner.approve(review, [self.index])
        self.reopen()
        self.assertEqual(self.state.identity.public_bundle(), before)
        row = self.state.records()[0]
        self.assertEqual(row['approval_id'], review.approval_id)
        self.assertEqual(row['status'], 'approved')
        self.assertTrue(self.owner.remove_share(self.index.share_id))
        self.assertFalse(self.owner.remove_share(self.index.share_id))
        self.reopen()
        row = self.state.records()[0]
        self.assertEqual((row['status'], row['generation'], row['shares']), ('revoked', 1, []))
        self.assertFalse(self.owner.revoke(row['pairing_id']))
        self.assertEqual(self.state.records()[0]['generation'], 1)

    def test_restart_retires_pending_secrets_and_cannot_replay_old_draft(self):
        draft, reader, _, account, wire = self.candidate()
        self.reopen()
        self.assertIsNone(draft._secret)
        self.assertEqual(self.state.records()[0]['status'], 'revoked')
        with self.assertRaises(SourceError):
            self.owner.receive(draft, account, reader.public_bundle(), wire)
        with self.assertRaises(SourceError):
            self.state._pending(draft.pairing_id)
        self.assertNotEqual(self.owner.start().pairing_id, draft.pairing_id)

    def test_invalid_bootstrap_and_monotonic_expiry_retire_pending_native_custody(self):
        draft, reader, _, account, wire = self.candidate(secret_change=True)
        with self.assertRaises(SourceError):
            self.owner.receive(draft, account, reader.public_bundle(), wire)
        self.assertIsNone(draft._secret)
        self.assertEqual(self.state.records()[0]['status'], 'revoked')
        first, second = self.owner.start(), self.owner.start()
        with self.assertRaises(SourceError):
            self.owner.start()
        self.now = 300
        self.owner.expire()
        self.assertIsNone(first._secret)
        self.assertIsNone(second._secret)
        self.assertTrue(all(row['status'] == 'revoked' for row in self.state.records()))

    def test_actual_replace_with_lost_reply_poison_owner_and_never_rearms_approval(self):
        review, _ = self.review()
        replace = os.replace
        old_fd = self.state.state_fd
        def lost_reply(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('synthetic lost replace response')
        with patch('comic_archive.connected.state.os.replace', side_effect=lost_reply):
            with self.assertRaises(SourceError):
                self.owner.approve(review, [self.index])
        self.assertTrue(self.state.closed)
        self.assertTrue(review.used)
        with self.assertRaises(OSError):
            os.fstat(old_fd)
        self.reopen()
        rows = self.state.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['status'], rows[0]['approval_id']), ('approved', review.approval_id))
        with self.assertRaises(SourceError):
            self.owner.approve(review, [self.index])

    def test_failed_installed_readback_retires_old_and_new_real_state_descriptors(self):
        review, _ = self.review()
        before = self.state.identity.public_bundle()
        fds = [self.state.state_fd, self.state.lock_fd, self.state.directory]
        with patch.object(HostState, '_read', side_effect=OSError('synthetic installed readback failure')):
            with self.assertRaises(SourceError):
                self.owner.approve(review, [self.index])
        self.assertTrue(self.state.closed)
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.reopen()
        self.assertEqual(self.state.identity.public_bundle(), before)
        self.assertEqual(self.state.records()[0]['approval_id'], review.approval_id)

    def test_foreign_thread_refusal_does_not_close_or_reuse_real_owner_descriptors(self):
        observed = []
        fd = self.state.state_fd
        def foreign():
            try:
                self.state.current()
            except SourceError as error:
                observed.append(error.code)
        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, ['cancelled'])
        self.assertTrue(os.fstat(fd).st_size > 0)
        self.assertTrue(self.state.current())
