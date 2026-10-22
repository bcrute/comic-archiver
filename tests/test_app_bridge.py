"""The bridge envelope contract: shapes, unknown ops, the frozen cookie-store ABI and the
lifecycle/listing ops against real throwaway workspaces (no network is touched)."""
import hashlib
import importlib.util
import json
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "app_bridge", Path(__file__).resolve().parent.parent / "android/app/src/main/python/app_bridge.py")
app_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_bridge)


class BridgeEnvelopeTests(unittest.TestCase):
    def call(self, request):
        return json.loads(app_bridge.dispatch(request))

    def test_ping_answers_the_envelope_with_a_result(self):
        value = self.call('{"v":1,"op":"ping","args":{}}')
        self.assertTrue(value["ok"])
        self.assertEqual(value["result"]["bridge_version"], 1)
        self.assertIn("python", value["result"])

    def test_unknown_and_malformed_requests_are_refused_without_detail(self):
        for bad in ('{"v":2,"op":"ping","args":{}}', 'not json', '{"v":1,"op":"nope","args":{}}',
                    '{"v":1,"op":"ping","args":7}', '{"v":1,"op":"ping"}'):
            with self.subTest(bad):
                value = self.call(bad)
                self.assertFalse(value["ok"])
                self.assertEqual(value["v"], 1)
                self.assertIn(value["code"], {"bad_request", "unknown_op"})

    def test_non_text_requests_get_the_same_refusal(self):
        value = json.loads(app_bridge.dispatch({"v": 1}))
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "bad_request")

    def test_every_op_in_the_envelope_vocabulary_answers(self):
        # Every §2 op (and the flagged native-line extensions) is a real op now: each one
        # answers its own envelope, never the scaffold's not_implemented.
        for op in ("enqueue_sync", "library_listing", "export_manifest", "export_status",
                   "signin_probe", "signin_restore", "signout_record"):
            with self.subTest(op):
                value = self.call(json.dumps({"v": 1, "op": op, "args": {}}))
                self.assertFalse(value["ok"])
                self.assertNotEqual(value["code"], "not_implemented")
                self.assertNotIn("/", value["message"])


class BridgeLifecycleTests(unittest.TestCase):
    """The lifecycle ops against a real throwaway workspace (no network is touched)."""

    def setUp(self):
        import tempfile
        self._previous = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="bridge-workspace-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name)
        app_bridge._workspace["root"] = None
        app_bridge._seams.update(store=None, cookie_source=None, cookie_sink=None)
        app_bridge._export_cache.update(root=None, facts=None, summary=None)

    def _cleanup(self):
        from comic_archive import network_android
        network_android.configure(None)  # Process-global probe state never leaks to later tests.
        os.chdir(self._previous)
        self._tmp.cleanup()

    def call(self, op, args):
        return json.loads(app_bridge.dispatch(json.dumps({"v": 1, "op": op, "args": args})))

    def test_prepare_workspace_makes_folders_holds_for_go_and_sets_the_cwd(self):
        value = self.call("prepare_workspace", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["held"])
        self.assertEqual(Path.cwd(), self.root)
        self.assertTrue((self.root / "Library").is_dir())
        self.assertTrue((self.root / ".comic-session").is_dir())

    def test_prepare_workspace_refuses_a_symlinked_root(self):
        link = Path(self._tmp.name) / "link"
        os.symlink(self.root / "real", link)
        value = self.call("prepare_workspace", {"root": str(link)})
        self.assertFalse(value["ok"])
        # Refused at the bridge boundary (every path arg is), before the core sees it.
        self.assertEqual(value["code"], "bad_request")

    def test_worker_status_reads_the_protocol_file(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_status", {"session_root": str(self.root / ".comic-session")})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["running"])

    def test_worker_start_needs_the_cookie_seams_before_anything_else(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "cookie_seams_missing")

    def test_worker_start_needs_a_whole_sign_in_record(self):
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def test_worker_stop_answers_through_the_envelope_when_no_worker_runs(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_stop", {"session_root": str(self.root / ".comic-session"),
                                          "wait_seconds": 0})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["running"])

    def test_go_needs_the_downloader_and_pause_answers_quietly(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        # Go before any worker runs is the core's own refusal: consecutive imports need
        # the downloader's queue first. The app wires worker_start before go, so this is
        # exactly the envelope a premature Go must produce.
        value = self.call("go", {"root": str(self.root)})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])

    def test_download_status_reports_durable_plan_separately_from_worker_liveness(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        queue = self.root / ".comic-session/worker"
        (queue / "requests").mkdir(parents=True)
        (queue / "responses").mkdir()
        self.assertTrue(self.call("go", {"root": str(self.root)})["ok"])
        value = self.call("download_status", {"root": str(self.root)})
        self.assertTrue(value["ok"])
        self.assertEqual(set(value["result"]), {"account", "worker", "counts", "progress"})
        self.assertEqual(value["result"]["account"]["status"], "running")
        self.assertEqual(value["result"]["account"]["phase"], "lists")
        self.assertFalse(value["result"]["worker"]["running"])
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])
        value = self.call("download_status", {"root": str(self.root)})
        self.assertEqual(value["result"]["account"]["status"], "stopped")

    def test_path_arguments_are_validated_without_detail(self):
        for bad in ({}, {"root": "Library"}, {"root": None}, {"session_root": "/tmp/a\0b"}):
            with self.subTest(bad):
                value = self.call("prepare_workspace", bad)
                self.assertFalse(value["ok"])
                self.assertEqual(value["code"], "bad_request")

    def test_cookie_seams_must_be_callables(self):
        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams("not callable")

    def test_the_kotlin_cookie_store_registers_by_its_frozen_names(self):
        # Chaquopy performs no snake_case conversion, so the store Python sees carries its
        # frozen camelCase methods; registration keeps the store itself (the adapter wraps
        # it at use time) and the worker then fails on the missing sign-in, not the seams.
        class Store:
            def headerFor(self, url):
                return "session=1"

            def apply(self, url, values):
                return None

        store = Store()
        app_bridge.set_cookie_seams(store)
        self.assertIs(app_bridge._seams["store"], store)
        self.assertIsNone(app_bridge._seams["cookie_source"])
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def test_a_store_without_the_frozen_methods_is_refused_not_swallowed(self):
        class HalfStore:
            def headerFor(self, url):
                return None

        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams(HalfStore())
        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams(object())

    def test_signin_probe_without_seams_is_an_honest_missing(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("signin_probe", {"session_root": str(self.root / ".comic-session")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "cookie_seams_missing")

    def test_restore_without_saved_session_issues_no_account_probe(self):
        from unittest.mock import patch
        with patch("comic_archive.android_adapter.signed_in_probe") as probe:
            value = self.call("signin_restore", {"session_root": str(self.root / ".comic-session")})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["signed_in"])
        probe.assert_not_called()

    def test_restore_verifies_saved_session_without_starting_worker_or_plan(self):
        from unittest.mock import patch
        from comic_archive import android_session, worker_host
        session = self.root / ".comic-session"
        android_session.mark_signed_in(session)
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        verified = {"signed_in": True, "verified": True, "reason": None}
        with patch("comic_archive.android_adapter.signed_in_probe", return_value=verified) as probe:
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertTrue(value["result"]["signed_in"])
        probe.assert_called_once()
        self.assertFalse(worker_host.status(session)["running"])
        self.assertFalse((session / "worker/account-export.json").exists())

    def test_restore_expired_saved_session_does_not_authenticate(self):
        from unittest.mock import patch
        from comic_archive import android_session
        session = self.root / ".comic-session"
        android_session.mark_signed_in(session)
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        refused = {"signed_in": False, "verified": False, "reason": "not_signed_in"}
        with patch("comic_archive.android_adapter.signed_in_probe", return_value=refused):
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["signed_in"])

    def test_restore_refuses_torn_saved_record_before_network(self):
        from unittest.mock import patch
        session = self.root / ".comic-session"
        session.mkdir()
        (session / "android.json").write_text('{"schema_version":1}')
        with patch("comic_archive.android_adapter.signed_in_probe") as probe:
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertFalse(value["ok"])
        probe.assert_not_called()

    def test_signout_record_forgets_the_written_sign_in(self):
        from comic_archive import android_session
        session = self.root / ".comic-session"
        session.mkdir()
        android_session.mark_signed_in(session)
        value = self.call("signout_record", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(android_session.is_signed_in(session))

    def test_enqueue_sync_needs_the_sign_in_first(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("enqueue_sync", {"session_root": str(self.root / ".comic-session")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def _export_library(self):
        comic = self.root / "Library" / "Sample Comic"
        comic.mkdir(parents=True)
        (comic / "series.json").write_text(json.dumps(
            {"source_series_id": "123", "title": "Sample Comic", "episode_count": 2}))
        (comic / "episode-1.cbz").write_bytes(b"data")
        (comic / "episode-1.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "456",
             "content_type": "comic", "format": "cbz", "title": "One"}))
        # Contract §5: tar.zst is not exportable. It still counts as saved for the listing,
        # and the sidecar-less cbz is offered nowhere at all.
        (comic / "episode-2.tar.zst").write_bytes(b"")
        (comic / "episode-2.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "789",
             "content_type": "comic", "format": "tar.zst", "title": "Two"}))
        (comic / "episode-3.cbz").write_bytes(b"no sidecar")
        novel = self.root / "Library" / "Sample Novel"
        novel.mkdir(parents=True)
        (novel / "series.json").write_text(json.dumps(
            {"source_series_id": "999", "title": "Sample Novel"}))
        (novel / "chapter-1.epub").write_bytes(b"novel")
        (novel / "chapter-1.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "999", "source_episode_id": "1000",
             "content_type": "novel", "format": "epub", "title": "Chapter"}))

    def test_library_listing_offers_series_by_the_cores_rules(self):
        self._export_library()
        value = self.call("library_listing", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        self.assertEqual(value["result"], [
            {"seriesId": "123", "title": "Sample Comic", "kind": "comic",
             "episodeCount": 2, "savedCount": 2},
            {"seriesId": "999", "title": "Sample Novel", "kind": "novels",
             "episodeCount": 1, "savedCount": 1}])

    def test_export_manifest_digests_the_originals_and_skips_tar_zst(self):
        self._export_library()
        value = self.call("export_manifest", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        entries = value["result"]["entries"]
        self.assertEqual([entry["filename"] for entry in entries],
                         ["episode-1.cbz", "chapter-1.epub"])
        self.assertEqual({entry["kind"] for entry in entries}, {"CBZ", "EPUB"})
        comic = entries[0]
        self.assertEqual(comic["sha256"], hashlib.sha256(b"data").hexdigest())
        self.assertEqual(comic["bytes"], 4)
        self.assertEqual(comic["episodeId"], "456")
        self.assertEqual(comic["seriesId"], "123")
        self.assertEqual(comic["relpath"], "Sample Comic/episode-1.cbz")
        self.assertRegex(value["result"]["manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_export_status_answers_the_summary_and_follows_changes(self):
        self._export_library()
        first = self.call("export_status", {"root": str(self.root)})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["entries"], 2)
        self.assertEqual(first["result"]["bytes"], 9)
        manifest = self.call("export_manifest", {"root": str(self.root)})["result"]
        self.assertEqual(first["result"]["manifest_sha256"], manifest["manifest_sha256"])
        extra = self.root / "Library" / "Sample Comic"
        (extra / "episode-4.cbz").write_bytes(b"more")
        (extra / "episode-4.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "457",
             "content_type": "comic", "format": "cbz", "title": "Four"}))
        second = self.call("export_status", {"root": str(self.root)})
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["result"]["entries"], 3)

