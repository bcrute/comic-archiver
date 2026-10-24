"""Real local publishers/stored files with DATA transports, not a live B2 claim."""

import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_cloud as helpers

from comic_archive import cloud
from comic_archive import reader_catalogue as catalogue
from comic_archive.cloud import Transfer as RealTransfer
from comic_archive.models import ArchiveError, Episode, Manifest, Page
from comic_archive.publication import PublicationStore
from contextlib import closing


class ReaderCatalogueTests(unittest.TestCase):
    setUp = helpers.CloudTests.setUp
    publish = helpers.CloudTests.publish
    configured = helpers.CloudTests.configured
    ready = helpers.CloudTests.ready
    publish_page = helpers.CloudTests.publish_page

    def receipt(self, episode=None):
        found = [
            json.loads(path.read_text())
            for path in (self.root / ".archive-state/cloud/outbox/b2").glob("*.json")
        ]
        return next(
            item for item in found if episode is None or item["episode_id"] == episode
        )

    def context(self, record):
        settings = cloud.settings(self.root)
        library, target = settings["library_id"], settings["targets"]["b2"]["target_id"]
        path = catalogue.state_path(self.root, library, target, record["episode_id"])
        transfer = helpers.FakeTransfer("b2", {})
        return library, target, path, transfer

    def test_actual_completed_upload_publishes_only_after_verification_and_stays_stable(
        self,
    ):
        self.publish()
        self.ready()
        self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        state = catalogue.state_read(path, library, target, record["episode_id"])
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["request"])
        self.assertEqual(state["committed"]["revision"], 1)
        self.assertEqual(state["committed"]["previous"], catalogue.ZERO)
        before = dict(helpers.FakeTransfer.files)
        with patch.object(
            transfer, "run", side_effect=AssertionError("no-op must not query")
        ):
            self.assertTrue(
                catalogue.publish(self.root, transfer, record, library, target)
            )
        self.assertEqual(helpers.FakeTransfer.files, before)
        public = catalogue.encoded(state["committed"])
        for canary in (b"cdn.test", b"rclone.conf", b"source_episode_id", b"target_id"):
            self.assertNotIn(canary, public)

    def test_unknown_metadata_reply_keeps_exact_pending_and_does_not_revoke_original_receipt(
        self,
    ):
        self.publish()
        self.ready()
        original_upload = helpers.FakeTransfer.upload

        def uncertain(transfer, source, destination, expected):
            original_upload(transfer, source, destination, expected)
            if "/.reader/" in destination:
                raise ArchiveError("synthetic lost metadata reply")

        with patch.object(helpers.FakeTransfer, "upload", uncertain):
            self.publish("82")
        record = self.receipt()
        self.assertEqual(record["state"], "verified")
        library, target, path, transfer = self.context(record)
        before = catalogue.state_read(path, library, target, record["episode_id"])
        fixed = copy.deepcopy(before["pending"])
        self.assertIsNotNone(fixed)
        self.assertFalse((self.root / record["archive"]).exists())
        snapshot = dict(helpers.FakeTransfer.files)
        self.assertTrue(catalogue.publish(self.root, transfer, record, library, target))
        after = catalogue.state_read(path, library, target, record["episode_id"])
        self.assertEqual(after["committed"], fixed)
        self.assertIsNone(after["pending"])
        self.assertEqual(helpers.FakeTransfer.files, snapshot)

    def test_stat_failure_retains_fresh_publication_request_for_read_only_retry(self):
        self.publish()
        self.ready()
        with patch.object(
            helpers.FakeTransfer,
            "size",
            side_effect=ArchiveError("synthetic stat failure"),
        ):
            self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        state = catalogue.state_read(path, library, target, record["episode_id"])
        self.assertTrue(state["request"]["advance"])
        self.assertIsNone(state["pending"])
        self.assertTrue(catalogue.publish(self.root, transfer, record, library, target))
        self.assertEqual(
            catalogue.state_read(path, library, target, record["episode_id"])[
                "committed"
            ]["version"],
            record["version"],
        )

    def test_cold_key_rotation_adopts_existing_current_without_new_object_or_version_guess(
        self,
    ):
        self.publish()
        self.ready()
        self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        # Use the real publication/provenance and upload writers for revision2.
        episode = Episode('82', '42', 'Later published title', 82,
                          (Page(1, 'https://cdn.test/a', False),), 'tapas_free')
        with closing(PublicationStore(self.root)) as store:
            store.archive(Manifest(self.series, (episode,)), helpers.MemoryAdapter(),
                          progress=lambda _: None)
        public = catalogue.state_read(path, library, target, record["episode_id"])[
            "committed"
        ]
        self.assertEqual(public["revision"], 2)
        path.unlink()  # Actual local-head loss/restore; no fabricated live owner.
        snapshot = dict(helpers.FakeTransfer.files)
        self.assertTrue(catalogue.publish(self.root, transfer, record, library, target))
        observed = catalogue.state_read(path, library, target, record["episode_id"])
        self.assertEqual(observed["committed"], public)
        self.assertEqual(helpers.FakeTransfer.files, snapshot)

    def test_fork_or_gap_refuses_publication_without_changing_originals(self):
        self.publish()
        self.ready()
        self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        first = catalogue.state_read(path, library, target, record["episode_id"])[
            "committed"
        ]
        changed = dict(first, title="Conflicting first selection")
        helpers.FakeTransfer.files[
            (
                "b2",
                catalogue.prefix(library, record["episode_id"])
                + "/"
                + catalogue.name(changed),
            )
        ] = catalogue.encoded(changed)
        path.unlink()
        snapshot = dict(helpers.FakeTransfer.files)
        self.assertFalse(
            catalogue.publish(self.root, transfer, record, library, target)
        )
        self.assertEqual(helpers.FakeTransfer.files, snapshot)
        with self.assertRaises(ArchiveError):
            catalogue.chain(
                [catalogue.name(dict(first, revision=2, previous="1" * 64))]
            )

    def test_malformed_private_state_and_foreign_target_never_start_a_metadata_write(
        self,
    ):
        self.publish()
        self.ready()
        self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        before = dict(helpers.FakeTransfer.files)
        path.write_text('{"format":"unknown"}')
        with patch.object(
            transfer, "run", side_effect=AssertionError("must refuse before remote IO")
        ):
            self.assertFalse(
                catalogue.publish(self.root, transfer, record, library, target)
            )
            self.assertFalse(
                catalogue.publish(
                    self.root, transfer, record, library, record["episode_id"]
                )
            )
        self.assertEqual(helpers.FakeTransfer.files, before)

    def test_genuine_earlier_receipt_producer_is_readable_without_receipt_rewrite(self):
        self.publish()
        self.ready()
        fixture = Path(__file__).parent / "fixtures/b2-catalogue-v0/cloud_48a0f55.py"
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "e5110517fdd7e657bd392623c4b3e6375fde4ae0eef499c7f132e2289e57f7c3",
        )
        spec = importlib.util.spec_from_file_location(
            "comic_archive.fixture_cloud_48a0f55", fixture
        )
        previous = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(previous)
        # Actual earlier queue AND upload entrypoints produce the stored receipt;
        # only external I/O is an explicitly synthetic recording transport.
        with (
            patch.object(previous, "Transfer", helpers.FakeTransfer),
            patch.object(cloud, "queue_episode", previous.queue_episode),
            patch.object(cloud, "upload_pending", previous.upload_pending),
        ):
            archive, _ = self.publish("82")
        self.assertFalse(archive.exists())
        self.assertFalse(
            any("/.reader/" in key for _, key in helpers.FakeTransfer.files)
        )
        path = next((self.root / ".archive-state/cloud/outbox/b2").glob("*.json"))
        old_bytes = path.read_bytes()
        record = json.loads(old_bytes)
        self.assertEqual(record["state"], "verified")
        library, target, _, transfer = self.context(record)
        self.assertTrue(catalogue.publish(self.root, transfer, record, library, target))
        self.assertEqual(path.read_bytes(), old_bytes)

    def test_first_remote_read_failure_preserves_fresh_request(self):
        self.publish()
        self.ready()
        with patch.object(
            helpers.FakeTransfer,
            "run",
            side_effect=ArchiveError("synthetic listing loss"),
        ):
            self.publish("82")
        record = self.receipt()
        library, target, path, transfer = self.context(record)
        self.assertTrue(
            catalogue.state_read(path, library, target, record["episode_id"])[
                "request"
            ]["advance"]
        )
        self.assertTrue(catalogue.publish(self.root, transfer, record, library, target))

    def test_unsupported_current_edition_has_explicit_null_selection(self):
        self.publish()
        self.ready()
        self.publish("82", novel=True)
        record = self.receipt()
        library, target, path, _ = self.context(record)
        self.assertIsNone(
            catalogue.state_read(path, library, target, record["episode_id"])[
                "committed"
            ]["archive"]
        )

    def test_stat_requires_one_actual_regular_object_with_integer_size(self):
        transfer = object.__new__(RealTransfer)
        transfer.remote = lambda path: "b2:" + path
        for value in (
            [],
            {},
            {"Size": True, "IsDir": False},
            {"Size": -1, "IsDir": False},
            {"Size": 3, "IsDir": True},
            {"Size": "3", "IsDir": False},
        ):
            with (
                self.subTest(value=value),
                patch.object(transfer, "run", return_value=json.dumps(value).encode()),
            ):
                with self.assertRaises(ArchiveError):
                    transfer.size("exact-object")
        with patch.object(
            transfer, "run", return_value=b'{"Size":42,"IsDir":false}'
        ) as run:
            self.assertEqual(transfer.size("exact-object"), 42)
        self.assertEqual(run.call_args.args, ("lsjson", "b2:exact-object", "--stat"))

    def test_strict_public_codec_limits_and_chain_fail_closed(self):
        self.publish()
        self.ready()
        self.publish("82")
        record = self.receipt()
        library, target, path, _ = self.context(record)
        first = catalogue.state_read(path, library, target, record["episode_id"])[
            "committed"
        ]
        for change in (
            {"revision": True},
            {"revision": 129},
            {"order": -1},
            {"archive": {"bytes": True, "sha256": "a" * 64}},
            {"title": "\x00"},
            {"title": "x" * 241},
            {"extra_url": "https://never.invalid"},
        ):
            with self.subTest(change=change), self.assertRaises(ArchiveError):
                catalogue.checked(first | change, library, record["episode_id"])
        for raw in (b'{"x":1,"x":2}', b"[]" * 9000, b'{"x":NaN}'):
            with self.assertRaises(ArchiveError):
                catalogue.parsed(raw)
        with self.assertRaises(ArchiveError):
            catalogue.chain([catalogue.name(first)] * 2)
