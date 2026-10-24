"""Actual native stored-file transitions; metadata is NOT live service authority."""

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from comic_archive.connected.files import Index, SharedRoot, SourceError
from comic_archive.connected.pairing import PairingOwner
from comic_archive.connected.renewal import DAY_MS, LIFETIME_MS
from comic_archive.connected.signaling import Identity
from comic_archive.connected.state import STATE, HostState


class RenewalStateTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.base = Path(work.name)
        self.indices, self.roots = {}, []
        for number in (1, 2):
            folder = self.base / f"shared-{number}"
            folder.mkdir()
            root = SharedRoot(folder)
            self.addCleanup(root.close)
            index = Index(self.base / f"private-{number}/index.sqlite", root)
            self.addCleanup(index.close)
            self.roots.append(root)
            self.indices[index.share_id] = index
        fixture = Path(__file__).parent / "fixtures/connected-host-v1/state_0865d83.py"
        # Genuine earlier bytes, not refreshed alongside the new implementation.
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "7ddb8ced2e8e62e63cc8d992bc2a41dd6da42e1c589a74df0f2a783a32621f9a",
        )
        spec = importlib.util.spec_from_file_location(
            "comic_archive.connected.fixture_state_0865d83", fixture
        )
        self.prior = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.prior)
        old = self.prior.HostState.open(
            self.base / "host", roots=self.roots, create=True
        )
        try:
            self.pair = str(uuid.uuid4())
            old._pending(self.pair)
            # Stored-format DATA only; no ApprovalReview or live Account/Peer is
            # constructed from it, and no file-read/HTTP authority is asserted.
            old._approve(
                self.pair,
                str(uuid.uuid4()),
                Identity.generate().public_bundle(),
                list(self.indices),
                str(uuid.uuid4()),
                "a" * 64,
            )
            self.original = old.records()[0]
            self.identity = old.identity.public_bundle()
        finally:
            old.close()
        self.before = (self.base / "host" / STATE).read_bytes()
        self.state = HostState.open(self.base / "host", roots=self.roots)
        self.addCleanup(lambda: self.state.close())

    def intent(self):
        row = self.state.record(self.pair)
        value = {
            name: row[name]
            for name in (
                "pairing_id",
                "account_binding",
                "reader_keys",
                "approval_id",
                "shares",
            )
        }
        value.update(
            from_generation=0,
            generation=1,
            previous_expires_at=5 * DAY_MS,
            token_expires_at=LIFETIME_MS,
        )
        return value

    def test_genuine_earlier_state_opens_without_rewrite_and_rotation_survives_reopen(
        self,
    ):
        self.assertEqual(self.state.records(), [self.original])
        self.assertEqual(self.state.identity.public_bundle(), self.identity)
        self.assertEqual((self.base / "host" / STATE).read_bytes(), self.before)
        intent = self.state._prepare_renewal(self.intent())
        written = (self.base / "host" / STATE).read_bytes()
        self.assertEqual(
            json.loads(written)["format"], "comic-archiver.connected-host/2"
        )
        self.state.close()
        with self.assertRaises(SourceError):
            self.prior.HostState.open(self.base / "host", roots=self.roots)
        self.assertEqual((self.base / "host" / STATE).read_bytes(), written)
        self.state = HostState.open(self.base / "host", roots=self.roots)
        self.assertEqual(self.state.renewal(self.pair), intent)
        self.assertEqual(self.state.records(), [self.original])
        self.assertEqual(self.state.identity.public_bundle(), self.identity)

    def test_intent_retry_is_fixed_and_changed_or_malformed_candidate_never_writes(
        self,
    ):
        intent = self.state._prepare_renewal(self.intent())
        before = (self.base / "host" / STATE).read_bytes()
        self.assertEqual(self.state._prepare_renewal(copy.deepcopy(intent)), intent)
        self.assertEqual((self.base / "host" / STATE).read_bytes(), before)
        for field, value in [
            ("token_expires_at", LIFETIME_MS + 1),
            ("generation", 3),
            ("previous_expires_at", True),
            ("shares", []),
            ("account_binding", str(uuid.uuid4())),
        ]:
            changed = copy.deepcopy(intent)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(SourceError):
                self.state._prepare_renewal(changed)
            self.assertEqual((self.base / "host" / STATE).read_bytes(), before)

    def test_pending_rotation_blocks_generic_ack_and_socket_until_exact_finish(self):
        intent = self.state._prepare_renewal(self.intent())
        owner = PairingOwner(self.state, self.indices)
        for operation in [
            lambda: owner.control_approval(self.pair, "http://127.0.0.1:9"),
            lambda: owner.control_scope(self.pair, "http://127.0.0.1:9"),
            lambda: owner.control_socket(self.pair, "http://127.0.0.1:9", "a" * 64),
        ]:
            with self.assertRaises(SourceError):
                operation()
        with self.assertRaises(SourceError):
            self.state._finish_renewal(intent, "not_committed")
        self.assertEqual(self.state.renewal(self.pair), intent)
        self.state._finish_renewal(intent, "committed")
        expected = dict(self.original, generation=1)
        self.assertEqual(self.state.records(), [expected])
        self.assertIsNone(self.state.renewal(self.pair))
        self.assertEqual(self.state.receipt(self.pair), intent)
        self.state.close()
        self.state = HostState.open(self.base / "host", roots=self.roots)
        self.assertEqual(self.state.receipt(self.pair), intent)
        owner = PairingOwner(self.state, self.indices)
        self.assertFalse(
            owner.control_renewal(self.pair, "http://127.0.0.1:9", receipt=True)[2]
        )
        self.assertTrue(owner.control_scope(self.pair, "http://127.0.0.1:9"))

    def test_next_rotation_keeps_previous_receipt_until_new_commit(self):
        first = self.state._prepare_renewal(self.intent())
        self.state._finish_renewal(first, "committed")
        second = dict(
            first,
            from_generation=1,
            generation=2,
            previous_expires_at=LIFETIME_MS,
            token_expires_at=2 * LIFETIME_MS,
        )
        self.state._prepare_renewal(second)
        owner = PairingOwner(self.state, self.indices)
        _, observed, allow_start, _ = owner.control_renewal(
            self.pair, "http://127.0.0.1:9", receipt=True
        )
        self.assertEqual(observed, first)
        self.assertFalse(allow_start)
        self.assertEqual(
            owner.control_renewal(self.pair, "http://127.0.0.1:9")[1], second
        )
        self.state._finish_renewal(second, "committed")
        self.assertEqual(self.state.receipt(self.pair), second)
        self.assertEqual(self.state.record(self.pair)["generation"], 2)

    def test_local_unshare_skips_reserved_counter_and_never_restarts_broad_intent(self):
        intent = self.state._prepare_renewal(self.intent())
        removed = list(self.indices)[1]
        self.state.remove_share(removed)
        row = self.state.record(self.pair)
        self.assertEqual(row["generation"], 2)
        self.assertEqual(row["shares"], [list(self.indices)[0]])
        owner = PairingOwner(self.state, self.indices)
        _, observed, allow_start, current = owner.control_renewal(
            self.pair, "http://127.0.0.1:9"
        )
        self.assertFalse(allow_start)
        self.assertEqual(observed, intent)
        self.assertEqual(current, row)
        self.state._finish_renewal(intent, "committed")
        self.assertEqual(self.state.record(self.pair), row)
        self.assertIsNone(self.state.renewal(self.pair))
        self.assertEqual(self.state.receipt(self.pair), intent)

    def test_uncommitted_reduced_intent_can_retire_without_advancing_approval(self):
        intent = self.state._prepare_renewal(self.intent())
        self.state.remove_share(list(self.indices)[1])
        reduced = self.state.record(self.pair)
        self.state._finish_renewal(intent, "not_committed")
        self.assertEqual(self.state.record(self.pair), reduced)
        self.assertIsNone(self.state.renewal(self.pair))

    def test_revoke_dominates_pending_rotation_and_late_ack_never_revives(self):
        intent = self.state._prepare_renewal(self.intent())
        self.state.revoke(self.pair)
        row = self.state.record(self.pair)
        self.assertEqual(
            (row["status"], row["generation"], row["shares"]), ("revoked", 2, [])
        )
        self.assertIsNone(self.state.renewal(self.pair))
        self.assertIsNone(self.state.receipt(self.pair))
        with self.assertRaises(SourceError):
            self.state._finish_renewal(intent, "committed")
        self.assertEqual(self.state.record(self.pair), row)

    def test_uncertain_intent_fsync_poisoning_retains_earlier_installed_state(self):
        with patch(
            "comic_archive.connected.state.os.fsync", side_effect=OSError("injected")
        ):
            with self.assertRaises(SourceError):
                self.state._prepare_renewal(self.intent())
        self.assertTrue(self.state.closed)
        self.assertEqual((self.base / "host" / STATE).read_bytes(), self.before)
        self.assertTrue(list((self.base / "host").glob(".pending-*")))
